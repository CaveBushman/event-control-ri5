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
import sys
import threading
import time
import urllib.error
import urllib.request

VERSION = "1.0"

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
        answer = self._request("/bmx/api/agent/commands/", timeout=READ_TIMEOUT)
        return answer.get("commands") or []

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


ACTIONS = {
    "tcp_probe": tcp_probe,
    "tcp_send": tcp_send,
    "tcp_exchange": tcp_exchange,
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

    # -- řízení ------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="agent", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
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

                for command in self.server.commands():
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
 :root {{ color-scheme: dark; }}
 * {{ box-sizing: border-box; }}
 html, body {{ margin:0; height:100%; background:#050505; overflow:hidden;
               font-family: system-ui, Arial, Helvetica, sans-serif; color:#fff; }}
 main {{ height:100%; display:flex; align-items:center; justify-content:center; padding:12px;
         background: radial-gradient(circle at 50% 30%, rgba(38,38,38,.28), transparent 45%),
                     linear-gradient(180deg, #090b0c 0%, #050607 100%); }}
 .ramecek {{ width:100%; height:100%; max-width:900px; max-height:520px; border-radius:28px;
             border:1px solid rgba(63,63,70,.6); background:rgba(0,0,0,.8); padding:16px;
             box-shadow:0 25px 50px -12px rgba(0,0,0,.6); }}
 .vnitrek {{ height:100%; border-radius:20px; border:1px solid #27272a; background:rgba(0,0,0,.4);
             padding:16px 24px; display:flex; flex-direction:column; }}
 header {{ text-align:center; border-bottom:1px solid #27272a; padding-bottom:14px; }}
 h1 {{ margin:0; font-weight:900; letter-spacing:.14em; line-height:1;
       font-size:clamp(2rem, 7vw, 4.6rem); }}
 h1 .tecka {{ color:#ef4444; }}
 .stav {{ flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center;
          border-bottom:1px solid #27272a; padding:14px 0; }}
 .popisek {{ text-transform:uppercase; letter-spacing:.08em; color:#e4e4e7; font-weight:600;
             font-size:clamp(1rem, 2.5vw, 1.8rem); margin:0; }}
 .vysledek {{ margin-top:12px; display:flex; align-items:center; gap:20px; }}
 .kolecko {{ width:clamp(5rem,9vw,7rem); height:clamp(5rem,9vw,7rem); border-radius:50%;
             border:5px solid {barva}; display:flex; align-items:center; justify-content:center;
             color:{barva}; font-size:clamp(2.5rem,6vw,4rem); font-weight:900;
             text-shadow:0 0 10px {zare}; }}
 .slovo {{ color:{barva}; font-weight:900; line-height:1; text-shadow:0 0 10px {zare};
           font-size:clamp(2.5rem, 8vw, 7rem); }}
 .detail {{ margin:10px 0 0; color:#a1a1aa; font-size:clamp(.8rem,1.8vw,1.05rem); text-align:center; }}
 .tokenblok {{ padding-top:14px; }}
 .tokenramecek {{ margin-top:10px; border-radius:16px; border:2px solid rgba(132,204,22,.8);
                  padding:14px 18px; text-align:center;
                  background-image: radial-gradient(rgba(87,255,36,.16) 1px, transparent 1px);
                  background-size:7px 7px; }}
 code {{ display:block; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-weight:700;
         letter-spacing:.09em; color:#84cc16; text-shadow:0 0 8px rgba(80,255,30,.25);
         font-size:clamp(1.5rem, 5vw, 3.4rem); }}
 .paticka {{ margin-top:14px; display:flex; align-items:center; justify-content:center; gap:8px;
             color:#a1a1aa; font-size:clamp(.7rem,1.7vw,1rem); }}
 .paticka a {{ color:#a1a1aa; }}

 /* Malé SPI displeje (MHS35: 480×320). Spodní mez clamp() je stavěná na
    monitor — tady by token, kvůli kterému displej existuje, skončil pod
    spodním okrajem. Nadpis a nadpisky ustoupí, token zůstává největší. */
 @media (max-height: 420px) {{
   main {{ padding:4px; }}
   .ramecek {{ padding:5px; border-radius:14px; }}
   .vnitrek {{ padding:4px 10px; border-radius:10px; }}
   header {{ padding-bottom:4px; }}
   h1 {{ font-size:1.15rem; letter-spacing:.1em; }}
   .stav {{ padding:4px 0; }}
   .popisek {{ font-size:.7rem; }}
   .vysledek {{ margin-top:4px; gap:12px; }}
   .kolecko {{ width:2.6rem; height:2.6rem; border-width:3px; font-size:1.3rem; }}
   .slovo {{ font-size:2.1rem; }}
   .detail {{ margin-top:4px; font-size:.62rem; }}
   .tokenblok {{ padding-top:5px; }}
   .tokenramecek {{ margin-top:4px; padding:6px 8px; border-radius:10px; }}
   code {{ font-size:2.3rem; letter-spacing:.03em; }}
   .paticka {{ margin-top:5px; font-size:.58rem; }}
 }}
"""

_SCREEN = """<!doctype html>
<html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{nadpis}</title>
<style>{styl}</style>
<meta http-equiv="refresh" content="5">
</head>
<body><main><section class="ramecek"><div class="vnitrek">
 <header><h1>BIKODY<span class="tecka">.</span>COM</h1></header>

 <section class="stav">
  <p class="popisek">Stav serveru:</p>
  <div class="vysledek">
   <div class="kolecko">{znak}</div>
   <div class="slovo">{slovo}</div>
  </div>
  <p class="detail">{detail}</p>
 </section>

 <section class="tokenblok">
  <p class="popisek" style="text-align:center">{token_popisek}</p>
  <div class="tokenramecek"><code>{token}</code></div>
  <p class="paticka">AKTUALIZOVÁNO: {cas}</p>
 </section>
</div></section></main></body></html>"""

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
 {spojeni}
 <p class="hint"><a href="/">zpět na displej</a></p>
</main></body></html>"""


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
    return {
        "barva": "#eab308", "zare": "rgba(234,179,8,.35)",
        "znak": "…", "slovo": "ČEKÁ",
        "detail": (worker.status if worker else "čeká na schválení v aplikaci"),
        "token_popisek": "Opište token do aplikace:",
        "token": token or "—",
    }


def _token_html(token: str) -> str:
    """Token na displeji: dva řádky po třech čtveřicích.

    Na jednom řádku se 24 znaků na 3,5" displej nevejde čitelně; na dvou
    unese písmo skoro dvojnásobný stupeň. Zkrácený tvar spárované krabičky
    (AKUW-…-7G59) i pomlčkami nedělený text zůstávají na jednom řádku.
    """
    groups = token.split("-")
    if len(groups) < 4:
        return token
    half = (len(groups) + 1) // 2
    return "-".join(groups[:half]) + "<br>" + "-".join(groups[half:])


def _render_screen(worker, config: dict) -> bytes:
    state = _screen_state(worker, config)
    style = _STYLE.format(barva=state["barva"], zare=state["zare"])
    page = _SCREEN.format(
        nadpis="BIKODY.COM — krabička u trati",
        styl=style,
        znak=state["znak"],
        slovo=state["slovo"],
        detail=state["detail"],
        token_popisek=state["token_popisek"],
        token=_token_html(state["token"]),
        cas=time.strftime("%d.%m.%Y %H:%M:%S"),
    )
    return page.encode("utf-8")


def _render_settings(worker, config: dict) -> bytes:
    page = _SETTINGS.format(
        stav=(worker.status if worker else "nespuštěno"),
        server=configured_server(config),
        autostart="checked" if config.get("autostart") else "",
        token=(config.get("token") or "—"),
        config=config_path(),
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
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):              # noqa: N802 — jméno určuje knihovna
            config = load_config()
            if self.path.startswith("/nastaveni"):
                self._send(_render_settings(state.get("worker"), config))
                return
            self._send(_render_screen(state.get("worker"), config))

        def do_POST(self):             # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            saved = load_config()
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
    args = parser.parse_args(argv)

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
