#!/usr/bin/env bash
# Z čerstvého Raspberry Pi OS udělá krabičku u trati.
#
# Jeden příkaz, žádné otázky. Co udělá:
#
#   1. doinstaluje, co chybí (cage, chromium, curl, avahi),
#   2. nastaví jméno stroje a časové pásmo,
#   3. nakopíruje agenta do /opt a zapne ho jako službu,
#   4. zapne displej (kiosk) jako službu — bez plochy a bez přihlašování,
#   5. zapne hardwarový watchdog a vypne zhasínání konzole,
#   6. volitelně předvyplní adresu aplikace a pevnou IP,
#   7. na konci zkontroluje, že obojí běží, a napíše, co zbývá.
#
# Použití:
#
#   sudo ./deploy.sh
#   sudo ./deploy.sh --server https://vas-server.cz
#   sudo ./deploy.sh --server https://vas-server.cz --hostname krabicka-brno \
#                    --static-ip 192.168.9.10/24 --gateway 192.168.9.1
#   ./deploy.sh --dry-run          # jen vypíše, co by udělal
#
# Skript se dá pouštět opakovaně — je to nastavení, ne instalace. Token
# krabičky se **nikdy nepřepisuje**: obsluha ho má opsaný v aplikaci.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=/opt/event-control-agent
AGENT_SERVICE=event-control-agent
KIOSK_SERVICE=event-control-kiosk
TIMEZONE_DEFAULT="Europe/Prague"

SERVER=""
HOSTNAME_NEW=""
TIMEZONE="$TIMEZONE_DEFAULT"
STATIC_IP=""
GATEWAY=""
DNS="1.1.1.1"
WITH_KIOSK=1
DRY_RUN=0

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --server)     SERVER="${2:-}"; shift 2 ;;
        --hostname)   HOSTNAME_NEW="${2:-}"; shift 2 ;;
        --timezone)   TIMEZONE="${2:-}"; shift 2 ;;
        --static-ip)  STATIC_IP="${2:-}"; shift 2 ;;
        --gateway)    GATEWAY="${2:-}"; shift 2 ;;
        --dns)        DNS="${2:-}"; shift 2 ;;
        --no-kiosk)   WITH_KIOSK=0; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -h|--help)    usage 0 ;;
        *) echo "Neznámý přepínač: $1" >&2; usage 1 ;;
    esac
done

# --- pomůcky ---------------------------------------------------------------

krok()  { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }
info()  { printf '  %s\n' "$*"; }
varuj() { printf '  \033[33m! %s\033[0m\n' "$*"; }

