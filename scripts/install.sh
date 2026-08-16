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

echo "▶ Displej (kiosk) pro uživatele $DESKTOP_USER"
install -m 755 "$ROOT/kiosk/start-kiosk.sh" "$INSTALL_DIR/start-kiosk.sh"

MISSING=()
command -v cage >/dev/null || MISSING+=(cage)
command -v chromium-browser >/dev/null || command -v chromium >/dev/null || MISSING+=(chromium-browser)
if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "  doinstaluji: ${MISSING[*]}"
    apt-get update -qq && apt-get install -y -qq "${MISSING[@]}"
fi

# Kiosk běží jako služba **konkrétního uživatele** (šablona `@`), aby měl
# `cage` kde mít svůj runtime adresář. Přihlašovat se nikdo nemusí.
install -m 644 "$ROOT/systemd/event-control-kiosk.service" \
    "/etc/systemd/system/event-control-kiosk@.service"
systemctl daemon-reload
systemctl enable --now "event-control-kiosk@$DESKTOP_USER"

# Aby se `cage` po odhlášení nezavřel a session přežila zavření terminálu.
loginctl enable-linger "$DESKTOP_USER" 2>/dev/null || true

# Konzole nemá zhasínat — pod kioskem není vidět, ale při pádu prohlížeče ano.
if [[ -f /boot/firmware/cmdline.txt ]] && ! grep -q "consoleblank=0" /boot/firmware/cmdline.txt; then
    sed -i '1 s/$/ consoleblank=0/' /boot/firmware/cmdline.txt
    echo "  vypnuto zhasínání konzole (projeví se po restartu)"
fi

# Zamrzlou krabičku u trati nemá kdo rozebírat: hardwarový watchdog ji do
# minuty restartuje sám.
if [[ -f /boot/firmware/config.txt ]] && ! grep -q "^dtparam=watchdog=on" /boot/firmware/config.txt; then
    echo "dtparam=watchdog=on" >> /boot/firmware/config.txt
    echo "  zapnut hardwarový watchdog (projeví se po restartu)"
fi
install -d -m 755 /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/event-control-watchdog.conf <<'WD'
# Krabička u trati běží bez obsluhy: když zamrzne, restartuje se sama.
[Manager]
RuntimeWatchdogSec=30
RebootWatchdogSec=2min
WD

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
