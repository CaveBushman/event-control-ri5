#!/usr/bin/env bash
# Pevná adresa krabičky (Raspberry Pi OS Bookworm, NetworkManager).
#
# Není povinná: spojení navazuje krabička vždycky směrem ven. Hodí se jen
# proto, aby se na její stránku dalo z notebooku trefit pořád stejnou adresou.
# Jednodušší je rezervace v DHCP na routeru — tam se nedá překlepnout maska.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Spusťte přes sudo: sudo $0 192.168.9.10/24 192.168.9.1" >&2
    exit 1
fi
if [[ $# -lt 2 ]]; then
    echo "Použití: sudo $0 <adresa/maska> <brána> [dns]" >&2
    echo "Příklad: sudo $0 192.168.9.10/24 192.168.9.1 1.1.1.1" >&2
    exit 1
fi

ADDRESS="$1"; GATEWAY="$2"; DNS="${3:-1.1.1.1}"
CONNECTION="$(nmcli -t -f NAME,DEVICE connection show --active | grep -v ':lo$' | head -1 | cut -d: -f1)"

if [[ -z "$CONNECTION" ]]; then
    echo "Nenašel jsem aktivní připojení. Zapojte kabel nebo připojte wifi." >&2
    exit 1
fi

echo "▶ Nastavuji $CONNECTION na $ADDRESS (brána $GATEWAY, DNS $DNS)"
nmcli connection modify "$CONNECTION" \
    ipv4.method manual ipv4.addresses "$ADDRESS" ipv4.gateway "$GATEWAY" ipv4.dns "$DNS"
nmcli connection up "$CONNECTION"
echo "▶ Hotovo. Stránka krabičky: http://${ADDRESS%%/*}:8088/"
