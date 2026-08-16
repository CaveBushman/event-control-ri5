#!/usr/bin/env bash
# Displej krabičky: stránka agenta přes celou obrazovku, bez lišt a kurzoru.
#
# Čeká na agenta, ne na pevný počet sekund — po zapnutí se služba rozjíždí
# různě dlouho a chybová stránka prohlížeče vypadá jako rozbitá krabička.
set -euo pipefail

URL="${EVENT_CONTROL_KIOSK_URL:-http://127.0.0.1:8088/}"

for _ in $(seq 1 90); do
    if curl -fsS --max-time 1 "$URL" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Obrazovka u trati nemá zhasínat: obsluha se na ni dívá zběžně a probouzet ji
# dotykem znamená hledat, kde vůbec je.
setterm --blank 0 --powerdown 0 >/dev/tty1 2>/dev/null || true

BROWSER="$(command -v chromium-browser || command -v chromium)"
FLAGS=(
    --kiosk
    --incognito
    --noerrdialogs
    --disable-infobars
    --disable-session-crashed-bubble
    --disable-features=Translate
    --check-for-update-interval=31536000
    --ozone-platform=wayland
)

# Uvnitř plochy se prohlížeč pouští **přímo**. `cage` je taky kompozitor
# a dva kompozitory na jedné obrazovce se perou: displej bliká, jak systemd
# každé tři vteřiny zvedá ten, který právě prohrál.
if [[ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]]; then
    exec "$BROWSER" "${FLAGS[@]}" "$URL"
fi

# Bez plochy si obrazovku vezme `cage`: minimální kompozitor, který spustí
# prohlížeč na holé obrazovce, takže se po zapnutí nemusí nikdo přihlašovat.
if command -v cage >/dev/null; then
    exec cage -d -- "$BROWSER" "${FLAGS[@]}" "$URL"
fi

exec "$BROWSER" "${FLAGS[@]}" "$URL"
