#!/usr/bin/env python3
"""Agent u trati — drát mezi aplikací v cloudu a železem v klubové síti.

Dekodéry MyLaps i cílová kamera mají privátní adresy (`192.168.x.y`), na které
se server aplikace z datového centra nedostane. Tenhle program běží na počítači
u trati, **sám se hlásí serveru** a dělá to, oč si řekne: připojí se na
zadanou adresu, pošle bajty a vrátí, co přišlo zpátky.

Ven jde jen odchozí HTTPS, takže se na routeru pořadatele nic neotevírá.

Spuštění:

    python3 tools/track_agent.py

Adresa aplikace je předvyplněná (`DEFAULT_SERVER`) a token si program vyrobí
sám — ukáže ho na své stránce a obsluha ho opíše v aplikaci do Nastavení
aplikace → Přihlásit krabičku. Vlastní server a hotový token se dají předat
přepínači `--server` / `--token` nebo prostředím (`EVENT_CONTROL_SERVER`,
`EVENT_CONTROL_AGENT_TOKEN`).

Program je schválně **jen ze standardní knihovny**: na notebooku u trati se
nemá co instalovat a nemá co se rozbít. Neví nic o P3 ani o formátu startovky
— protokoly zůstávají na serveru, takže aktualizace aplikace neznamená
aktualizaci notebooků.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

VERSION = "1.3"

#: Kam se agent hlásí, když mu nikdo neřekl jinak. Aplikace běží na jednom
#: místě, takže adresu nemá co obsluha u trati vypisovat — krabička po zapnutí
#: rovnou ukáže token a jediné, co zbývá, je opsat ho v aplikaci. Vlastní
#: server se pořád dá zadat na stránce krabičky, přepínačem nebo prostředím.
DEFAULT_SERVER = "https://bikody.com"

#: Server drží dotaz otevřený, dokud nemá co poslat. Čtecí timeout musí být
#: delší, jinak by agent spojení trhal těsně před odpovědí.
READ_TIMEOUT = 40.0

#: Po výpadku sítě se zkouší dál, jen pomaleji — u trati se běžně přepojuje
#: kabel nebo přepíná wifi a agent to má přežít bez zásahu obsluhy.
RECONNECT_MIN = 1.0
RECONNECT_MAX = 15.0

#: Jak často se krabička ptá, jestli už ji někdo v aplikaci schválil. Obsluha
#: mezitím opisuje token z displeje, takže ani rychleji, ani líně.
APPROVAL_POLL_SECONDS = 5.0


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class Server:
    """Dotazy na aplikaci. Token se posílá v hlavičce, ne v adrese."""

    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token

    def _request(self, path: str, payload: dict | None = None, *, timeout: float) -> dict:
        url = f"{self.base}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")

    def hello(self) -> dict:
        return self._request(
            "/bmx/api/agent/hello/",
            {"hostname": socket.gethostname(), "version": VERSION},
            timeout=15.0,
        )

    def commands(self) -> list[dict]:
        answer = self.poll()
        return answer.get("commands") or []

    def poll(self) -> dict:
        """Dlouhý dotaz: příkazy k vyřízení + konfigurace proudu průjezdů."""
        return self._request("/bmx/api/agent/commands/", timeout=READ_TIMEOUT)

    def push_passings(self, decoder_id: str, frames: list[str]) -> dict:
        """Pošle rámce průjezdů hned, jak je dekodér vydal (base64)."""
        return self._request(
            "/bmx/api/agent/passings/",
            {"decoder": decoder_id, "frames": frames},
            timeout=15.0,
        )

    def result(self, command_id: str, ok: bool, data: dict | None = None, error: str = "") -> None:
        self._request(
            "/bmx/api/agent/result/",
            {"id": command_id, "ok": ok, "data": data or {}, "error": error},
            timeout=15.0,
        )


# --- spojení na železo -----------------------------------------------------
#
# Dekodér MyLaps pustí najednou **čtyři spojení** a víc jich nepustí ani na
# chvíli. Kdyby agent otevíral nové spojení na každý dotaz, vyčerpal by je sám
# sebou: odběr průjezdů se ptá po vteřinách a kontrolka v liště taky. Spojení
# se proto drží otevřené a používá se znovu — na dekodér se pak jde jedním
# slotem místo nekonečné řady.

_pool: dict[tuple[str, int], socket.socket] = {}

#: Posledních pár spojení na železo. Na displeji krabičky u trati je to jediné,
#: podle čeho obsluha pozná, jestli se dekodéry a kamera ozývají — do aplikace
#: se přes rameno nekouká.
_recent: list[dict] = []
_RECENT_MAX = 6


def _remember(host: str, port: int, ok: bool, detail: str = "") -> None:
    entry = {
        "cil": f"{host}:{port}",
        "ok": ok,
        "detail": detail,
        "cas": time.strftime("%H:%M:%S"),
    }
    _recent.insert(0, entry)
    del _recent[_RECENT_MAX:]


def _drop(key: tuple[str, int]) -> None:
    sock = _pool.pop(key, None)
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass


def _connection(host: str, port: int, timeout: float) -> socket.socket:
    """Držené spojení na tuhle adresu, nebo nové."""
    key = (host, int(port))
    sock = _pool.get(key)
    if sock is not None:
        return sock
    sock = socket.create_connection(key, timeout=timeout)
    _pool[key] = sock
    return sock


def _drain(sock: socket.socket) -> None:
    """Zahodí, co v bufferu zbylo z minula.

    Dekodér posílá sám od sebe status každých pár sekund. Kdyby to zůstalo
    ve frontě, přimíchalo by se to k odpovědi na další dotaz — server sice
    cizí rámce přeskakuje, ale zbytečně by se přenášely.
    """
    sock.setblocking(False)
    try:
        while True:
            if not sock.recv(8192):
                break
    except (BlockingIOError, OSError):
        pass
    finally:
        sock.setblocking(True)


# --- co agent umí ----------------------------------------------------------


def tcp_probe(args: dict) -> dict:
    """Poslouchá na té adrese vůbec někdo?

    Držené spojení je samo o sobě odpověď — nové se kvůli kontrolce
    neotevírá, aby se dekodéru neujídaly sloty.
    """
    timeout = float(args.get("timeout") or 2.0)
    host, port = args["host"], int(args["port"])
    if (host, port) in _pool:
        return {}
    _connection(host, port, timeout)
    return {}


def tcp_send(args: dict) -> dict:
    """Pošle bajty a zavře — startovka do kamery nic nevrací.

    Kamera spojení po každé zprávě zavírá sama a slotů se u ní netahá, takže
    se tu nic nedrží.
    """
    timeout = float(args.get("timeout") or 2.0)
    payload = base64.b64decode(args.get("data") or "")
    with socket.create_connection((args["host"], int(args["port"])), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)
    return {}


def tcp_exchange(args: dict) -> dict:
    """Pošle bajty a vrátí, co přišlo zpátky.

    Čte, dokud spojení `quiet` sekund mlčí nebo dokud nevyprší `timeout`.
    Kde končí odpověď, ví server — agent rámce nerozebírá.

    Jede po **drženém spojení**; když se pod rukama zavře (dekodér se
    restartoval, wifi vypadla), zkusí se jednou znovu s novým.
    """
    timeout = float(args.get("timeout") or 3.0)
    quiet = float(args.get("quiet") or 0.3)
    payload = base64.b64decode(args.get("data") or "")
    host, port = args["host"], int(args["port"])

    for attempt in (1, 2):
        sock = _connection(host, port, timeout)
        try:
            _drain(sock)
            sock.sendall(payload)
            deadline = time.monotonic() + timeout
            chunks: list[bytes] = []
            while time.monotonic() < deadline:
                sock.settimeout(min(quiet, max(0.05, deadline - time.monotonic())))
                try:
                    chunk = sock.recv(8192)
                except socket.timeout:
                    if chunks:
                        break      # chvíli ticho a něco už máme — konec odpovědi
                    continue
                if not chunk:
                    # Protějšek zavřel. Co dorazilo, platí — spojení se jen
                    # zahodí, aby se příště navázalo nové.
                    _drop((host, port))
                    if chunks:
                        break
                    raise ConnectionError("Spojení zavřel protějšek.")
                chunks.append(chunk)
            return {"data": base64.b64encode(b"".join(chunks)).decode("ascii")}
        except (OSError, TimeoutError):
            _drop((host, port))
            if attempt == 2:
                raise


def udp_discover(args: dict) -> dict:
    """Broadcast do místní sítě a sběr odpovědí — hledání dekodérů.

    Jediná věc, kterou agent dělá po UDP. Důvod je ten samý, proč vůbec
    existuje: broadcast musí vyjít **ze sítě, kde dekodéry stojí**. Server
    v datovém centru ho pošle nanejvýš svým sousedům, takže „Dohledat MAC
    adresy" tam vracelo prázdno, i když všechno ostatní přes agenta prošlo
    (nález 19. 8. 2026).

    Agent protokolu nerozumí — pošle hotové bajty a vrátí, co se ozvalo,
    včetně adresy odesílatele. Rozebrat rámce je věc serveru.

    **Naslouchá se dřív, než se pošle:** dekodér odpovídá okamžitě a odpověď
    na nenavázaný port by spadla do prázdna.
    """
    timeout = float(args.get("timeout") or 3.0)
    payload = base64.b64decode(args.get("data") or "")
    request_port = int(args.get("request_port") or 5403)
    reply_port = int(args.get("reply_port") or 5303)

    replies: list[dict] = []
    seen: set[str] = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", reply_port))
        sock.settimeout(timeout)
        sock.sendto(payload, ("255.255.255.255", request_port))

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(deadline - time.monotonic(), 0.05))
            try:
                data, sender = sock.recvfrom(4096)
            except (socket.timeout, TimeoutError):
                break
            except OSError:
                break
            host = sender[0]
            if host in seen:
                continue
            seen.add(host)
            replies.append({"host": host, "data": base64.b64encode(data).decode("ascii")})
    except OSError as exc:
        # Port 5303 může držet jiný program (MyLaps Toolkit, druhá instance).
        # Hledání se pak nekoná, ale agent kvůli tomu nepadá.
        return {"replies": replies, "error": str(exc)}
    finally:
        sock.close()
    return {"replies": replies}


ACTIONS = {
    "tcp_probe": tcp_probe,
    "tcp_send": tcp_send,
    "tcp_exchange": tcp_exchange,
    "udp_discover": udp_discover,
}


def run_command(server: Server, command: dict) -> None:
    action = ACTIONS.get(command.get("action") or "")
    if action is None:
        server.result(command.get("id"), False, error=f"Neznámý příkaz {command.get('action')!r}.")
        return
    args = command.get("args") or {}
    host, port = str(args.get("host", "")), int(args.get("port") or 0)
    try:
        data = action(args)
    except (OSError, TimeoutError, ValueError) as exc:
        # Nedostupné železo je běžný stav, ne pád agenta: server chybu ukáže
        # obsluze u rampy stejně, jako by se připojoval sám.
        _remember(host, port, False, str(exc))
        server.result(command.get("id"), False, error=str(exc))
        return
    _remember(host, port, True)
    server.result(command.get("id"), True, data=data)


# --- proud průjezdů --------------------------------------------------------
#
# Dekodér posílá průjezdy sám (v příručce P3 značka „A" — send autonomously),
# takže na ně netřeba čekat dotazem. Krabička drží spojení otevřené — stejně,
# jako ho v přímém režimu drží server — a každý rámec hned POSTne do aplikace.
# Do 19. 8. 2026 se průjezdy jen stahovaly na dotaz serveru po 1,5 s a každý
# dotaz stál dvě cesty přes internet; od smyčky k obrazovce to dělalo 2–4 s.
#
# Krabička protokolu P3 **nerozumí**: otevírací rámce (watchdog, resend od
# záložky) jí předchystá server v konfiguraci proudu a ona jen řeže příchozí
# bajty na rámce podle SOR/EOR — uvnitř rámce jsou tyhle bajty escapované,
# takže se s obsahem nespletou.

STREAM_SOR = 0x8E
STREAM_EOR = 0x8F

#: Průjezdy se posílají po skupinkách: osm jezdců projede cílem ve zlomku
#: sekundy a POST na každý zvlášť by byl osminásobný provoz. Delší čekání by
#: zdržovalo obrazovku, takže jen okamžik.
STREAM_QUIET_SECONDS = 0.3
STREAM_MAX_FRAMES = 50

#: Kolik rámců smí čekat na odeslání, když server zrovna nebere. Víc znamená
#: výpadek — zahodit a nechat server dotáhnout záložkou (od té se stahuje
#: jen to, co nedošlo).
STREAM_BACKLOG_MAX = 2000

#: Strop bufferu proudu — rámec má desítky bajtů; víc bez konce rámce je
#: rozsypaný proud, ne data (stejná pojistka jako na serveru).
STREAM_BUFFER_MAX = 256 * 1024

#: Poslední průjezdy, které server vzal — pro červenou kontrolku na displeji
#: (Davidovo zadání 20. 8. 2026: „nešlo by, aby i krabička měla červenou
#: kontrolku, když přijme průjezd?"). Krabička protokolu nerozumí, takže se
#: nepočítají rámce (dekodér posílá i status), ale `stored` z odpovědi
#: serveru — kontrolka tak svítí jen za průjezdy, které opravdu dojely.
_prujezdy_lock = threading.Lock()
#: `smycka_kdy` = kdy naposledy dorazil rámec z dekodéru (příjem ze smyčky),
#: `server_kdy` = kdy server dávku přijal. Dvě razítka, protože displej podle
#: návrhu Ri5 v2 (20. 8. 2026) ukazuje cestu průjezdu zvlášť: smyčka → server.
_prujezdy = {
    "kdy": 0.0, "celkem": 0, "ze_serveru": None, "naposledy": "",
    "smycka_kdy": 0.0, "server_kdy": 0.0,
}

#: Jak dlouho svítí dioda smyčky/serveru. Je to **puls**, ne stav: delší
#: svícení by z „právě přišel průjezd" udělalo „něco se kdysi stalo".
LED_SVITI_S = 2.2

#: Jak dlouho po průjezdu kontrolka svítí. Displej se obnovuje po 1 s; deset
#: sekund dává obsluze dost času potvrzení bezpečně zahlédnout.
PRUJEZD_SVITI_S = 10.0


def _zaznamenat_pocitadlo(celkem) -> None:
    """Počítadlo průjezdů od serveru — kontrolka svítí i u stahované cesty.

    Krabička protokolu nerozumí a když si průjezdy stahuje server sám, o nich
    vůbec neví: displej pak zůstával tmavý, i když měření běželo (Davidův
    nález 20. 8. 2026). Server posílá součet záložek dekodérů; jeho růst
    znamená průjezd. První odpověď jen založí základ, jinak by kontrolka
    blikla po každém startu agenta.
    """
    if celkem is None:
        return
    try:
        celkem = int(celkem)
    except (TypeError, ValueError):
        return
    with _prujezdy_lock:
        znamy = _prujezdy["ze_serveru"]
        _prujezdy["ze_serveru"] = celkem
        if znamy is None or celkem <= znamy:
            return
        pribylo = celkem - znamy
        _prujezdy["kdy"] = time.monotonic()
        # Stahovaná cesta: o průjezdu víme až od serveru, takže se rozsvítí
        # obě diody naráz. Jinak by u staré krabičky (1.0) zůstala levá
        # dioda navěky tmavá, i když měření běží.
        _prujezdy["smycka_kdy"] = time.monotonic()
        _prujezdy["server_kdy"] = time.monotonic()
        _prujezdy["celkem"] += pribylo
        _prujezdy["naposledy"] = time.strftime("%H:%M:%S")


def _zaznamenat_prujezdy(stored: int) -> None:
    if stored <= 0:
        return
    with _prujezdy_lock:
        _prujezdy["kdy"] = time.monotonic()
        _prujezdy["server_kdy"] = time.monotonic()
        _prujezdy["celkem"] += stored
        _prujezdy["naposledy"] = time.strftime("%H:%M:%S")
        # Aktivní POST rozsvítí displej okamžitě. Stejný průjezd se ale za
        # okamžik objeví i v serverovém počítadle záložek; posuneme proto
        # známý základ spolu s ním, aby se na displeji nezapočítal podruhé.
        if _prujezdy["ze_serveru"] is not None:
            _prujezdy["ze_serveru"] += stored


def _zaznamenat_smycku() -> None:
    """Z dekodéru přišel rámec — dioda „smyčka" se rozsvítí okamžitě.

    Krabička neví, jestli je ten rámec průjezd (P3 nezná) a vědět to nemá:
    pro obsluhu u trati je podstatné, že se smyčka **ozvala**. Že se z toho
    stal průjezd, potvrdí až server druhou diodou.
    """
    with _prujezdy_lock:
        _prujezdy["smycka_kdy"] = time.monotonic()


def _prujezdy_stav() -> dict:
    with _prujezdy_lock:
        snapshot = dict(_prujezdy)
    now = time.monotonic()

    def sviti(razitko: float, delka: float) -> bool:
        return bool(razitko) and (now - razitko) < delka

    return {
        "celkem": snapshot["celkem"],
        "cerstvy": sviti(snapshot["kdy"], PRUJEZD_SVITI_S),
        "smycka": sviti(snapshot["smycka_kdy"], LED_SVITI_S),
        "server": sviti(snapshot["server_kdy"], LED_SVITI_S),
        "naposledy": snapshot["naposledy"],
    }


def _split_frames(buffer: bytearray) -> list[bytes]:
    """Vytáhne celé rámce SOR…EOR a nedokončený zbytek nechá v bufferu."""
    frames: list[bytes] = []
    while True:
        try:
            start = buffer.index(STREAM_SOR)
        except ValueError:
            buffer.clear()
            return frames
        if start:
            del buffer[:start]
        try:
            end = buffer.index(STREAM_EOR, 1)
        except ValueError:
            return frames
        frames.append(bytes(buffer[: end + 1]))
        del buffer[: end + 1]


class StreamLink:
    """Jedno trvalé spojení na dekodér: čte proud a posílá rámce serveru."""

    def __init__(self, server: Server, config: dict):
        self.server = server
        self.config = dict(config)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"stream-{config.get('host')}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def matches(self, config: dict) -> bool:
        """Stejná smyčka a stejné otevírací rámce — spojení může běžet dál.

        Otevírací rámce se mění se záložkou serveru; přehrávají se jen při
        (re)connectu, takže běžící spojení kvůli nim netřeba trhat.
        """
        for key in ("decoder", "host", "port", "service", "service_seconds"):
            if self.config.get(key) != config.get(key):
                return False
        return True

    def _run(self) -> None:
        host = str(self.config.get("host", ""))
        port = int(self.config.get("port") or 0)
        decoder_id = str(self.config.get("decoder", ""))
        service = base64.b64decode(self.config.get("service") or b"")
        service_seconds = float(self.config.get("service_seconds") or 10)
        backoff = RECONNECT_MIN
        backlog: list[str] = []

        while not self._stop.is_set():
            try:
                with socket.create_connection((host, port), timeout=3.0) as sock:
                    sock.settimeout(1.0)
                    _remember(host, port, True, "proud průjezdů")
                    log(f"Proud {host}:{port} otevřen")
                    for encoded in self.config.get("open") or []:
                        sock.sendall(base64.b64decode(encoded))
                    backoff = RECONNECT_MIN
                    self._pump(sock, decoder_id, service, service_seconds, backlog)
            except (OSError, TimeoutError, ValueError) as exc:
                _remember(host, port, False, str(exc))
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, RECONNECT_MAX)

    def _pump(self, sock, decoder_id, service, service_seconds, backlog) -> None:
        buffer = bytearray()
        last_frame_at = time.monotonic()
        last_service_at = time.monotonic()

        while not self._stop.is_set():
            # Jakmile leží v backlogu první průjezd, nesmí nás další
            # `recv()` na celou sekundu zablokovat. Dřív se deklarované
            # 0,3s dávkovací okno kontrolovalo až po 1s socket timeoutu,
            # takže samotná krabička spotřebovala celý požadovaný limit.
            if backlog:
                quiet_left = STREAM_QUIET_SECONDS - (
                    time.monotonic() - last_frame_at
                )
                sock.settimeout(max(0.01, min(STREAM_QUIET_SECONDS, quiet_left)))
            else:
                sock.settimeout(1.0)
            try:
                chunk = sock.recv(8192)
                if not chunk:
                    raise ConnectionError("Dekodér spojení zavřel.")
                buffer.extend(chunk)
                if len(buffer) > STREAM_BUFFER_MAX:
                    log(f"Proud {decoder_id[:8]} rozsypaný — buffer se zahazuje")
                    buffer.clear()
                for raw in _split_frames(buffer):
                    backlog.append(base64.b64encode(raw).decode("ascii"))
                    last_frame_at = time.monotonic()
                    _zaznamenat_smycku()
                if len(backlog) > STREAM_BACKLOG_MAX:
                    # Server dlouho nebere — zahodit; dotáhne si to záložkou.
                    del backlog[: len(backlog) - STREAM_BACKLOG_MAX]
            except socket.timeout:
                pass

            now = time.monotonic()
            quiet = now - last_frame_at >= STREAM_QUIET_SECONDS
            if backlog and (quiet or len(backlog) >= STREAM_MAX_FRAMES):
                batch = backlog[:STREAM_MAX_FRAMES]
                try:
                    answer = self.server.push_passings(decoder_id, batch)
                except (urllib.error.URLError, OSError, TimeoutError, ValueError):
                    # Server nedostupný — dávka zůstává a zkusí se s další.
                    pass
                else:
                    # ok:false znamená „nechci" (žádný závod si proud neříká,
                    # neznámá smyčka) — držet takovou dávku nemá smysl; co by
                    # chybělo, server dotáhne záložkou, až si proud řekne.
                    del backlog[: len(batch)]
                    if answer.get("ok"):
                        _zaznamenat_prujezdy(int(answer.get("stored") or 0))
                    elif answer.get("error"):
                        log(f"Server dávku nevzal: {answer['error']}")

            if now - last_service_at >= service_seconds and service:
                sock.sendall(service)
                last_service_at = now


# --- běh na pozadí ---------------------------------------------------------


class Worker:
    """Agent běžící ve vlákně: dá se spustit, zastavit a zeptat se na stav.

    Kvůli okénku s ikonou v liště — to musí zůstat obsluze k ruce, zatímco
    agent na pozadí pracuje. Bez vlákna by se dalo jen buď dívat, nebo běžet.
    """

    def __init__(self, server_url: str, token: str, *, on_status=None):
        self.server = Server(server_url, token)
        self.on_status = on_status or (lambda text, connected: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.status = "nespuštěno"
        #: id smyčky -> běžící proud průjezdů (StreamLink)
        self._streams: dict[str, StreamLink] = {}

    # -- řízení ------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="agent", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._sync_streams([])
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._set("zastaveno", connected=False)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- vnitřek -----------------------------------------------------------

    def _set(self, text: str, *, connected: bool) -> None:
        self.status = text
        self.connected = connected
        log(text)
        try:
            self.on_status(text, connected)
        except Exception:  # noqa: BLE001 — chyba v okénku nesmí shodit agenta
            pass

    def _sync_streams(self, wanted: list[dict]) -> None:
        """Srovná běžící proudy s konfigurací ze serveru.

        Nová smyčka v konfiguraci → otevřít; zmizelá → zavřít; změněná
        adresa nebo servis → přeotevřít. Otevírací rámce (mění se se
        záložkou) běžící spojení netrhají — přehrávají se jen po výpadku.
        """
        by_id = {str(item.get("decoder", "")): item for item in wanted if item.get("decoder")}

        for decoder_id in list(self._streams):
            link = self._streams[decoder_id]
            config = by_id.get(decoder_id)
            if config is not None and link.matches(config) and link.is_alive():
                link.config = dict(config)  # čerstvé otevírací rámce pro reconnect
                continue
            link.stop()
            del self._streams[decoder_id]
            if config is None:
                log(f"Proud {decoder_id[:8]} ukončen — závod si ho už neříká")

        if self._stop.is_set():
            return
        for decoder_id, config in by_id.items():
            if decoder_id in self._streams:
                continue
            link = StreamLink(self.server, config)
            self._streams[decoder_id] = link
            link.start()

    def _run(self) -> None:
        backoff = RECONNECT_MIN
        greeted = False
        self._set(f"připojuji se na {self.server.base}", connected=False)

        while not self._stop.is_set():
            try:
                if not greeted:
                    hello = self.server.hello()
                    greeted = True
                    name = hello.get("agent")
                    organization = hello.get("organization")
                    self._set(f"připojen jako {name} ({organization})", connected=True)

                answer = self.server.poll()
                _zaznamenat_pocitadlo(answer.get("passings"))
                self._sync_streams(answer.get("stream") or [])
                for command in answer.get("commands") or []:
                    if self._stop.is_set():
                        break
                    host = (command.get("args") or {}).get("host", "")
                    log(f"Příkaz {command.get('action')} → {host}")
                    run_command(self.server, command)
                backoff = RECONNECT_MIN
            except urllib.error.HTTPError as exc:
                if exc.code == 403:
                    # Token aplikace (zatím) nezná — přesně tenhle stav má
                    # krabička po zapnutí, než ho někdo opíše do Nastavení
                    # dekodérů. Není to chyba, je to čekání.
                    greeted = False
                    self._set("čeká na schválení v aplikaci", connected=False)
                    self._stop.wait(APPROVAL_POLL_SECONDS)
                    continue
                greeted = False
                self._set(f"server odpověděl {exc.code}, zkusím to znovu", connected=False)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
            except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
                greeted = False
                self._set(f"server není k dispozici ({exc})", connected=False)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)


# --- nastavení na disku ----------------------------------------------------


def config_path() -> pathlib.Path:
    """Kde má agent uložený server a token — podle zvyklostí systému."""
    if sys.platform.startswith("win"):
        base = pathlib.Path(os.environ.get("APPDATA", pathlib.Path.home())) / "EventControlAgent"
    elif sys.platform == "darwin":
        base = pathlib.Path.home() / "Library" / "Application Support" / "EventControlAgent"
    else:
        base = pathlib.Path(
            os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")
        ) / "event-control-agent"
    return base / "config.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def configured_server(config: dict) -> str:
    """Adresa aplikace — z nastavení, jinak výchozí `DEFAULT_SERVER`.

    Prázdný řetězec v souboru znamená „nikdo nic nezadal", ne „nikam se
    nehlásit": krabička se staví pro jednu aplikaci a obsluha u trati nemá co
    opisovat adresu. Kdo chce vlastní server, přepíše ji v nastavení.
    """
    return (config.get("server") or "").strip() or DEFAULT_SERVER


def save_config(server_url: str, token: str, *, autostart: bool = False) -> None:
    """Uloží nastavení tak, aby ho nečetl kdokoli — token je heslo do sítě."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"server": server_url, "token": token, "autostart": autostart}, indent=2),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass          # Windows práva takhle neumí a nevadí to


