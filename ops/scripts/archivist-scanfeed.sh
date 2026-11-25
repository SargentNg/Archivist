#!/usr/bin/env bash
# Router SMB share (\\RT-BE96U-EC70\HPScan) -> Archivist watch dir.
# Drains (moves) each settled scan file so it is processed exactly once.
# Settling delay (--min-age) keeps half-written scanner PDFs out of the
# pipeline until the scanner has stopped touching them.
set -u

RCLONE="$HOME/.local/bin/rclone"
CONF="$HOME/.config/rclone/rclone.conf"
SRC="scanner:HPScan"
DST="$HOME/opencode/Archivist/watch"
# how long a file must sit untouched on the share before it is pulled
SETTLE="${ARCHIVIST_SCAN_SETTLE:-60s}"

if [ ! -f "$CONF" ]; then
    echo "rclone not configured yet ($CONF missing) — skipping scan feed"
    exit 0
fi
if ! grep -q '^\[scanner\]' "$CONF" 2>/dev/null; then
    echo "SMB remote 'scanner' not configured yet — skipping scan feed"
    exit 0
fi
mkdir -p "$DST"

exec "$RCLONE" move "$SRC" "$DST" \
    --min-age "$SETTLE" \
    --transfers 4 --checkers 8 \
    --log-level INFO