# Každý zásah do systému jde přes `spust`, takže `--dry-run` je opravdu
# nanečisto — a zároveň je z výpisu vidět, co přesně se s Pi stalo.
spust() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  \033[2m$ %s\033[0m\n' "$*"
        return 0
    fi
    "$@"
}

zapis() {
    local cesta="$1" obsah="$2"
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  \033[2m$ zapsat %s (%d B)\033[0m\n' "$cesta" "${#obsah}"
        return 0
    fi
    printf '%s' "$obsah" > "$cesta"
}

if [[ $DRY_RUN -eq 0 && $EUID -ne 0 ]]; then
    echo "Spusťte přes sudo: sudo $0 $*" >&2
    exit 1
fi

if [[ $DRY_RUN -eq 0 && ! -f /etc/rpi-issue && ! -f /boot/firmware/config.txt ]]; then
    varuj "Tohle nevypadá na Raspberry Pi OS — pokračuji, ale watchdog a"
    varuj "vypnutí zhasínání konzole se nejspíš nepovedou."
fi

# Uživatel, pod kterým poběží displej. Kiosk potřebuje mít kde mít svůj
# runtime adresář, takže běží jako konkrétní člověk, ne jako root.
DESKTOP_USER="${SUDO_USER:-${USER:-pi}}"
[[ "$DESKTOP_USER" == "root" ]] && DESKTOP_USER="pi"

echo "Event Control — nastavení krabičky u trati"
[[ $DRY_RUN -eq 1 ]] && echo "(nanečisto — nic se nemění)"

# --- 1. balíčky ------------------------------------------------------------

krok "Balíčky"
CHYBI=()
command -v curl >/dev/null || CHYBI+=(curl)
command -v python3 >/dev/null || CHYBI+=(python3)
if [[ $WITH_KIOSK -eq 1 ]]; then
    command -v cage >/dev/null || CHYBI+=(cage)
    command -v chromium-browser >/dev/null || command -v chromium >/dev/null || CHYBI+=(chromium-browser)
fi
command -v avahi-daemon >/dev/null || CHYBI+=(avahi-daemon)

if [[ ${#CHYBI[@]} -gt 0 ]]; then
    info "doinstaluji: ${CHYBI[*]}"
    spust apt-get update -qq
    spust apt-get install -y -qq "${CHYBI[@]}"
else
    info "všechno je nainstalované"
fi

# --- 2. jméno stroje a čas -------------------------------------------------

krok "Jméno stroje a časové pásmo"
if [[ -n "$HOSTNAME_NEW" ]]; then
    spust hostnamectl set-hostname "$HOSTNAME_NEW"
    info "jméno: $HOSTNAME_NEW (dostupná jako $HOSTNAME_NEW.local)"
else
    info "jméno ponecháno: $(hostname)"
fi
spust timedatectl set-timezone "$TIMEZONE"
info "časové pásmo: $TIMEZONE"

# --- 3. agent --------------------------------------------------------------

krok "Agent"
spust install -d -m 755 "$INSTALL_DIR"
spust install -m 755 "$ROOT/agent/track_agent.py" "$INSTALL_DIR/track_agent.py"
spust install -m 755 "$ROOT/kiosk/start-kiosk.sh" "$INSTALL_DIR/start-kiosk.sh"
spust install -m 644 "$ROOT/systemd/$AGENT_SERVICE.service" "/etc/systemd/system/$AGENT_SERVICE.service"

# Adresa aplikace se dá předvyplnit, aby krabička po zapnutí rovnou ukázala
# token a nemuselo se na ní nic nastavovat. Token se nikdy nepřepisuje —
# obsluha ho má opsaný v aplikaci a tichá výměna by krabičku odpojila.
if [[ -n "$SERVER" ]]; then
    if [[ -f "$INSTALL_DIR/config.json" ]]; then
        info "adresa aplikace: $SERVER (token zůstává)"
        spust python3 - "$INSTALL_DIR/config.json" "$SERVER" <<'PY'
import json, sys
cesta, server = sys.argv[1], sys.argv[2]
data = json.load(open(cesta))
data["server"] = server
json.dump(data, open(cesta, "w"), indent=2)
PY
    else
        info "adresa aplikace: $SERVER (token si krabička vyrobí sama)"
        zapis "$INSTALL_DIR/config.json" "$(printf '{\n  "server": "%s",\n  "token": "",\n  "autostart": false\n}\n' "$SERVER")"
        spust chmod 600 "$INSTALL_DIR/config.json"
    fi
fi

spust systemctl daemon-reload
spust systemctl enable --now "$AGENT_SERVICE"
info "služba $AGENT_SERVICE zapnuta"

# --- 4. displej ------------------------------------------------------------

if [[ $WITH_KIOSK -eq 1 ]]; then
    krok "Displej (kiosk)"
    spust install -m 644 "$ROOT/systemd/$KIOSK_SERVICE.service" \
        "/etc/systemd/system/$KIOSK_SERVICE@.service"
    spust systemctl daemon-reload
    spust systemctl enable --now "$KIOSK_SERVICE@$DESKTOP_USER"
    spust loginctl enable-linger "$DESKTOP_USER"
    info "běží pod uživatelem $DESKTOP_USER, bez plochy a bez přihlašování"
else
    krok "Displej přeskočen (--no-kiosk)"
fi

# --- 5. krabička, která se o sebe stará ------------------------------------

krok "Odolnost"
if [[ ! -f /boot/firmware/cmdline.txt ]]; then
    varuj "/boot/firmware/cmdline.txt neexistuje — zhasínání konzole neřeším"
elif grep -q "consoleblank=0" /boot/firmware/cmdline.txt; then
    info "zhasínání konzole už je vypnuté"
else
    spust sed -i '1 s/$/ consoleblank=0/' /boot/firmware/cmdline.txt
    info "vypnuto zhasínání konzole (po restartu)"
fi

if [[ ! -f /boot/firmware/config.txt ]]; then
    varuj "/boot/firmware/config.txt neexistuje — watchdog neřeším"
elif grep -q "^dtparam=watchdog=on" /boot/firmware/config.txt; then
    info "watchdog už je zapnutý"
else
    spust bash -c 'echo "dtparam=watchdog=on" >> /boot/firmware/config.txt'
    info "zapnut hardwarový watchdog (po restartu)"
fi

spust install -d -m 755 /etc/systemd/system.conf.d
zapis /etc/systemd/system.conf.d/event-control-watchdog.conf \
"# Krabička u trati běží bez obsluhy: když zamrzne, restartuje se sama.
[Manager]
RuntimeWatchdogSec=30
RebootWatchdogSec=2min
"

# Log na SD kartě neroste donekonečna — karta se u trati vytahuje ze zásuvky
# a plný disk by krabičku položil dřív než cokoli jiného.
spust install -d -m 755 /etc/systemd/journald.conf.d
zapis /etc/systemd/journald.conf.d/event-control.conf \
"[Journal]
SystemMaxUse=50M
"
info "log omezen na 50 MB, watchdog na 30 s"

# --- 6. síť ----------------------------------------------------------------

if [[ -n "$STATIC_IP" ]]; then
    krok "Pevná adresa"
    if [[ -z "$GATEWAY" ]]; then
        echo "K --static-ip patří i --gateway." >&2
        exit 1
    fi
    spust "$ROOT/scripts/set-static-ip.sh" "$STATIC_IP" "$GATEWAY" "$DNS"
fi

# --- 7. kontrola -----------------------------------------------------------

krok "Kontrola"
SLUZBY=("$AGENT_SERVICE")
[[ $WITH_KIOSK -eq 1 ]] && SLUZBY+=("$KIOSK_SERVICE@$DESKTOP_USER")

if [[ $DRY_RUN -eq 1 ]]; then
    info "nanečisto — služby se nespouštěly"
else
    for SLUZBA in "${SLUZBY[@]}"; do
        if systemctl is-active --quiet "$SLUZBA"; then
            info "$SLUZBA běží"
        else
            varuj "$SLUZBA neběží — podívejte se: journalctl -u $SLUZBA -n 30"
        fi
    done
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
NAZEV="$(hostname)"
cat <<INFO

Hotovo.

  Displej krabičky:  http://${IP:-<ip-krabicky>}:8088/   (nebo http://$NAZEV.local:8088/)
  Nastavení:         http://${IP:-<ip-krabicky>}:8088/nastaveni

Po zapnutí zdroje najede agent i displej samy, bez přihlašování.

Zbývá:
INFO
if [[ -z "$SERVER" ]]; then
    echo "  1) v nastavení krabičky vyplnit adresu aplikace,"
    echo "  2) token z displeje opsat v aplikaci: Nastavení aplikace → Přihlásit krabičku."
else
    echo "  1) token z displeje opsat v aplikaci: Nastavení aplikace → Přihlásit krabičku."
fi
echo
for SLUZBA in "${SLUZBY[@]}"; do
    echo "  stav:  systemctl status $SLUZBA"
done
cat <<INFO
  log:   journalctl -u $AGENT_SERVICE -f

Watchdog a vypnuté zhasínání se projeví po restartu: sudo reboot
INFO