# --- spouštění po startu počítače -----------------------------------------


def autostart_path() -> pathlib.Path:
    """Soubor, kterým se agent přihlásí ke spuštění po startu počítače."""
    if sys.platform.startswith("win"):
        return (
            pathlib.Path(os.environ.get("APPDATA", pathlib.Path.home()))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            / "EventControlAgent.cmd"
        )
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "LaunchAgents" / "cz.bikody.event-control-agent.plist"
    return (
        pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config"))
        / "autostart" / "event-control-agent.desktop"
    )


def _autostart_body(command: list[str]) -> str:
    """Obsah souboru pro automatický start — pro každý systém jeho tvar."""
    quoted = " ".join(f'"{part}"' for part in command)
    if sys.platform.startswith("win"):
        return f"@echo off\r\nstart \"\" {quoted}\r\n"
    if sys.platform == "darwin":
        args = "".join(f"        <string>{part}</string>\n" for part in command)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            "    <key>Label</key>\n    <string>cz.bikody.event-control-agent</string>\n"
            f"    <key>ProgramArguments</key>\n    <array>\n{args}    </array>\n"
            "    <key>RunAtLoad</key>\n    <true/>\n"
            "    <key>KeepAlive</key>\n    <true/>\n"
            "</dict>\n</plist>\n"
        )
    return (
        "[Desktop Entry]\nType=Application\nName=Event Control — agent u trati\n"
        f"Exec={quoted}\nX-GNOME-Autostart-enabled=true\nTerminal=false\n"
    )


