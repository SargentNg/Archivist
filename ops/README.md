# Ops: systemd user units & helper scripts

Everything the Archivist needs to run unattended on the desktop host.
All units are **user** units (`systemctl --user ...`); with
`loginctl enable-linger <user>` they also run at boot without a login.

## Components

| Path | Role |
| ---- | ---- |
| `systemd/archivist.service` | The pipeline itself: `archivist.py watch` with the OneDrive push wired in as `--after-batch`. `Restart=always` + `RestartSec=10`: any crash — including the pipeline's own stall self-termination (see archivist.md / `STALL_*_SECONDS`) — is relaunched. |
| `systemd/archivist-scanfeed.{service,timer}` | Every ~30 s, drains the router's SMB share (the HP scanner's save-to-PDF destination) into `watch/`. |
| `systemd/archivist-sync.{service,timer}` | Every 10 min, re-runs the OneDrive sync as a backstop, catching anything a per-file push missed (network blip, pre-hook backlog). |
| `scripts/archivist-scanfeed.sh` | One feed pass: `rclone move` from the `scanner:` remote into `watch/`, skipping files modified within a settle window (`ARCHIVIST_SCAN_SETTLE`, default 60 s) so half-written scanner PDFs are never pulled. |
| `scripts/archivist-onedrive-sync.sh` | Flattens `output/**/*.pdf` into a staging dir, then a plain `rclone copy --ignore-existing` into the shared folder (default `onedrive:Family Vault/Scans`). **PDFs only** — OCR sidecars stay in the local `output/`. |

## Install (one time)

```sh
# 1. the shared pipeline directory
cd ~/opencode/Archivist
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. rclone remotes (see rclone.conf.example below for the two needed)
#    - scanner:  SMB share on the router = \\<router>\HPScan, user/pass
#    - onedrive: personal OneDrive (browser OAuth)
#    fill in ~/.config/archivist/onedrive.env:
#      ARCHIVIST_ONEDRIVE_DEST="onedrive:Family Vault/Scans"
#      ARCHIVIST_SYNC_SRC=/home/<user>/opencode/Archivist/output

# 3. LLM endpoint (optional — every field has a rules-only fallback)
#    default: http://127.0.0.1:8080/v1, or /home/<user>/llm-config/secrets.env

# 4. scripts + units
install -m 755 scripts/archivist-scanfeed.sh scripts/archivist-onedrive-sync.sh ~/.local/bin/
install -m 644 systemd/archivist*.service systemd/archivist*.timer ~/.config/systemd/user/
loginctl enable-linger <user>
systemctl --user daemon-reload
systemctl --user enable --now archivist-scanfeed.timer
systemctl --user enable --now archivist-sync.timer
systemctl --user enable --now archivist.service
```

Status: `systemctl --user status archivist` · log: `~/opencode/Archivist/archivist.log`

## rclone.conf remotes

```ini
[scanner]
type = smb
host = <router-ip>
user = <share-user>
pass = <share-pass>            # or: rclone config password scanner pass <clear>
share = HPScan

[onedrive]
type = onedrive
client_id = <from `rclone config create` OAuth flow>
client_secret =
token = <auto-filled by the OAuth flow>
```

## Data flow

```
HP scanner --save-to-PDF--> \\<router>\HPScan
        --scanfeed.timer (rclone move, 30s)--> watch/
        --archivist.service (OCR, split, LLM name)--> output/<Source>/<Type>/
        --after-batch per document + sync.timer (10min)--> OneDrive, FLAT
        originals (untouched) -----------------------------> watch/processed/
```

## Policies

- **Flat shared folder.** The family folder receives documents directly in
  its root; the `Source/Type` structure is local-only. The sync script
  stages a flattened copy because rclone's `--files-from` cannot remap
  nested local paths onto a flat remote (entries resolve relative to the
  source).
- **`rclone copy`, never `rclone sync`.** The script can only *add* files
  to the shared folder — content family members add or edit is never
  deleted or overwritten.
- **Name collisions on the flat folder.** `unique_path` suffixes are
  per-directory, so two documents in different `Source/Type` folders can
  share a basename. In that case the first file pushed wins
  (`--ignore-existing`), the duplicate stays in the local `output/` tree
  (still findable by its full path), and the script logs a warning.
- **Self-healing.** The watcher re-polls `watch/` every 5 s regardless of
  whether inotify is live; a file still unprocessed after 60 s is taken
  over by the poller. Oldest-queued age > 10 min logs a warning, > 20 min
  exits non-zero so systemd restarts the service.