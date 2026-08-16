#!/usr/bin/env python3
"""Agent u trati — drát mezi aplikací v cloudu a železem v klubové síti.

Dekodéry MyLaps i cílová kamera mají privátní adresy (`192.168.x.y`), na které
se server aplikace z datového centra nedostane. Tenhle program běží na počítači
u trati, **sám se hlásí serveru** a dělá to, oč si řekne: připojí se na
zadanou adresu, pošle bajty a vrátí, co přišlo zpátky.

Ven jde jen odchozí HTTPS, takže se na routeru pořadatele nic neotevírá.

Spuštění:

    python3 tools/track_agent.py --server https://vas-server.cz --token <token>

Token vydá aplikace v Nastavení dekodérů. Dá se místo přepínačů vzít
i z prostředí (`EVENT_CONTROL_SERVER`, `EVENT_CONTROL_AGENT_TOKEN`).

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

#: Server drží dotaz otevřený, dokud nemá co poslat. Čtecí timeout musí být
#: delší, jinak by agent spojení trhal těsně před odpovědí.
READ_TIMEOUT = 40.0

#: Po výpadku sítě se zkouší dál, jen pomaleji — u trati se běžně přepojuje
#: kabel nebo přepíná wifi a agent to má přežít bez zásahu obsluhy.
RECONNECT_MIN = 1.0
RECONNECT_MAX = 15.0


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
    try:
        data = action(command.get("args") or {})
    except (OSError, TimeoutError, ValueError) as exc:
        # Nedostupné železo je běžný stav, ne pád agenta: server chybu ukáže
        # obsluze u rampy stejně, jako by se připojoval sám.
        server.result(command.get("id"), False, error=str(exc))
        return
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
                    self._set("server token odmítl — zkontrolujte ho v aplikaci", connected=False)
                    return
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


# --- obsluha přes prohlížeč ------------------------------------------------
#
# Agent nemá okno ani ikonu v liště: obojí by znamenalo knihovnu navíc pro
# každý systém zvlášť a na krabičce u trati (Raspberry Pi) by stejně nebylo
# komu se dívat. Místo toho má **vlastní stránku**. Na Pi se otevře na jeho
# displeji, na notebooku na `localhost` — a je to tatáž stránka, takže se
# nastavení dělá na obou místech stejně.
#
# Bez závislostí: `http.server` je ve standardní knihovně.

WEB_PORT = 8088

_PAGE = """<!doctype html>
<html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent u trati</title>
<style>
 :root {{ color-scheme: dark; font-family: system-ui, sans-serif; }}
 body {{ margin:0; background:#0b0f14; color:#e6edf3; display:flex; justify-content:center; }}
 main {{ width:min(560px, 92vw); padding:24px 0 40px; }}
 h1 {{ font-size:20px; margin:24px 0 4px; }}
 .stav {{ display:flex; align-items:center; gap:10px; background:#111820; border:1px solid #1e2a36;
          border-radius:12px; padding:14px 16px; margin:16px 0; }}
 .tecka {{ width:10px; height:10px; border-radius:50%; background:{muted}; flex:none; }}
 .tecka.ok {{ background:#3fb950; }}
 label {{ display:block; font-size:12px; text-transform:uppercase; letter-spacing:.08em;
          color:#8b98a5; margin:16px 0 6px; }}
 input[type=text] {{ width:100%; box-sizing:border-box; height:40px; padding:0 12px; border-radius:10px;
         border:1px solid #1e2a36; background:#0d141b; color:#e6edf3; font-size:15px; }}
 .radek {{ display:flex; align-items:center; gap:8px; margin-top:16px; font-size:14px; color:#c9d5e1; }}
 button {{ margin-top:20px; height:40px; padding:0 20px; border-radius:10px; border:0;
           background:#2f81f7; color:#fff; font-weight:600; font-size:15px; cursor:pointer; }}
 p.hint {{ color:#8b98a5; font-size:12px; line-height:1.5; }}
 code {{ background:#0d141b; padding:2px 5px; border-radius:5px; font-size:12px; }}
</style></head>
<body><main>
 <h1>Agent u trati</h1>
 <p class="hint">Přeposílá dotazy aplikace na dekodéry a cílovou kameru v téhle síti.</p>
 <div class="stav"><span class="tecka{ok}"></span><span>{status}</span></div>
 <form method="post">
  <label for="server">Adresa aplikace</label>
  <input type="text" id="server" name="server" value="{server}" placeholder="https://vas-server.cz">
  <label for="token">Token z Nastavení dekodérů</label>
  <input type="text" id="token" name="token" value="" placeholder="{token_hint}">
  <div class="radek">
   <input type="checkbox" id="autostart" name="autostart" {autostart}>
   <label for="autostart" style="margin:0; text-transform:none; letter-spacing:0; font-size:14px;">
     Spouštět po startu počítače</label>
  </div>
  <button type="submit">Uložit a připojit</button>
 </form>
 <p class="hint">Token se ukládá do <code>{config}</code> a na stránce se už nezobrazuje —
   nový se vydává v aplikaci, v Nastavení dekodérů.</p>
</main>
<script>setTimeout(function () {{ location.reload(); }}, 5000);</script>
</body></html>"""


def _render_page(worker, config: dict) -> bytes:
    token = config.get("token") or ""
    page = _PAGE.format(
        muted="#6e7681",
        ok=" ok" if (worker and worker.connected) else "",
        status=(worker.status if worker else "nenastaveno — doplňte adresu a token"),
        server=(config.get("server") or ""),
        token_hint=("uložený token: …" + token[-4:]) if token else "vložte token z aplikace",
        autostart="checked" if config.get("autostart") else "",
        config=config_path(),
    )
    return page.encode("utf-8")


def build_web_server(state: dict, *, host: str, port: int):
    """Stránka agenta. `state` drží běžícího workera, ať jde vyměnit za chodu."""
    import http.server
    import urllib.parse

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass                       # vlastní log stačí, přístupy nikoho nezajímají

        def _send(self, body: bytes, status: int = 200, headers=()):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):              # noqa: N802 — jméno určuje knihovna
            self._send(_render_page(state.get("worker"), load_config()))

        def do_POST(self):             # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            saved = load_config()
            server_url = (form.get("server", [""])[0] or "").strip() or saved.get("server", "")
            # Prázdné pole tokenu znamená „nech ten uložený" — na stránce se
            # nezobrazuje, takže by ho jinak každé uložení smazalo.
            token = (form.get("token", [""])[0] or "").strip() or saved.get("token", "")
            autostart = "autostart" in form

            save_config(server_url, token, autostart=autostart)
            set_autostart(autostart)

            worker = state.get("worker")
            if worker is not None:
                worker.stop()
            if server_url and token:
                worker = Worker(server_url, token)
                worker.start()
                state["worker"] = worker
            self._send(b"", status=303, headers=[("Location", "/")])

    return http.server.ThreadingHTTPServer((host, port), Handler)


def serve_web(state: dict, *, host: str, port: int) -> None:
    server = build_web_server(state, host=host, port=port)
    shown = host if host != "0.0.0.0" else "adresa-teto-krabicky"
    log(f"Nastavení agenta: http://{shown}:{port}/")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent u trati pro Event Control")
    parser.add_argument(
        "--server",
        default=os.environ.get("EVENT_CONTROL_SERVER", ""),
        help="Adresa aplikace, např. https://vas-server.cz",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("EVENT_CONTROL_AGENT_TOKEN", ""),
        help="Token agenta z Nastavení dekodérů",
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
    server_url = args.server or saved.get("server", "")
    token = args.token or saved.get("token", "")

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
        log("Zatím není zadaný server ani token — otevřete stránku nastavení.")

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
