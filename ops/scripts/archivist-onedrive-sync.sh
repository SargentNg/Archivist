#!/usr/bin/env bash
# Archivist output -> OneDrive shared folder (FLAT).
#
# The family archive is a flat collection: every document sits directly in
# "Family Vault/Scans/" with no per-source/per-type subfolders (that
# structure is kept only in the local output/ tree).
#
# How: files are flattened into a staging dir, then a plain `rclone copy`
# of that flat dir. (rclone's --files-from entries are resolved RELATIVE
# to the source, so it can't turn nested local paths into a flat remote —
# staging sidesteps the quirk entirely.)
#
# Safety: `rclone copy --ignore-existing` only ever ADDS new files. It
# never deletes or overwrites anything already in the shared folder, so
# files family members add/upload are never touched.
#
# The family folder receives PDFs only (the pre-existing archive is 100%
# PDFs). The PDFs themselves carry the OCR text as an invisible layer,
# so they are text-searchable; no companion files are produced.
set -u

RCLONE="$HOME/.local/bin/rclone"
CONF="$HOME/.config/rclone/rclone.conf"
ENVFILE="$HOME/.config/archivist/onedrive.env"
# sourced when run by hand / via --after-batch; systemd's EnvironmentFile
# provides the same values for the timer, so the effective destination is
# identical both ways
[ -f "$ENVFILE" ] && . "$ENVFILE"
SRC="${ARCHIVIST_SYNC_SRC:-$HOME/opencode/Archivist/output}"
DEST="${ARCHIVIST_ONEDRIVE_DEST:-onedrive:Family Vault/Scans}"
STAGE="${ARCHIVIST_SYNC_STAGE:-$HOME/.cache/archivist/staging}"

if [ ! -f "$CONF" ]; then
    echo "rclone not configured yet ($CONF missing) — skipping sync"
    exit 0
fi
if [ ! -d "$SRC" ]; then
    echo "source dir missing: $SRC — skipping"
    exit 0
fi

# flatten (first file wins on basename collisions; duplicates stay in the
# local output/ tree where their Source/Type path still keeps them apart)
rm -rf "$STAGE"
mkdir -p "$STAGE"
cd "$SRC" || exit 1
flat=0
dupes=""
while IFS= read -r -d '' f; do
    b="$(basename "$f")"
    if [ -e "$STAGE/$b" ]; then
        dupes="$dupes $b"
        continue
    fi
    cp "$f" "$STAGE/$b" && flat=$((flat + 1))
done < <(find . -type f -name '*.pdf' -print0)
if [ -n "$dupes" ]; then
    echo "warning: duplicate basenames (kept first only): $dupes" >&2
fi
if [ "$flat" -eq 0 ]; then
    echo "nothing to sync"
    exit 0
fi

"$RCLONE" copy "$STAGE" "$DEST" \
    --transfers 4 --checkers 8 \
    --ignore-existing \
    --log-level INFO
rc=$?
if [ "$rc" -eq 0 ]; then
    rm -rf "$STAGE"
else
    echo "sync exited $rc — staging kept at $STAGE for inspection" >&2
fi
exit "$rc"