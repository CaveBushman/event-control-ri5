#!/usr/bin/env bash
# Displej krabičky: stránka agenta přes celou obrazovku, bez lišt a bez kurzoru.
#
# Čeká na agenta, ne na pevný počet sekund — po startu Pi se služba rozjíždí
# různě dlouho a chybová stránka prohlížeče vypadá jako rozbitá krabička.
set -euo pipefail

URL="http://127.0.0.1:8088/"

for _ in $(seq 1 60); do
    if curl -fsS --max-time 1 "$URL" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Obrazovka u trati nemá zhasínat ani usínat — obsluha se na ni dívá zběžně
# a probouzet ji dotykem znamená hledat, kde vůbec je.
xset s off -dpms || true
command -v unclutter >/dev/null && unclutter -idle 0.5 -root &

BROWSER="$(command -v chromium-browser || command -v chromium)"

# Prohlížeč se u trati nikdy neaktualizuje ani nezavírá sám — ale kdyby přesto
# spadl, černá obrazovka vypadá jako rozbitá krabička. Proto smyčka: agent
# běží dál pod systemd, tohle jen znovu ukáže jeho stránku.
while true; do
  "$BROWSER" \
    --kiosk \
    --incognito \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --check-for-update-interval=31536000 \
    "$URL" || true
  sleep 3
done
