#!/usr/bin/env bash
# Instalace krabičky u trati na Raspberry Pi OS.
#
# Dělá čtyři věci a nic víc: nakopíruje agenta, zapne ho jako službu, nastaví
# displej na kiosk se stránkou agenta a vypne zhasínání obrazovky. Nastavení
# (adresa serveru a token) se zadává až na té stránce — ne tady, aby se token
# nemusel psát do skriptu ani do historie příkazů.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/event-control-agent
SERVICE=event-control-agent

if [[ $EUID -ne 0 ]]; then
    echo "Spusťte přes sudo: sudo $0" >&2
    exit 1
fi

# Uživatel, pod kterým běží plocha — pod ním poběží i kiosk.
DESKTOP_USER="${SUDO_USER:-pi}"
DESKTOP_HOME="$(getent passwd "$DESKTOP_USER" | cut -d: -f6)"

echo "▶ Agent do $INSTALL_DIR"
install -d -m 755 "$INSTALL_DIR"
install -m 755 "$ROOT/agent/track_agent.py" "$INSTALL_DIR/track_agent.py"

echo "▶ Služba $SERVICE"
install -m 644 "$ROOT/systemd/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable --now "$SERVICE"

if [[ -d "$DESKTOP_HOME" ]]; then
    echo "▶ Kiosk pro uživatele $DESKTOP_USER"
    install -d -m 755 -o "$DESKTOP_USER" -g "$DESKTOP_USER" "$DESKTOP_HOME/.config/autostart"
    install -m 644 -o "$DESKTOP_USER" -g "$DESKTOP_USER" \
        "$ROOT/kiosk/event-control-kiosk.desktop" \
        "$DESKTOP_HOME/.config/autostart/event-control-kiosk.desktop"
    install -m 755 "$ROOT/kiosk/start-kiosk.sh" "$INSTALL_DIR/start-kiosk.sh"

    if ! command -v chromium-browser >/dev/null && ! command -v chromium >/dev/null; then
        echo "  chromium není nainstalovaný — doinstaluji"
        apt-get update -qq && apt-get install -y -qq chromium-browser unclutter
    fi
else
    echo "▶ Kiosk přeskočen (uživatel $DESKTOP_USER nemá domovskou složku)"
fi

IP="$(hostname -I | awk '{print $1}')"
cat <<INFO

Hotovo.

  Stránka krabičky:  http://${IP:-<ip-krabicky>}:8088/
  Na displeji se otevře sama po restartu.

Zbývá poslední krok — na té stránce vyplňte adresu aplikace a token
z Nastavení dekodérů. Do té doby agent nikam nevolá.

  stav:     systemctl status $SERVICE
  log:      journalctl -u $SERVICE -f
INFO