SERVICE_NAME = "event-control-agent.service"


def service_paths() -> tuple[pathlib.Path, bool]:
    """Kam zapsat unit systemd a jestli je systémová. Druhá hodnota = systémová."""
    if os.geteuid() == 0:
        return pathlib.Path("/etc/systemd/system") / SERVICE_NAME, True
    base = pathlib.Path(
        os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")
    )
    return base / "systemd" / "user" / SERVICE_NAME, False


def _service_unit(command: list[str]) -> str:
    quoted = " ".join(f'"{part}"' for part in command)
    return (
        "[Unit]\n"
        "Description=Event Control — agent u trati\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={quoted}\n"
        # Krabička u trati musí vstát sama: spadlý agent znamená závod bez
        # časomíry a nikdo u trati nehlídá, jestli proces ještě žije.
        "Restart=always\n"
        "RestartSec=5\n"
        "StartLimitIntervalSec=0\n"
        f"WorkingDirectory={pathlib.Path.home()}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
        if os.geteuid() == 0
        else (
            "[Unit]\n"
            "Description=Event Control — agent u trati\n"
            "After=network-online.target\n"
            "\n"
            "[Service]\n"
            f"ExecStart={quoted}\n"
            "Restart=always\n"
            "RestartSec=5\n"
            "StartLimitIntervalSec=0\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
    )


def install_service() -> int:
    """Zapíše a zapne službu systemd, která agenta po pádu i po restartu vrátí.

    **Proč to nestačí přes „spouštět po startu"**: na Linuxu je to soubor
    v `~/.config/autostart`, tedy věc plochy — bez přihlášené grafické session
    se nespustí vůbec, a **spadlý proces nikdo nezvedne**. Krabička u trati
    přitom stojí v garáži bez klávesnice a spadlý agent znamená závod bez
    časomíry (David, 19. 8. 2026 — čtyři dny před prvním ostrým závodem).

    Bez roota se instaluje **uživatelská** služba; aby běžela i bez přihlášení,
    je potřeba `loginctl enable-linger`, což skript vypíše.
    """
    if not sys.platform.startswith("linux"):
        print(
            'Služba se instaluje jen na Linuxu (krabička u trati). '
            'Na macOS drží agenta LaunchAgent s KeepAlive, na Windows '
            'použijte volbu „Spouštět po startu počítače“ v nastavení.'
        )
        return 1

    program = pathlib.Path(sys.argv[0]).resolve()
    command = [str(program)] if getattr(sys, "frozen", False) else [sys.executable, str(program)]
    path, system = service_paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_service_unit(command), encoding="utf-8")

    scope = [] if system else ["--user"]
    steps = [
        ["systemctl", *scope, "daemon-reload"],
        ["systemctl", *scope, "enable", SERVICE_NAME],
        ["systemctl", *scope, "restart", SERVICE_NAME],
    ]
    for step in steps:
        result = subprocess.run(step, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Nepovedlo se: {' '.join(step)}\n{result.stderr.strip()}")
            return result.returncode
        print(f"OK: {' '.join(step)}")

    print(f"\nSlužba je v {path}")
    if system:
        print("Agent se spustí po startu i po pádu. Log: journalctl -u " + SERVICE_NAME + " -f")
    else:
        user = os.environ.get("USER") or "pi"
        print(
            "Agent se spustí po přihlášení a po pádu. Aby běžel i bez přihlášení:\n"
            f"    sudo loginctl enable-linger {user}\n"
            f"Log: journalctl --user -u {SERVICE_NAME} -f"
        )
    return 0


def uninstall_service() -> int:
    """Vypne a smaže službu — pro notebook, kde má agenta spouštět obsluha."""
    if not sys.platform.startswith("linux"):
        print("Služba existuje jen na Linuxu.")
        return 1
    path, system = service_paths()
    scope = [] if system else ["--user"]
    for step in (
        ["systemctl", *scope, "disable", "--now", SERVICE_NAME],
        ["systemctl", *scope, "daemon-reload"],
    ):
        subprocess.run(step, capture_output=True, text=True)
    try:
        path.unlink()
    except OSError:
        pass
    print(f"Služba odstraněna ({path}).")
    return 0


def service_state() -> str:
    """Co říká systemd o službě — pro displej krabičky. Prázdné = neinstalovaná."""
    if not sys.platform.startswith("linux"):
        return ""
    path, system = service_paths()
    if not path.exists():
        return ""
    scope = [] if system else ["--user"]
    result = subprocess.run(
        ["systemctl", *scope, "is-active", SERVICE_NAME], capture_output=True, text=True
    )
    return (result.stdout or result.stderr or "").strip()


def set_autostart(enabled: bool) -> pathlib.Path | None:
    """Přihlásí (nebo odhlásí) agenta ke spuštění po startu počítače.

    Píše se do uživatelské složky, ne do systému: u trati nikdo nemá chodit
    pro heslo správce a agent má běžet pod tím, kdo u počítače sedí.
    """
    path = autostart_path()
    if not enabled:
        try:
            path.unlink()
        except OSError:
            pass
        return None

    program = pathlib.Path(sys.argv[0]).resolve()
    command = [str(program)] if getattr(sys, "frozen", False) else [sys.executable, str(program)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_autostart_body(command), encoding="utf-8")
    if not sys.platform.startswith("win"):
        try:
            path.chmod(0o755)
        except OSError:
            pass
    return path


# --- token krabičky --------------------------------------------------------
#
# **Token si vyrábí krabička, ne server.** Na jejím displeji se ukáže a obsluha
# ho opíše do aplikace (Nastavení aplikace → Přihlásit krabičku). Obráceně by se třiačtyřicetiznakový
# řetězec opisoval na dotykovém displeji — a to nikdo nechce. Takhle se píše
# tam, kde je klávesnice.
#
# Abeceda je bez znaků, které se na obrazovce pletou (0/O, 1/I/L), a token je
# po čtveřicích: šest skupin = 120 bitů náhody. Na displeji se láme na dva
# řádky po třech čtveřicích, takže se čte pohodlně i z 3,5" panelu.

TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TOKEN_GROUPS = 6

#: Tlačítko „Nový token" na displeji jistí dvě klepnutí: nový token odpojí
#: spárovanou krabičku, takže ho nesmí vydat náhodný dotek. První klepnutí
#: odjistí, druhé musí přijít do téhle lhůty.
NOVY_TOKEN_POTVRZENI_S = 15.0
_novy_token_pozadan = 0.0
TOKEN_GROUP_LEN = 4


def generate_token() -> str:
    import secrets

    groups = [
        "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_GROUP_LEN))
        for _ in range(TOKEN_GROUPS)
    ]
    return "-".join(groups)


def normalize_token(raw: str) -> str:
    """Token z displeje — bez mezer a pomlček, velkými písmeny.

    Obsluha ho opisuje z obrazovky, takže pomlčky vynechá, přidá mezery nebo
    napíše malá písmena. Server i agent porovnávají stejně očištěný tvar.
    """
    return "".join(char for char in (raw or "").upper() if char.isalnum())


def ensure_token(config: dict) -> str:
    """Token krabičky — jednou vyrobený zůstává, dokud ho někdo nezmění."""
    token = (config.get("token") or "").strip()
    if token:
        return token
    token = generate_token()
    save_config(config.get("server", ""), token, autostart=bool(config.get("autostart")))
    log(f"Vyroben token krabičky: {token}")
    return token


# --- displej a obsluha přes prohlížeč --------------------------------------
#
# Agent nemá okno ani ikonu v liště: obojí by znamenalo knihovnu navíc pro
# každý systém zvlášť. Místo toho má **vlastní stránku**. Na krabičce u trati
# běží přes celou obrazovku a je to její displej; z notebooku se otevře přes
# síť. Nastavení (adresa aplikace) je pod ní na `/nastaveni`, aby se na hlavní
# obrazovce nedalo omylem nic přepsat.
#
# Bez závislostí: `http.server` je ve standardní knihovně, styl je vlastní.

WEB_PORT = 8088

_STYLE = """
 /* Displej krabičky podle návrhu `bikody_ri5_v2.html` (David, 20. 8. 2026).
    Návrh stojí na Tailwindu z CDN; tady je přepsaný do vlastního CSS —
    krabička u trati bývá bez internetu a stránka z CDN by se jí nenačetla
    vůbec. Rozměry jdou z `clamp()` stejně jako v návrhu, takže obrazovka
    sedne na monitor i na 3,5" SPI displej. */
 :root {{ color-scheme: dark; }}
 * {{ box-sizing: border-box; }}
 html, body {{ margin:0; width:100%; height:100%; background:#050505; overflow:hidden;
               font-family: Arial, Helvetica, sans-serif; color:#fff; }}
 main {{ width:100vw; height:100vh; padding:16px;
         background: radial-gradient(circle at 50% 20%, rgba(30,34,36,.45), transparent 35%),
                     linear-gradient(180deg, #070809 0%, #020303 100%); }}
 .ramecek {{ width:100%; height:100%; border-radius:28px; border:1px solid #27272a;
             background:rgba(0,0,0,.8); padding:16px; display:flex; flex-direction:column;
             gap:16px; }}

 /* Hlavička: značka vlevo, hodiny a datum vpravo, červená linka pod tím. */
 header {{ display:flex; align-items:center; justify-content:space-between;
           border-bottom:2px solid #dc2626; padding-bottom:12px; }}
 h1 {{ margin:0; font-weight:900; letter-spacing:.11em; line-height:1;
       font-size:clamp(2rem, 6vw, 4.5rem); }}
 h1 .tecka {{ color:#ef4444; }}
 .hodiny {{ text-align:right; }}
 .hodiny .cas {{ font-weight:900; line-height:1; font-size:clamp(1.5rem, 4vw, 3rem); }}
 .hodiny .datum {{ margin-top:4px; color:#d4d4d8; font-size:clamp(.75rem, 1.8vw, 1.2rem); }}

 /* Dvě karty na polovinu: stav serveru a tlačítko nového tokenu. */
 .dvojice {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
 .karta {{ border-radius:24px; border:2px solid {barva}; background:rgba(0,0,0,.4);
           padding:16px; display:flex; flex-direction:column; align-items:center;
           justify-content:center; min-height:145px; }}
 .karta .popisek {{ text-transform:uppercase; letter-spacing:.05em; font-weight:900;
                    font-size:clamp(1rem, 2.6vw, 1.8rem); margin:0; }}
 .vysledek {{ margin-top:12px; display:flex; align-items:center; gap:16px; }}
 .kolecko {{ width:clamp(4rem,7vw,6rem); height:clamp(4rem,7vw,6rem); border-radius:50%;
             border:5px solid {barva}; display:flex; align-items:center; justify-content:center;
             color:{barva}; font-size:clamp(2rem,4.5vw,3.5rem); font-weight:900; }}
 .slovo {{ color:{barva}; font-weight:900; line-height:1; text-shadow:0 0 10px {zare};
           font-size:clamp(2.5rem, 8vw, 6.5rem); }}
 .detail {{ margin:10px 0 0; color:#a1a1aa; font-size:clamp(.7rem,1.6vw,1rem);
            text-align:center; }}

 /* Tlačítko tokenu vypadá jako karta — na dotykovém displeji je to půlka
    obrazovky, ne malý knoflík. Odjištěné je červené a říká, co se stane. */
 .karta-tlacitko {{ border-color:#84cc16; background:rgba(132,204,22,.1); cursor:pointer;
                    font-family:inherit; color:#fff; text-align:center; transition:background .15s; }}
 .karta-tlacitko:hover {{ background:rgba(132,204,22,.2); }}
 .karta-tlacitko .ikona {{ color:#a3e635; line-height:1; font-size:clamp(2.5rem,6vw,4.5rem); }}
 .karta-tlacitko .nadpis {{ margin-top:4px; font-weight:900; letter-spacing:.03em;
                            color:#bef264; font-size:clamp(1.1rem,3vw,2.2rem); }}
 .karta-tlacitko.pozor {{ border-color:#ef4444; background:rgba(239,68,68,.14); }}
 .karta-tlacitko.pozor .ikona,
 .karta-tlacitko.pozor .nadpis {{ color:#fca5a5; }}

 /* Cesta průjezdu: smyčka → server. Dvě diody, mezi nimi šipka. */
 .cesta {{ display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:12px; }}
 .cesta .sipka {{ font-weight:900; color:#d4d4d8; font-size:clamp(2rem,5vw,4rem); }}
 .uzel {{ border-radius:20px; border:1px solid #84cc16; padding:16px; display:flex;
          align-items:center; gap:16px; min-height:105px; }}
 .uzel.server {{ border-color:#0ea5e9; }}
 .uzel .nazev {{ font-weight:900; text-transform:uppercase; letter-spacing:.1em;
                 font-size:clamp(.85rem,2vw,1.2rem); }}
 .uzel .hodnota {{ font-weight:900; color:#71717a; font-size:clamp(1rem,2.3vw,1.5rem); }}
 .uzel .hodnota.zeleno {{ color:#a3e635; }}
 .uzel .hodnota.modro {{ color:#38bdf8; }}
 .uzel .hodnota.oranzovo {{ color:#fbbf24; }}
 .uzel .pod {{ color:#a1a1aa; font-size:clamp(.65rem,1.4vw,.9rem); }}
 .dioda {{ width:28px; height:28px; border-radius:9999px; background:#24272a;
           border:2px solid #4b5563; flex:0 0 auto; transition:all .15s ease; }}
 .dioda.zelena {{ background:#67ff19; border-color:#c0ff9f;
   box-shadow:0 0 8px rgba(103,255,25,.95), 0 0 20px rgba(103,255,25,.8),
              0 0 34px rgba(103,255,25,.45); }}
 .dioda.modra {{ background:#28b7ff; border-color:#8fddff;
   box-shadow:0 0 8px rgba(40,183,255,.95), 0 0 20px rgba(40,183,255,.8),
              0 0 34px rgba(40,183,255,.45); }}
 .dioda.blik {{ animation:blik .5s ease-out; }}
 @keyframes blik {{
   0% {{ transform:scale(1); }}
   20% {{ transform:scale(1.45); filter:brightness(1.8); }}
   100% {{ transform:scale(1); }}
 }}

 /* Token: rámeček s tečkovaným rastrem a nadpisem posazeným do hrany. */
 .tokenblok {{ position:relative; border-radius:18px; border:2px solid #84cc16;
               padding:16px 20px;
               background-image: radial-gradient(rgba(98,255,30,.18) 1px, transparent 1px);
               background-size:7px 7px; }}
 .tokenblok .nadpis {{ position:absolute; top:-14px; left:50%; transform:translateX(-50%);
                       background:#050607; padding:0 14px; font-weight:900;
                       text-transform:uppercase; letter-spacing:.1em;
                       font-size:clamp(.8rem,1.8vw,1.15rem); white-space:nowrap; }}
 .tokenradek {{ display:flex; align-items:center; gap:16px; }}
 .tokenradek .zamek {{ color:#a3e635; font-size:clamp(1.6rem,3.5vw,3rem); }}
 /* Token se láme na dva řádky **po třech skupinách**, ne kdekoli: na
    3,5" displeji se celý na řádek nevejde a zlom uprostřed skupiny by se
    obsluze opisoval s chybou. Zlom nese text (\n) + `pre-line`. */
 code {{ display:block; flex:1; text-align:center; font-family: ui-monospace, "SF Mono", Menlo, monospace;
         font-weight:900; letter-spacing:.08em; color:#a3e635; white-space:pre-line;
         text-shadow:0 0 10px rgba(106,255,0,.5);
         font-size:clamp(1.1rem, 3.2vw, 2.4rem); line-height:1.15; }}

 /* Noha: poslední průjezd vlevo, čas obnovení vpravo. */
 .paticka {{ margin-top:auto; padding-top:12px; border-top:1px solid #27272a;
             display:flex; align-items:center; justify-content:space-between; gap:16px;
             color:#d4d4d8; font-size:clamp(.65rem,1.4vw,.95rem); }}
 .paticka strong {{ color:#fff; }}
 .paticka .vlevo {{ display:flex; align-items:center; gap:8px; min-width:0; }}

 /* Malé SPI displeje (MHS35: 480×320). Spodní mez clamp() je stavěná na
    monitor — tady by token, kvůli kterému displej existuje, skončil pod
    spodním okrajem. Ustupuje všechno kromě tokenu a diod. */
 @media (max-height: 420px) {{
   main {{ padding:4px; }}
   .ramecek {{ padding:6px; border-radius:14px; gap:6px; }}
   header {{ padding-bottom:4px; border-bottom-width:1px; }}
   h1 {{ font-size:1.25rem; letter-spacing:.08em; }}
   .hodiny .cas {{ font-size:1rem; }}
   .hodiny .datum {{ font-size:.58rem; margin-top:1px; }}
   .dvojice {{ gap:6px; }}
   .karta {{ min-height:0; padding:6px; border-radius:12px; border-width:1px; }}
   .karta .popisek {{ font-size:.6rem; }}
   .vysledek {{ margin-top:4px; gap:8px; }}
   .kolecko {{ width:2rem; height:2rem; border-width:2px; font-size:1rem; }}
   .slovo {{ font-size:1.7rem; }}
   .detail {{ display:none; }}
   .karta-tlacitko .ikona {{ font-size:1.5rem; }}
   .karta-tlacitko .nadpis {{ font-size:.72rem; margin-top:0; }}
   .cesta {{ gap:6px; }}
   .cesta .sipka {{ font-size:1.2rem; }}
   .uzel {{ min-height:0; padding:6px 8px; gap:8px; border-radius:12px; }}
   .uzel .nazev {{ font-size:.55rem; letter-spacing:.06em; }}
   .uzel .hodnota {{ font-size:.72rem; }}
   .uzel .pod {{ display:none; }}
   .dioda {{ width:16px; height:16px; }}
   /* Token dostane zbylé místo: na 3,5" displeji je to ta věc, kvůli které
      se na obrazovku kouká, a prázdná plocha pod ním by byla plýtvání. */
   .tokenblok {{ padding:8px 10px; border-radius:12px; flex:1;
     display:flex; align-items:center; }}
   .tokenblok .tokenradek {{ flex:1; }}
   .tokenblok .nadpis {{ top:-9px; font-size:.55rem; padding:0 8px; }}
   .tokenradek {{ gap:8px; }}
   .tokenradek .zamek {{ font-size:1rem; }}
   code {{ font-size:1.45rem; letter-spacing:.02em; }}
   .paticka {{ padding-top:5px; font-size:.55rem; }}
 }}
"""

#: Displej se **neobnovuje celou stránkou**. Návrh Ri5 v2 má živé hodiny
#: a diody, které bliknou na průjezd; při `meta refresh` po sekundě by
#: animace nikdy nedoběhla a obrazovka by problikávala. Stav se proto tahá
#: z `/stav` (JSON) a mění se jen to, co se změnilo.
_SCREEN = """<!doctype html>
<html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{nadpis}</title>
<style>{styl}</style>
</head>
<body><main><section class="ramecek">

 <header>
  <h1>BIKODY<span class="tecka">.</span>COM</h1>
  <div class="hodiny">
   <div class="cas" id="cas">{cas_hodiny}</div>
   <div class="datum" id="datum">{cas_datum}</div>
  </div>
 </header>

 <section class="dvojice">
  <div class="karta">
   <p class="popisek">Stav serveru</p>
   <div class="vysledek">
    <div class="kolecko" id="znak">{znak}</div>
    <div class="slovo" id="slovo">{slovo}</div>
   </div>
   <p class="detail" id="detail">{detail}</p>
  </div>
  {tlacitko}
 </section>

 <section class="cesta">
  <div class="uzel">
   <span class="{dioda_smycka}" id="dioda-smycka"></span>
   <div>
    <div class="nazev">Smyčka</div>
    <div class="{trida_smycka}" id="text-smycka">{text_smycka}</div>
    <div class="pod">PŘÍJEM ZE SMYČKY</div>
   </div>
  </div>
  <div class="sipka">&rarr;</div>
  <div class="uzel server">
   <span class="{dioda_server}" id="dioda-server"></span>
   <div>
    <div class="nazev">Server</div>
    <div class="{trida_server}" id="text-server">{text_server}</div>
    <div class="pod">DATA PŘEDÁNA SERVERU</div>
   </div>
  </div>
 </section>

 <section class="tokenblok">
  <div class="nadpis">{token_popisek}</div>
  <div class="tokenradek">
   <div class="zamek">&#128274;</div>
   <code id="token">{token}</code>
  </div>
 </section>

 <footer class="paticka">
  <span class="vlevo">&#8635;&nbsp;POSLEDNÍ PRŮJEZD: <strong id="posledni">{posledni}</strong></span>
  <span>AKTUALIZOVÁNO: <strong id="aktualizovano">{cas}</strong></span>
 </footer>

</section></main>
<script>
// Živý displej: hodiny tikají v prohlížeči, ostatní se tahá z /stav.
// Celá stránka se neobnovuje — animace diod by při obnovení po sekundě
// nikdy nedoběhla a token by problikával.
(function () {{
  "use strict";
  var LED_MS = 2200;

  function dvojmistne(cislo) {{ return (cislo < 10 ? "0" : "") + cislo; }}

  function tik() {{
    var now = new Date();
    var cas = dvojmistne(now.getHours()) + ":" + dvojmistne(now.getMinutes())
            + ":" + dvojmistne(now.getSeconds());
    var datum = dvojmistne(now.getDate()) + "." + dvojmistne(now.getMonth() + 1)
              + "." + now.getFullYear();
    document.getElementById("cas").textContent = cas;
    document.getElementById("datum").textContent = datum;
    return datum + " " + cas;
  }}

  var stav = {{ smycka: false, server: false }};

  function dioda(id, trida, sviti) {{
    var el = document.getElementById(id);
    if (!el) return;
    var chtene = sviti ? "dioda " + trida : "dioda";
    if (el.className.indexOf(trida) === -1 && sviti) chtene += " blik";
    el.className = chtene;
  }}

  function napis(id, text, trida) {{
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = "hodnota" + (trida ? " " + trida : "");
  }}

  function nakresli(data) {{
    dioda("dioda-smycka", "zelena", data.smycka);
    dioda("dioda-server", "modra", data.server);
    napis("text-smycka", data.smycka ? "SIGNÁL PŘIJAT" : "ČEKÁM NA PRŮJEZD",
          data.smycka ? "zeleno" : "");
    if (data.server) napis("text-server", "ODESLÁNO", "modro");
    else if (data.smycka) napis("text-server", "ODESÍLÁM…", "oranzovo");
    else napis("text-server", "PŘIPRAVEN", "");

    document.getElementById("posledni").textContent = data.posledni || "--:--:--";
    document.getElementById("znak").textContent = data.znak;
    document.getElementById("slovo").textContent = data.slovo;
    document.getElementById("detail").textContent = data.detail;
    document.getElementById("token").textContent = data.token;
    document.getElementById("aktualizovano").textContent = tik();

    // Barva stavu serveru se mění podle toho stavu, takže ji nese odpověď
    // `/stav`, ne jen styl vygenerovaný při prvním načtení stránky.
    document.querySelectorAll(".karta:first-child .kolecko, .karta:first-child .slovo")
      .forEach(function (el) {{ el.style.color = data.barva; }});
    var kolecko = document.querySelector(".karta:first-child .kolecko");
    if (kolecko) kolecko.style.borderColor = data.barva;
    var karta = document.querySelector(".karta:first-child");
    if (karta) karta.style.borderColor = data.barva;

    var tlacitko = document.getElementById("token-tlacitko");
    if (tlacitko) {{
      tlacitko.className = "karta karta-tlacitko" + (data.odjisteno ? " pozor" : "");
      var nadpis = tlacitko.querySelector(".nadpis");
      if (nadpis) {{
        nadpis.textContent = data.odjisteno
          ? "KLEPNĚTE ZNOVU — STARÝ TOKEN PŘESTANE PLATIT"
          : "NOVÝ TOKEN";
      }}
    }}
  }}

  function ptej() {{
    fetch("/stav", {{ cache: "no-store" }})
      .then(function (r) {{ return r.ok ? r.json() : null; }})
      .then(function (data) {{ if (data) nakresli(data); }})
      .catch(function () {{}});
  }}

  tik();
  window.setInterval(tik, 1000);
  ptej();
  window.setInterval(ptej, 1000);
}})();
</script>
</body></html>"""

_SETTINGS = """<!doctype html>
<html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nastavení — agent u trati</title>
<style>
 :root {{ color-scheme: dark; font-family: system-ui, sans-serif; }}
 body {{ margin:0; background:#0b0f14; color:#e6edf3; display:flex; justify-content:center; }}
 main {{ width:min(560px,92vw); padding:24px 0 40px; }}
 h1 {{ font-size:20px; margin:24px 0 4px; }}
 label {{ display:block; font-size:12px; text-transform:uppercase; letter-spacing:.08em;
          color:#8b98a5; margin:16px 0 6px; }}
 input[type=text] {{ width:100%; height:40px; padding:0 12px; border-radius:10px; box-sizing:border-box;
         border:1px solid #1e2a36; background:#0d141b; color:#e6edf3; font-size:15px; }}
 .radek {{ display:flex; align-items:center; gap:8px; margin-top:16px; font-size:14px; color:#c9d5e1; }}
 button {{ margin-top:20px; height:40px; padding:0 20px; border-radius:10px; border:0;
           background:#2f81f7; color:#fff; font-weight:600; font-size:15px; cursor:pointer; }}
 p.hint {{ color:#8b98a5; font-size:12px; line-height:1.5; }}
 code {{ background:#0d141b; padding:2px 5px; border-radius:5px; font-size:12px; }}
 table {{ width:100%; border-collapse:collapse; margin-top:24px; font-size:13px; }}
 th {{ text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.08em;
       color:#8b98a5; font-weight:600; padding:0 0 6px; }}
 td {{ padding:6px 0; border-top:1px solid #1e2a36; color:#c9d5e1; }}
 td.stavbunka {{ color:#3fb950; }} td.stavbunka.chyba {{ color:#f85149; }}
 a {{ color:#2f81f7; }}
</style></head>
<body><main>
 <h1>Agent u trati — nastavení</h1>
 <p class="hint">{stav}</p>
 <form method="post">
  <label for="server">Adresa aplikace</label>
  <input type="text" id="server" name="server" value="{server}">
  <p class="hint" style="margin:-8px 0 14px">Předvyplněno; měňte jen u vlastního serveru.</p>
  <div class="radek">
   <input type="checkbox" id="autostart" name="autostart" {autostart}>
   <label for="autostart" style="margin:0;text-transform:none;letter-spacing:0;font-size:14px">
     Spouštět po startu počítače</label>
  </div>
  <div class="radek">
   <input type="checkbox" id="novytoken" name="novytoken">
   <label for="novytoken" style="margin:0;text-transform:none;letter-spacing:0;font-size:14px">
     Vyrobit nový token (starý přestane platit)</label>
  </div>
  <button type="submit">Uložit</button>
 </form>
 <p class="hint">Token krabičky: <code>{token}</code><br>
   Opište ho v aplikaci do <strong>Nastavení aplikace → Přihlásit krabičku</strong>.
   Uložený je v <code>{config}</code>.</p>

 <h1 style="font-size:15px;margin-top:28px">Zkusit spojení na železo</h1>
 <p class="hint" style="margin-top:4px">Ověří kabel a adresu <strong>bez serveru</strong> —
   napište adresu dekodéru nebo kamery. Výsledek přibude do tabulky níž.</p>
 <form method="post" action="/zkusit">
  <label for="host">Adresa a port</label>
  <div class="radek" style="margin-top:0">
   <input type="text" id="host" name="host" value="{zkouska_host}" placeholder="192.168.9.25"
          style="flex:1">
   <input type="text" name="port" value="{zkouska_port}" placeholder="5403"
          style="width:96px">
  </div>
  <button type="submit">Zkusit</button>
 </form>

 <h1 style="font-size:15px;margin-top:28px">Služba (doporučeno pro krabičku)</h1>
 <p class="hint" style="margin-top:4px">{sluzba}</p>
 {spojeni}
 <p class="hint"><a href="/">zpět na displej</a></p>
</main></body></html>"""


#: Poslední ručně zkoušená adresa — displej ji nabídne znovu, obsluha
#: u trati nemá překlepávat IP dekodéru dvakrát.
_posledni_zkouska: dict = {"host": "", "port": ""}


def _recent_table() -> str:
    """Poslední spojení na dekodéry a kameru — co je vidět v nastavení."""
    if not _recent:
        return ""
    rows = "".join(
        "<tr><td>{cas}</td><td>{cil}</td>"
        '<td class="stavbunka{trida}">{text}</td></tr>'.format(
            cas=entry["cas"],
            cil=entry["cil"],
            trida="" if entry["ok"] else " chyba",
            text="odpovědělo" if entry["ok"] else (entry["detail"] or "neodpovědělo"),
        )
        for entry in _recent
    )
    return "<table><tr><th>Kdy</th><th>Kam</th><th>Výsledek</th></tr>" + rows + "</table>"


def _screen_state(worker, config: dict) -> dict:
    """Co má být na displeji: velký stav, a token jen dokud je k něčemu.

    **Spárovaná krabička token neukazuje.** Do té doby je to jediné, proč se
    na displej dívat; potom už je to jen klíč do klubové sítě vystavený celý
    den na obrazovce u trati. Kdo ho potřebuje znovu, najde ho v nastavení.
    """
    connected = bool(worker and worker.connected)
    token = (config.get("token") or "").strip()

    if connected:
        return {
            "barva": "#84cc16", "zare": "rgba(80,255,30,.35)",
            "znak": "✓", "slovo": "OK",
            "detail": worker.status if worker else "",
            "token_popisek": "Token krabičky:",
            "token": token[: TOKEN_GROUP_LEN] + "-…-" + token[-TOKEN_GROUP_LEN:] if token else "—",
        }
    if not configured_server(config):
        return {
            "barva": "#eab308", "zare": "rgba(234,179,8,.35)",
            "znak": "!", "slovo": "NASTAVIT",
            "detail": "V nastavení krabičky je smazaná adresa aplikace.",
            "token_popisek": "Token krabičky:", "token": token or "—",
        }
    # Červeně, ne žlutě: ČEKÁ znamená „ještě to nejede" a od zeleného OK se
    # musí lišit na první pohled i přes půlku závodiště. Žlutá zůstává
    # výjimečnému NASTAVIT.
    return {
        "barva": "#ef4444", "zare": "rgba(239,68,68,.35)",
        "znak": "…", "slovo": "ČEKÁ",
        "detail": (worker.status if worker else "čeká na schválení v aplikaci"),
        "token_popisek": "Opište token do aplikace — bez pomlček:",
        "token": token or "—",
    }


def _token_text(token: str) -> str:
    """Token na displeji: dva řádky po třech čtveřicích.

    Na jednom řádku se 24 znaků na 3,5" displej nevejde čitelně; na dvou
    unese písmo skoro dvojnásobný stupeň. Zkrácený tvar spárované krabičky
    (AKUW-…-7G59) i pomlčkami nedělený text zůstávají na jednom řádku.

    Zlom je **obyčejný „\n"**, ne `<br>`: obrazovka si stav tahá JSONem
    a text sází přes `textContent`, kde by značka byla vidět jako text.
    Zalomení kreslí CSS (`white-space: pre-line`).
    """
    groups = token.split("-")
    if len(groups) < 4:
        return token
    half = (len(groups) + 1) // 2
    return "-".join(groups[:half]) + "\n" + "-".join(groups[half:])


def _novy_token_odjisten() -> bool:
    return (time.monotonic() - _novy_token_pozadan) <= NOVY_TOKEN_POTVRZENI_S


def _tlacitko_html() -> str:
    """Tlačítko „Nový token" — v návrhu Ri5 v2 je to celá polovina obrazovky.

    **Jištění dvěma klepnutími zůstává.** Návrh má jedno tlačítko, ale nový
    token okamžitě odpojí krabičku od aplikace a obsluha ho má opsaný —
    náhodný dotek na displeji u trati by závod odstřihl od časomíry. První
    klepnutí proto tlačítko zčervená a řekne, co se stane; druhé ve lhůtě
    token vydá. Po jejím uplynutí se samo vrátí do klidu (displej se ptá
    na `/stav` každou sekundu).
    """
    odjisteno = _novy_token_odjisten()
    trida = "karta karta-tlacitko pozor" if odjisteno else "karta karta-tlacitko"
    nadpis = (
        "KLEPNĚTE ZNOVU — STARÝ TOKEN PŘESTANE PLATIT" if odjisteno else "NOVÝ TOKEN"
    )
    return (
        '<form method="post" action="/novy-token" style="display:contents">'
        f'<button type="submit" id="token-tlacitko" class="{trida}">'
        '<span class="ikona">&#8635;</span>'
        f'<span class="nadpis">{nadpis}</span>'
        "</button></form>"
    )


def _stav_json(worker, config: dict) -> bytes:
    """Stav displeje jako JSON — z něj si obrazovka bere všechno živé.

    Displej se **neobnovuje celou stránkou** (návrh Ri5 v2 má tikající hodiny
    a diody, které bliknou na průjezd; obnovení po sekundě by animaci nikdy
    nenechalo doběhnout). Odpovídá se proto malým JSONem a mění se jen to,
    co se opravdu změnilo.
    """
    import json

    state = _screen_state(worker, config)
    prujezdy = _prujezdy_stav()
    return json.dumps(
        {
            "barva": state["barva"],
            "znak": state["znak"],
            "slovo": state["slovo"],
            "detail": state["detail"],
            "token": _token_text(state["token"]),
            "token_popisek": state["token_popisek"],
            "smycka": prujezdy["smycka"],
            "server": prujezdy["server"],
            "celkem": prujezdy["celkem"],
            "posledni": prujezdy["naposledy"],
            "odjisteno": _novy_token_odjisten(),
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _render_screen(worker, config: dict) -> bytes:
    state = _screen_state(worker, config)
    style = _STYLE.format(barva=state["barva"], zare=state["zare"])
    prujezdy = _prujezdy_stav()
    now = time.localtime()
    page = _SCREEN.format(
        nadpis="BIKODY.COM — krabička u trati",
        styl=style,
        # První vykreslení nese stav diod samo: než dojde první odpověď
        # `/stav`, byla by obrazovka po restartu kiosku vždycky tmavá —
        # i uprostřed závodu, kdy průjezdy chodí.
        dioda_smycka="dioda zelena" if prujezdy["smycka"] else "dioda",
        dioda_server="dioda modra" if prujezdy["server"] else "dioda",
        text_smycka="SIGNÁL PŘIJAT" if prujezdy["smycka"] else "ČEKÁM NA PRŮJEZD",
        trida_smycka="hodnota zeleno" if prujezdy["smycka"] else "hodnota",
        text_server=(
            "ODESLÁNO" if prujezdy["server"]
            else ("ODESÍLÁM…" if prujezdy["smycka"] else "PŘIPRAVEN")
        ),
        trida_server=(
            "hodnota modro" if prujezdy["server"]
            else ("hodnota oranzovo" if prujezdy["smycka"] else "hodnota")
        ),
        znak=state["znak"],
        slovo=state["slovo"],
        detail=state["detail"],
        token_popisek=state["token_popisek"],
        token=_token_text(state["token"]),
        tlacitko=_tlacitko_html(),
        posledni=prujezdy["naposledy"] or "--:--:--",
        cas=time.strftime("%d.%m.%Y %H:%M:%S", now),
        cas_hodiny=time.strftime("%H:%M:%S", now),
        cas_datum=time.strftime("%d.%m.%Y", now),
    )
    return page.encode("utf-8")


def _service_hint() -> str:
    """Co na displeji stojí o službě — podle toho, jestli je nainstalovaná.

    Krabička u trati musí po pádu i po restartu vstát sama; „spouštět po
    startu" je na Linuxu jen soubor plochy a spadlý proces nikdo nezvedne.
    """
    if not sys.platform.startswith("linux"):
        return (
            "Na tomhle systému se služba neinstaluje — agenta drží volba "
            "„Spouštět po startu počítače“ výše."
        )
    state = service_state()
    if not state:
        return (
            "Není nainstalovaná. Agent se po pádu sám nevrátí. Na krabičce ji "
            "zapněte příkazem <code>sudo python3 track_agent.py "
            "--install-service</code> — pak vstane po pádu i po restartu."
        )
    if state == "active":
        return "Běží jako služba (<code>Restart=always</code>) — po pádu i po restartu se vrátí sama."
    return (
        f"Nainstalovaná, ale systemd hlásí <code>{state}</code>. "
        "Log: <code>journalctl -u event-control-agent.service -f</code>"
    )


def _render_settings(worker, config: dict) -> bytes:
    page = _SETTINGS.format(
        stav=(worker.status if worker else "nespuštěno"),
        server=configured_server(config),
        autostart="checked" if config.get("autostart") else "",
        token=(config.get("token") or "—"),
        config=config_path(),
        sluzba=_service_hint(),
        zkouska_host=_posledni_zkouska.get("host", ""),
        zkouska_port=_posledni_zkouska.get("port", ""),
        spojeni=_recent_table(),
    )
    return page.encode("utf-8")


def build_web_server(state: dict, *, host: str, port: int):
    """Displej a nastavení krabičky. `state` drží workera, ať jde vyměnit za chodu."""
    import http.server
    import urllib.parse

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass                       # vlastní log stačí, přístupy nikoho nezajímají

        def handle(self):
            """Klient, který odejde uprostřed odpovědi, není chyba.

            Displej krabičky si stránku obnovuje sám a prohlížeč v kiosku se
            po restartu odpojí bez rozloučení. Standardní knihovna z toho
            sype dvacetiřádkový traceback do logu — a v logu krabičky u trati
            má být vidět, co dělají dekodéry, ne tohle.
            """
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _send(self, body: bytes, status: int = 200, headers=()):
            self.send_response(status)
            # Content-Type se posílá **jednou**: `/stav` vrací JSON a dvě
            # hlavičky se stejným jménem by prohlížeč vyhodnotil podle první,
            # tedy jako HTML.
            hlavicky = list(headers)
            if not any(name.lower() == "content-type" for name, _ in hlavicky):
                hlavicky.insert(0, ("Content-Type", "text/html; charset=utf-8"))
            self.send_header("Content-Length", str(len(body)))
            for name, value in hlavicky:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):              # noqa: N802 — jméno určuje knihovna
            config = load_config()
            if self.path.startswith("/nastaveni"):
                self._send(_render_settings(state.get("worker"), config))
                return
            if self.path.startswith("/stav"):
                # Živý stav displeje. Bez cache: obrazovka se ptá po sekundě
                # a kiosk prohlížeč by jinak servíroval první odpověď pořád.
                self._send(
                    _stav_json(state.get("worker"), config),
                    headers=[
                        ("Content-Type", "application/json; charset=utf-8"),
                        ("Cache-Control", "no-store"),
                    ],
                )
                return
            self._send(_render_screen(state.get("worker"), config))

        def do_POST(self):             # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            saved = load_config()

            if self.path.startswith("/zkusit"):
                # Test spojení **bez serveru**: obsluha u trati potřebuje před
                # závodem vědět, že kabel a adresa sedí, i když je internet
                # zrovna mimo (David, 19. 8. 2026).
                host = (form.get("host", [""])[0] or "").strip()
                raw_port = (form.get("port", [""])[0] or "").strip()
                _posledni_zkouska["host"] = host
                _posledni_zkouska["port"] = raw_port
                try:
                    port = int(raw_port)
                except ValueError:
                    _remember(host or "—", 0, False, "port není číslo")
                else:
                    # `tcp_probe` výsledek nevrací — úspěch i selhání zapisuje
                    # `_remember`, takže se objeví v tabulce níž. Výjimka tady
                    # nesmí spadnout do HTTP odpovědi: displej by místo
                    # výsledku ukázal chybu serveru.
                    try:
                        tcp_probe({"host": host, "port": port, "timeout": 3.0})
                        log(f"Zkouška spojení {host}:{port} → odpovědělo")
                    except Exception as exc:  # noqa: BLE001
                        log(f"Zkouška spojení {host}:{port} → {exc}")
                self._send(b"", status=303, headers=[("Location", "/nastaveni")])
                return

            if self.path.startswith("/novy-token"):
                global _novy_token_pozadan
                if _novy_token_odjisten():
                    # Druhé klepnutí ve lhůtě — teď doopravdy.
                    _novy_token_pozadan = 0.0
                    token = generate_token()
                    save_config(
                        configured_server(saved), token,
                        autostart=bool(saved.get("autostart")),
                    )
                    log("Vydán nový token z displeje krabičky")
                    worker = state.get("worker")
                    if worker is not None:
                        worker.stop()
                        worker = Worker(configured_server(saved), token)
                        worker.start()
                        state["worker"] = worker
                else:
                    _novy_token_pozadan = time.monotonic()
                self._send(b"", status=303, headers=[("Location", "/")])
                return

            server_url = (form.get("server", [""])[0] or "").strip() or configured_server(saved)
            autostart = "autostart" in form

            # Nový token se vyrábí jen na výslovné přání: obsluha ho má
            # opsaný v aplikaci a tichá výměna by krabičku odpojila.
            token = generate_token() if "novytoken" in form else ensure_token(saved)

            save_config(server_url, token, autostart=autostart)
            set_autostart(autostart)

            worker = state.get("worker")
            if worker is not None:
                worker.stop()
            if server_url and token:
                worker = Worker(server_url, token)
                worker.start()
                state["worker"] = worker
            self._send(b"", status=303, headers=[("Location", "/nastaveni")])

    class TichyServer(http.server.ThreadingHTTPServer):
        """Odpojený klient se nehlásí jako chyba serveru.

        `handle()` v handleru pokryje běžný případ, ale výjimka umí vzniknout
        i dřív, než se handler vůbec dostane ke slovu. Log krabičky u trati má
        zůstat čitelný — je to jediné, podle čeho se u trati hledá závada.
        """

        daemon_threads = True

        def handle_error(self, request, client_address):
            if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
                return
            super().handle_error(request, client_address)

    return TichyServer((host, port), Handler)


def serve_web(state: dict, *, host: str, port: int) -> None:
    server = build_web_server(state, host=host, port=port)
    shown = host if host != "0.0.0.0" else "adresa-teto-krabicky"
    log(f"Displej krabičky: http://{shown}:{port}/")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent u trati pro Event Control")
    parser.add_argument(
        "--server",
        default=os.environ.get("EVENT_CONTROL_SERVER", ""),
        help=f"Adresa aplikace (výchozí {DEFAULT_SERVER})",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("EVENT_CONTROL_AGENT_TOKEN", ""),
        help="Token agenta z Nastavení aplikace",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Jen přeposílání, bez stránky s nastavením (služba, systemd).",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=WEB_PORT,
        help=f"Port stránky s nastavením (výchozí {WEB_PORT}).",
    )
    parser.add_argument(
        "--web-host",
        default="127.0.0.1",
        help=(
            "Na které adrese stránku nabízet. Výchozí je jen tenhle počítač; "
            "krabička u trati potřebuje 0.0.0.0, aby se na ni dalo z jiného stroje."
        ),
    )
    parser.add_argument(
        "--install-service",
        action="store_true",
        help=(
            "Zapíše a zapne službu systemd s automatickým restartem — "
            "krabička u trati po pádu i po restartu vstane sama."
        ),
    )
    parser.add_argument(
        "--uninstall-service",
        action="store_true",
        help="Službu vypne a smaže (pro notebook, kde agenta spouští obsluha).",
    )
    args = parser.parse_args(argv)

    if args.install_service:
        return install_service()
    if args.uninstall_service:
        return uninstall_service()

    saved = load_config()
    server_url = args.server or configured_server(saved)
    # Token si krabička vyrobí sama a ukáže ho na displeji; obsluha ho opíše
    # v aplikaci do Nastavení aplikace. Opačný směr by znamenal opisovat na
    # dotykovém displeji, což nikdo nechce.
    token = args.token or ensure_token(saved)

    log(f"Agent {VERSION} startuje")

    if args.headless:
        # Čistý přeposílač: nastavení přišlo z prostředí nebo ze souboru a
        # měnit se nemá. Tak běží služba na serveru.
        if not server_url or not token:
            parser.error("Chybí --server nebo --token (jde je předat i přes prostředí).")
        worker = Worker(server_url, token)
        worker.start()
        try:
            while worker.is_running():
                time.sleep(0.5)
        except KeyboardInterrupt:
            worker.stop()
            log("Konec.")
        return 0

    # Jinak se agent obsluhuje stránkou: token se vloží v prohlížeči, ne
    # přepisováním souborů. Na krabičce u trati je to jediná obsluha, kterou má.
    state: dict = {}
    if server_url and token:
        worker = Worker(server_url, token)
        worker.start()
        state["worker"] = worker
    else:
        log("Zatím není zadaná adresa aplikace — doplňte ji v nastavení krabičky.")

    try:
        serve_web(state, host=args.web_host, port=args.web_port)
    except KeyboardInterrupt:
        pass
    finally:
        if state.get("worker") is not None:
            state["worker"].stop()
    log("Konec.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
