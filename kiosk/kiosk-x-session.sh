#!/usr/bin/env bash
# X sezení kiosku: vypnout šetřič obrazovky a spustit prohlížeč.
#
# Volá ho `xinit` ze start-kiosk.sh — celý příkaz prohlížeče přijde
# v argumentech, aby seznam přepínačů žil na jednom místě.
set -euo pipefail
xset s off 2>/dev/null || true
xset -dpms 2>/dev/null || true
exec "$@"
