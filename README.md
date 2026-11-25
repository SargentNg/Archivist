# scan-sorter

Watches a folder for newly-arriving scanner output (e.g. a scanner's
save-to-PDF destination) and automatically sorts each PDF into
`<output>/<source>/<type>/<filename>.pdf` based on its content.

Every scanned page is read (embedded text layer, or Tesseract OCR when the
page is image-only), classified per page, and page-boundary heuristics split
a multi-document scan into separate output files. Once a document is
complete, an LLM (OpenAI-compatible chat endpoint — llama-swap by default)
confirms its source/type and invents the final filename according to the
naming instructions in `rules.json` (source-type-date, with tax and LES
variants). Every field has a rules-only fallback, so the script works fully
offline or if the LLM is unavailable.

## Requirements

- Python 3.10+
- [PyMuPDF](https://pymupdf.readthedocs.io/) (`pymupdf` on PyPI) — PDF text/
  page manipulation
- [watchdog](https://python-watchdog.readthedocs.io/) — folder watching
  (with a built-in polling fallback if inotify isn't available)
- [Tesseract](https://github.com/tesseract-ocr/tesseract) **tesseract**
  CLI + the `eng` traineddata (required only for image-only scans; pages
  with an embedded text layer do not need it)

Setup:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Debian/Ubuntu: sudo apt install tesseract-ocr
```

## Usage

```
scan_sorter.py WATCH_DIR [options]
```

Continuous mode (default) watches the folder forever:

```sh
.venv/bin/python scan_sorter.py /path/to/scanner-outdir
```

One-shot mode — process whatever is already in the folder, then exit
(useful for testing or a cron-style sweep):

```sh
.venv/bin/python scan_sorter.py /path/to/scanner-outdir --once
```

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--watch-dir DIR` | positional arg | Folder to watch |
| `--output-dir DIR` | `output` (relative to CWD) | Where sorted documents go |
| `--rules FILE` | `rules.json` next to the script | Classification rules |
| `--dpi N` | `200` | Rendering resolution for OCR |
| `--once` | off | Process existing files, then exit |
| `--keep-originals` | off | Don't move originals to `processed/` |
| `--no-text` | off | Don't write `.txt` dumps next to outputs |
| `--leave-crud` | off | Don't move non-PDF files to `skipped/` |
| `--log-to FILE` | stderr | Also log to a file |
| `--no-llm` | off | Disable LLM classification/naming (rules only) |
| `--no-naming` | off | Use the rules-based filename even when the LLM is on |
| `--llm-url URL` | `http://127.0.0.1:8080/v1` | OpenAI-compatible base URL |
| `--llm-model MODEL` | `Qwen3.8-27B-exl3-5.00bpw-H6-V6` | Model name |
| `--llm-api-key KEY` | auto-discovered | See “LLM classification & naming” |
| `--llm-timeout SECS` | `120` | Per-call HTTP timeout |
| `--llm-max-tokens N` | `2000` | Answer budget (reasoning counts against it) |
| `--llm-think` | off | Don't request reasoning suppression (slower) |
| `--after-batch CMD` | off | Shell command run after new documents are sorted, e.g. a `rclone copy` to a shared OneDrive folder (see “Sharing output to OneDrive”) |
| `--after-batch-timeout SECS` | `600` | Max seconds for the `--after-batch` command |
| `--tag-in-name` / `--no-tag-in-name` | on | Append the `_digital` / `_scanned` / `_digital-scanned` tag to output filenames (the tag is always in the PDF metadata too) |

### What happens to files

- **Digital vs. scanned pages** (decided per page):
  - A page with a usable embedded text layer — a born-digital PDF, or a
    scan that already carries an OCR text layer — is read **verbatim and
    goes straight to the LLM**; Tesseract is never run on it.
  - A page with no text layer (paper scan straight from the scanner) is
    **OCR'd first**, then flows on to boundary detection, classification
    and the LLM.
  - Each output's log line reports which path a document took:
    `digital`, `scanned (all pages OCR)`, or `digital-scanned (n/m pages OCR)`.
- **Provenance tag.** Every output PDF is tagged as `digital`, `scanned`,
  or `digital-scanned` (both, i.e. a mixed bundle) in two places:
  - in the filename — `acme-invoice_2025-11-03_digital.pdf`,
    `utility-bill_2025-10-31_scanned.pdf` — appended by the program on top
    of the LLM/rule name (`--no-tag-in-name` to keep names bare)
  - in the PDF's own metadata: `keywords` = the tag, `title` = the filename,
    `creator` = `scan-sorter` — visible in any viewer's document properties
    and greppable (`pdftotext -v` aside, `fitz` / `pdfinfo` can read it)
- **PDFs** are processed, then the original is moved to
  `<watch-dir>/processed/` (unless `--keep-originals`).
- **Non-PDFs** are moved to `<watch-dir>/skipped/` (unless `--leave-crud`).
- Output filenames follow the naming instructions in `rules.json`
  (LLM-invented where the LLM is on, rules-derived otherwise):
  - default: `source_type_yyyy-mm-dd` → `acme-invoice_2025-10-30.pdf`
  - tax documents: `year_type_source_yyyy-mm-dd`
    → `2025_1099-nec_irs_2025-12-31.pdf`
  - LES documents: `les_yyyy-mm` → `les_2026-05.pdf`
- If the LLM returns a usable filename it wins; otherwise the deterministic
  rules-based name is used (same conventions, date found by regex). If both
  somehow fail the output is never empty — name collisions get a
  `_2`, `_3`, … suffix.
- A `.txt` with the page text is written next to each output PDF unless
  `--no-text` is given.

Files smaller than nothing / zero-byte, partially-written PDFs are ignored
until their size is stable (`--settle` window, 2 s).

## Rules file

`rules.json` is plain JSON; unknown keys are ignored. All patterns are
compiled as **case-insensitive Python regexes**.

```jsonc
{
  "sources": [            // issuers (one per output subdir of <source>/)
    { "name": "IRS", "patterns": ["\\bIRS\\b", "internal\\s+revenue\\s+service"] },
    ...
  ],
  "types": [              // document kinds (one per <type>/ subdir)
    { "name": "Invoice", "patterns": ["\\binvoice\\b"] },
    ...
  ],
  "strong_start_patterns": [ // line-anchored (uses (?m)) regexes that score
    "(?m)^\\s*(invoice|bill|statement|receipt)\\s*(#|no\\.?|number)?\\s*[:\\-]?"
  ],  // a page as a document boundary
  "continuation_markers": [  // regexes scoring a page as a continuation
    "page\\s+\\d+\\s+of\\s+\\d+"
  ]
  // (searched over the whole page),
  "default_source": "unknown",   // used when a page matches no source rule
  "default_type": "unclassified", // used when a page matches no type rule

  "naming": {                    // how each final .pdf is named
    "instructions": [            // fed to the LLM verbatim — edit freely
      "1) Default: <source>_<type>_<yyyy-mm-dd> …",
      "3) Tax documents: <tax_year>_<type>_<source>_<yyyy-mm-dd> …",
      "4) LES documents: use only <les>_<yyyy-mm> …",
      "…"
    ],
    "tax_types": ["1099-NEC", "W-2", "Tax form"],  // types (case-insensitive)
                                             // that get the tax name format
    "les_types": ["LES"],                // types that get the LES format
    "les_text_indicators": ["\\bLES\\b"] // regexes: text match also means LES
  }
}
```

`naming.instructions` is the **system-prompt text for the LLM naming step** —
edit those lines to change how filenames look (the LLM follows them verbatim;
the rules-based fallback implements the same conventions in code).

### How classification works

1. Each page's text is extracted (or OCR'ed).
2. For every source (resp. type) the page earns **+1 per pattern match**.
   Any match inside the **first ~500 characters** of the page also earns a
   **+2 heading bonus** (once per pattern) — so a keyword in the letterhead
   counts more than one buried in the body.
3. The source/type with the highest score wins; exact ties go to the rule
   listed **first**, so put more specific rules earlier.
4. Cards that match nothing use `default_source` / `default_type`.

### How document boundaries are detected

A page is treated as the **start of a new document** when its *start score*
reaches the threshold (2 points). Signals used:

- `Document N of M` in the text → page 1 starts, any other N ends the
  previous doc (very high confidence, no other scoring needed).
- `page X of Y` near the **bottom** of a page with X > 1 → continuation.
- A page whose detected *type* differs from the previous page's type →
  likely a new document.
- `continued` / `continued on next page` / `see next page` / `page X of Y`
  anywhere in the page → continuation.
- Strong start patterns in the top of the page, and a capitalized title
  line with a date, add points.

When nothing indicates a boundary, the **whole PDF is treated as one
document** (its source/type comes from the first page).

## LLM classification & naming

For each complete document (after splitting), one chat-completions call asks
the model to return exactly one JSON object:

```json
{"source": "IRS", "type": "1099-NEC", "filename": "2025_1099-nec_irs_2025-12-31.pdf"}
```

using the allowed source/type names from `rules.json` plus the naming
instructions. Calls are sequential (the local gateway is not parallel),
typically **~3 s/document** with reasoning suppression on.

**Layered fallbacks** (nothing can fail a scan):

| Field | LLM answer valid? | Otherwise |
| --- | --- | --- |
| source / type | must be an allowed `rules.json` name (or `unknown`/`unclassified`) | page heuristic from `rules.json` |
| filename | sanitized to `a-z0-9_-` + `.pdf`, ≤ 64 chars, no path | deterministic rules-based name (same conventions, date by regex) |

A failed HTTP call, timeout, missing JSON, or `finish_reason: length`
all degrade to the fallback per-field without aborting the file.

**API key discovery order** (no flag needed under a normal llama-swap setup):

1. env var `LLAMA_SWAP_API_KEY`
2. `--llm-api-key`
3. `LLAMA_SWAP_API_KEY=…` in `/home/k0k0/llm-config/secrets.env`
4. the environment of the running `llama-swap` process (`/proc/<pid>/environ`)

If none is found the script logs a warning and runs rules-only.

**Reasoning models** (Qwen3.8-27b etc.): by default the script sends
`chat_template_kwargs: {"enable_thinking": false}` for speed; the client
merges `content` **and** `reasoning_content` when parsing, so backends that
honor or ignore the flag both work. Use `--llm-think` to go back to the
model's default reasoning behavior.

## Tunables

Constants at the top of `scan_sorter.py`:

| Constant | Value | Meaning |
| --- | --- | --- |
| `SETTLE_SECONDS` | 2.0 | A file must be size-stable this long before it's processed |
| `MAX_WAIT_SECONDS` | 120.0 | Give up waiting for stability after this |
| `OCR_MIN_CHARS` | 25 | Below this many embedded chars → page is OCR'ed |
| `HEADING_CHARS` | 500 | How much of a page gets the heading bonus |
| `START_SCORE_THRESHOLD` | 2 | Start-score needed to call a page a new document |
| `POLL_INTERVAL` | 2.0 | Polling-fallback interval (seconds) |
| `LLM_DOC_CHARS` | 6000 | How much of a document's text is sent to the LLM |

## Notes / caveats

- Rule matching is heuristic: a scanner feed with an entirely new issuer
  will land in `unknown/unclassified` until you add a rule for it. Review
  the dumped `.txt` files to see what text you can key your regexes on.
- If Tesseract is missing or times out, OCR'ed pages degrade to whatever
  text is available (possibly none) and the page is unclassifiable, but the
  script **does not crash**.
- Boundary detection is best-effort. If a particular multi-document scan is
  always split incorrectly, the start/continuation rules are the place to
  tune (or call with `--keep-originals` and inspect).
- LLM calls dominate throughput on a busy scanner queue (~3 s/document with
  reasoning suppression on this machine, up to ~40 s when the model reasons
  a lot). `--no-llm` keeps the pipeline fully local; filenames stay in the
  same format either way.

## Sharing output to OneDrive

No official OneDrive client exists for Linux, so the output folder is pushed
to your OneDrive with **rclone** (Microsoft Graph). Two complementary paths:

1. **Immediate** — the program runs your `--after-batch` command the moment
   new documents are sorted, so files land in the shared folder seconds
   after scanning.
2. **Safety net** — a user systemd timer (`scan-sorter-sync.timer`) runs the
   same copy every 10 minutes, catching anything the hook missed (e.g. files
   produced before the hook existed, or a transient network blip).

### Setup (one time)

```sh
~/.local/bin/setup-onedrive.sh
```

That script logs rclone in to your personal OneDrive (follow the browser
sign-in it prints), shows your OneDrive root, and asks for the name of the
shared folder. It stores the destination in
`~/.config/scan-sorter/onedrive.env`.

### Wiring the hook into the program

Run the watcher with:

```sh
.venv/bin/python scan_sorter.py /path/to/scanner-outdir \
    --after-batch 'bash ~/.local/bin/scan-sorter-onedrive-sync.sh' \
    --log-to /path/to/scan-sorter.log
```

The hook script reads the same `onedrive.env` as the timer, so hook and
timer always push to the same folder. You can of course pass any other
shell command — e.g. a raw
`rclone copy "$SCAN_SORTER_OUTPUT_DIR" onedrive:YourFolder ...`.

The command runs through `sh -c` and receives `SCAN_SORTER_OUTPUT_DIR`,
`SCAN_SORTER_WATCH_DIR`, `SCAN_SORTER_BATCH_DOCS` and `SCAN_SORTER_SRC_FILE`
as environment variables. Failures are logged and never crash the watcher.

### How it stays safe

- `rclone copy`, never `rclone sync` — files family members add or upload
  in the shared folder are **never deleted or overwritten on their side**
  (and local moves of your own files never delete remote content).
- Output PDFs are written atomically (temp file + rename), so the hook or
  the timer never sees a half-written PDF.
- If rclone is not configured yet, the sync script exits 0 with a note, so
  the timer runs harmlessly until you complete the setup.

### Notes

- The timer is a **user** unit, so it runs while you are logged in. To have
  it survive reboots without a login session, once run
  `sudo loginctl enable-linger` (then reboot or re-login).
- If the OneDrive folder is a work/school (Microsoft 365) one, the rclone
  backend must be `m365` instead of `onedrive` — same setup, different type.