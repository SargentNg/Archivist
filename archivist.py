#!/usr/bin/env python3
"""
archivist.py — watch a folder for scanner output and sort PDFs into
<output>/<source>/<type>/ automatically.

How it works
------------
1. Watches a folder (inotify via watchdog, polling fallback).
2. Once a file is stable (size unchanged) and looks like a PDF:
   - extracts the text layer of each page (pdftotext-style), falling back to
     Tesseract OCR when the page has no usable embedded text;
   - classifies each page into a *source* (issuer) and *type* (kind of
     document) using the regex rules in the rules file;
   - detects where multi-document scans change document (page-boundary
     heuristics, incl. "Document N of M" markers);
   - for every complete document, asks the LLM (OpenAI-compatible chat
     endpoint, on by default) to confirm source/type and invent a filename
     per the naming instructions in rules.json; rules-only fallbacks cover
     every field if the LLM is off or fails. Names follow
     <source>_<type>_<yyyy-mm-dd>, tax docs use
     <year>_<type>_<source>_<date>, LES docs use <les>_<yyyy-mm>;
   - writes each document as its own PDF into
     <output>/<source>/<type>/<name>.pdf  (plus a .txt with the extracted
     text, unless --no-text);
   - moves the original to <watch-dir>/processed/ (unless --keep-originals).
3. Non-PDF files are moved to <watch-dir>/skipped/ (or left alone with
   --leave-crud).

Usage
-----
    python3 archivist.py /path/to/scanner-outdir [options]

Options:
    --watch-dir DIR    folder to watch (positional also accepted)
    --output-dir DIR   where to sort (default: ./output)
    --rules FILE       rules JSON (default: rules.json next to this script)
    --dpi N            OCR render resolution (default 200)
    --once             process existing files, then exit (no watching)
    --keep-originals   don't move originals to processed/
    --no-text          don't write .txt text dumps next to sorted PDFs
    --leave-crud       don't move non-PDF files to skipped/
    --log-to FILE      also write logs to this file
    --after-batch CMD  shell command run after new docs are sorted (env:
                       ARCHIVIST_OUTPUT_DIR, ARCHIVIST_WATCH_DIR,
                       ARCHIVIST_BATCH_DOCS, ARCHIVIST_SRC_FILE)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

try:
    import pymupdf
except ImportError:
    sys.exit("pymupdf is required:  pip install pymupdf")

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    HAVE_WATCHDOG = True
except ImportError:
    HAVE_WATCHDOG = False  # fall back to polling

log = logging.getLogger("archivist")

# --------------------------------------------------------------------------
# tunables
# --------------------------------------------------------------------------
SETTLE_SECONDS = 2.0        # how long a file's size must be stable
MAX_WAIT_SECONDS = 120.0    # give up waiting for a file to finish appearing
OCR_MIN_CHARS = 25          # fewer than this embedded chars on a page -> OCR
HEADING_CHARS = 500         # "strong start" patterns only count near the top
START_SCORE_THRESHOLD = 2   # score needed for a page to begin a new document
POLL_INTERVAL = 2.0         # fallback watcher interval
POLL_GRACE_SECONDS = 60.0   # a file may sit unprocessed this long before the
                            # poll loop takes it over (observer fast path first)
STALL_WARN_SECONDS = 600.0  # log a warning when the oldest queued file is this old
STALL_FATAL_SECONDS = 1200.0  # exit non-zero when stuck this long (supervisor restarts)
LLM_DOC_CHARS = 6000        # how much of a document's text is sent to the LLM

UNCLASSIFIED = "unclassified"
UNKNOWN = "unknown"

DEFAULT_LLM_URL = "http://127.0.0.1:8080/v1"
DEFAULT_LLM_MODEL = "Qwen3.8-27B-exl3-5.00bpw-H6-V6"
LLAMA_SWAP_SECRETS = "/home/k0k0/llm-config/secrets.env"


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------
@dataclass
class Rule:
    name: str
    patterns: list[re.Pattern] = field(default_factory=list)


def load_rules(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    def compile_rules(key: str) -> list[Rule]:
        out = []
        for item in data.get(key, []):
            out.append(Rule(
                name=str(item["name"]),
                patterns=[re.compile(p, re.IGNORECASE) for p in item.get("patterns", [])],
            ))
        return out

    rules = {
        "sources": compile_rules("sources"),
        "types": compile_rules("types"),
        "start_patterns": [re.compile(p, re.IGNORECASE) for p in data.get("strong_start_patterns", [])],
        "continuation_markers": [re.compile(p, re.IGNORECASE) for p in data.get("continuation_markers", [])],
        "default_source": data.get("default_source", UNKNOWN),
        "default_type": data.get("default_type", UNCLASSIFIED),
        "naming": data.get("naming", {}),  # raw: LLM instructions + deterministic naming config
    }
    return rules


def score_matches(text: str, rule: Rule, heading_weight: int = 2) -> int:
    """Occurrence-based score; hits near the top of a page weigh more."""
    score = 0
    for pat in rule.patterns:
        hits = list(pat.finditer(text))
        if not hits:
            continue
        score += len(hits)
        for h in hits:
            if h.start() < HEADING_CHARS:
                score += heading_weight
                break  # one heading bonus per pattern
    return score


def classify_page(text: str, rules: dict) -> tuple[str, str]:
    """Return (source, type) for a page's text."""
    source, best_s = rules["default_source"], 0
    for rule in rules["sources"]:
        s = score_matches(text, rule)
        if s > best_s:
            source, best_s = rule.name, s

    dtype, best_t = rules["default_type"], 0
    for rule in rules["types"]:
        s = score_matches(text, rule)
        if s > best_t:
            dtype, best_t = rule.name, s
    return source, dtype


# --------------------------------------------------------------------------
# page text (embedded text + OCR fallback)
# --------------------------------------------------------------------------
def ocr_page(page: "pymupdf.Page", dpi: int) -> str:
    pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72))
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        pix.save(tmp.name)
        res = subprocess_run_tesseract(tmp.name)
        return res
    finally:
        os.unlink(tmp.name)


def subprocess_run_tesseract(png_path: str) -> str:
    import subprocess
    try:
        out = subprocess.run(
            ["tesseract", png_path, "stdout", "--psm", "6", "-l", "eng"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        return out.stdout or ""
    except FileNotFoundError:
        log.warning("tesseract not found on PATH; OCR disabled for this page")
        return ""
    except subprocess.TimeoutExpired:
        log.warning("tesseract timed out")
        return ""


@dataclass
class Page:
    index: int              # 0-based
    text: str
    ocr: bool = False
    source: str = field(default="")
    type: str = field(default="")


def extract_pages(pdf_path: str, dpi: int, rules: dict) -> list[Page]:
    doc = pymupdf.open(pdf_path)
    pages: list[Page] = []
    try:
        for i, page in enumerate(doc):
            text = page.get_text("text").strip()
            ocr_used = False
            if len(text) < OCR_MIN_CHARS:
                # image-only page (paper scan from the scanner): OCR first,
                # then the text goes on to everything else (boundary
                # detection, classification, LLM ingest)
                text = ocr_page(page, dpi).strip()
                ocr_used = bool(text)
            else:
                # digital page: the embedded text layer is used as-is,
                # no OCR, straight through to the document/LLM stages
                log.debug("page %d: digital (%d embedded chars), no OCR",
                          i + 1, len(text))
            p = Page(index=i, text=text, ocr=ocr_used)
            if ocr_used:
                log.debug("page %d: scanned, OCR'd (%d chars)", i + 1, len(text))
            p.source, p.type = classify_page(text, rules)
            pages.append(p)
    finally:
        doc.close()
    return pages


def document_tag(pages: "list[Page]", page_idxs: "list[int]") -> str:
    """Compact provenance tag: digital | scanned | digital-scanned."""
    scanned = sum(1 for i in page_idxs if pages[i].ocr)
    total = len(page_idxs)
    if scanned == 0:
        return "digital"
    if scanned == total:
        return "scanned"
    return "digital-scanned"


def document_kind(pages: "list[Page]", page_idxs: "list[int]") -> str:
    """Log label: where this document's text came from."""
    tag = document_tag(pages, page_idxs)
    if tag == "digital":
        return "digital"
    if tag == "scanned":
        return "scanned (all pages OCR)"
    n = sum(1 for i in page_idxs if pages[i].ocr)
    return f"digital-scanned ({n}/{len(page_idxs)} pages OCR)"


# --------------------------------------------------------------------------
# document boundary detection
# --------------------------------------------------------------------------
PAGE_OF_TOTAL = re.compile(r"\bpage\s+(\d+)\s+of\s+(\d+)\b", re.IGNORECASE)
DOC_OF_TOTAL = re.compile(r"\bdocument\s+(\d+)\s+of\s+(\d+)\b", re.IGNORECASE)
CONTINUED_FOOTER = re.compile(r"\(?\bcontinued\b\)?", re.IGNORECASE)


def start_score(page: Page, prev: Page, rules: dict) -> int:
    """Positive score = this page probably starts a new document."""
    score = 0
    head = page.text[:HEADING_CHARS]
    tail = page.text[-200:]

    # explicit scanner/office minders
    m = DOC_OF_TOTAL.search(page.text)
    if m:
        return 99 if int(m.group(1)) == 1 else -99

    # "page 3 of 5" at the tail is a continuation, first page is not
    m = PAGE_OF_TOTAL.search(tail)
    if m and int(m.group(1)) > 1:
        score -= 4

    # repeated running header: same detected type as previous page
    if prev.type and prev.type != rules["default_type"] and page.type == prev.type:
        score -= 3
    elif (prev.type and page.type and page.type != rules["default_type"]
          and page.type != prev.type):
        score += 3

    # explicit continuation wording
    if CONTINUED_FOOTER.search(tail) or any(p.search(page.text) for p in rules["continuation_markers"]):
        score -= 4

    # strong start signals near the top (form numbers, INVOICE, DATE, etc.)
    for pat in rules["start_patterns"]:
        if pat.search(head):
            score += 3
            break  # one strong signal is enough to boost

    # fresh issuer header + date pair is a decent start hint
    if re.match(r"^\s*[A-Z][A-Za-z0-9'&.\-\s]{2,40}\s*\n", page.text) and re.search(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}", page.text[:300]
    ):
        score += 1

    return score


def find_documents(pages: list[Page], rules: dict) -> list[list[int]]:
    """Return lists of page indices; each list is one logical document."""
    if not pages:
        return []
    starts = [0]
    for i in range(1, len(pages)):
        s = start_score(pages[i], pages[i - 1], rules)
        if s >= START_SCORE_THRESHOLD:
            starts.append(i)
    docs = []
    for a, b in zip(starts, starts[1:] + [len(pages)]):
        docs.append(list(range(a, b)))
    return docs


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------
def sanitize(name: str) -> str:
    name = re.sub(r"[^\w\-.]+", "_", name.strip(), flags=re.UNICODE)
    return name or "_"


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem}_{n}{ext}"):
        n += 1
    return f"{stem}_{n}{ext}"


def write_document(pdf_src: str, page_idxs: list[int], out_path: str,
                   tag: str = "") -> str:
    """Extract selected pages into a new PDF; returns the written path.

    `tag` (digital | scanned | digital-scanned) is stamped into the PDF's
    metadata (keywords) so provenance travels with the file.
    """
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        dir=os.path.dirname(out_path) or ".", suffix=".tmp", delete=False)
    tmp.close()
    src = pymupdf.open(pdf_src)
    dst = pymupdf.open()
    try:
        dst.insert_pdf(src, from_page=page_idxs[0], to_page=page_idxs[-1])
        if tag:
            dst.set_metadata({
                "title": os.path.splitext(os.path.basename(out_path))[0],
                "keywords": tag,
                "creator": "archivist",
            })
        dst.save(tmp.name)
    finally:
        src.close()
        dst.close()
    os.replace(tmp.name, out_path)  # atomic: readers never see a partial PDF
    return out_path


def write_text_dump(txt: str, pdf_path: str) -> None:
    tmp = pdf_path + ".txt.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(txt)
    os.replace(tmp, pdf_path + ".txt")


# --------------------------------------------------------------------------
# LLM ingest: classify + name each document via a chat-completions endpoint
# --------------------------------------------------------------------------
@dataclass
class LlmSettings:
    enabled: bool = False
    naming: bool = True
    base_url: str = DEFAULT_LLM_URL
    model: str = DEFAULT_LLM_MODEL
    api_key: str = ""
    key_source: str = ""
    timeout: float = 120.0
    max_tokens: int = 2000
    think: bool = False   # False = ask the backend to suppress reasoning (faster)


def _key_value_in(path: str, key: str) -> str:
    """Read KEY=value from a dotenv-style file or a /proc/<pid>/environ."""
    try:
        if path.startswith("/proc/"):
            with open(path, "rb") as f:
                for tok in f.read().split(b"\0"):
                    if tok.startswith(key.encode() + b"="):
                        return tok.split(b"=", 1)[1].decode()
            return ""
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def find_api_key(flag_value: "str | None") -> "tuple[str, str]":
    """API key discovery: env var -> CLI flag -> secrets file -> llama-swap's own env."""
    k = os.environ.get("LLAMA_SWAP_API_KEY")
    if k:
        return k, "env LLAMA_SWAP_API_KEY"
    if flag_value:
        return flag_value, "--llm-api-key"
    k = _key_value_in(LLAMA_SWAP_SECRETS, "LLAMA_SWAP_API_KEY")
    if k:
        return k, LLAMA_SWAP_SECRETS
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-f", "llama-swap"],
                             capture_output=True, text=True, timeout=5).stdout
        for pid in out.split():
            k = _key_value_in(f"/proc/{pid}/environ", "LLAMA_SWAP_API_KEY")
            if k:
                return k, f"/proc/{pid}/environ (llama-swap)"
    except Exception:
        pass
    return "", ""


def llm_chat(llm: LlmSettings, system_prompt: str, user_prompt: str) -> "tuple[str, str]":
    """One chat completion. Returns (answer_text, finish_reason).

    Reasoning models may leave `content` null and put the answer in
    `reasoning_content`, so both fields are read and merged.
    """
    import urllib.request
    body = {
        "model": llm.model,
        "temperature": 0,
        "max_tokens": llm.max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if not llm.think:
        # suppress the reasoning pass where the backend supports it; when
        # unsupported the answer may arrive in reasoning_content instead of
        # content, which llm_chat() handles either way
        body["chat_template_kwargs"] = {"enable_thinking": False}
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        llm.base_url.rstrip("/") + "/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {llm.api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=llm.timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    choice = data["choices"][0]
    msg = choice.get("message") or {}
    parts = [p for p in (msg.get("content"), msg.get("reasoning_content"))
             if p and str(p).strip()]
    return "\n".join(str(p).strip() for p in parts), str(choice.get("finish_reason") or "")


def extract_json_object(text: str):
    """First parseable JSON object in the text (whole blob, fenced, or embedded)."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        v = json.loads(t)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.DOTALL)
    if m:
        try:
            v = json.loads(m.group(1))
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    for m in re.finditer(r"\{", t):
        depth = 0
        for i in range(m.start(), len(t)):
            c = t[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(t[m.start():i + 1])
                        if isinstance(v, dict):
                            return v
                    except json.JSONDecodeError:
                        break
    return None


def sanitize_llm_filename(fn: str) -> str:
    """Coerce an LLM-supplied filename to <safe-name>.pdf ('' if unusable).

    House style is sentence case, so letter case is preserved; only the
    characters a .pdf filename may not contain are coerced (everything
    other than [A-Za-z0-9-] becomes an underscore)."""
    fn = str(fn or "").strip().replace("\\", "/")
    fn = fn.split("/")[-1]
    fn = re.sub(r"\.pdf$", "", fn, flags=re.IGNORECASE)
    fn = re.sub(r"[^A-Za-z0-9\-]+", "_", fn)
    fn = re.sub(r"_+", "_", fn).strip("_-")
    if not fn:
        return ""
    return fn[:120] + ".pdf"


def llm_ingest(llm: LlmSettings, allowed_sources: "list[str]", allowed_types: "list[str]",
               naming_cfg: dict, stem: str, text: str):
    """Ask the LLM for {source, type, filename} for one document.

    Returns (source|None, type|None, filename|None).  Anything the LLM gets
    wrong (bad category, no JSON, timeout) comes back as None so the caller
    can keep the rules-based value for that field.
    """
    instr = "\n".join(naming_cfg.get("instructions", []))
    sysp = (
        "You are a document-filing assistant. You are given the extracted text of ONE "
        "scanned document. Do two things:\n"
        f"1) Classify it. source must be one of: {', '.join(allowed_sources)}. type must be "
        f"one of: {', '.join(allowed_types)}. If nothing fits, use \"unknown\" for source "
        "and \"unclassified\" for type.\n"
        f"2) Invent ONE filename for it, following exactly these naming instructions:\n"
        f"{instr}\n"
        "Reply with ONLY one JSON object and no other text:\n"
        '{"source": "...", "type": "...", "filename": "..."}'
    )
    user = f"Document text:\n---\n{text[:LLM_DOC_CHARS]}\n---"
    log.info("llm: classifying/naming %s", stem)
    t0 = time.time()
    try:
        answer, finish = llm_chat(llm, sysp, user)
    except Exception as e:
        log.warning("llm: call failed (%s: %s); using rules fallback", type(e).__name__, e)
        return None, None, None
    log.info("llm: answered in %.1fs (finish=%s)", time.time() - t0, finish or "?")
    obj = extract_json_object(answer)
    if not obj:
        log.warning("llm: no JSON object in answer; rules fallback. answer: %.200s", answer)
        return None, None, None

    src_map = {s.casefold(): s for s in allowed_sources}
    typ_map = {t.casefold(): t for t in allowed_types}
    source = src_map.get(str(obj.get("source", "")).strip().casefold())
    dtype = typ_map.get(str(obj.get("type", "")).strip().casefold())
    fname = sanitize_llm_filename(obj.get("filename", "")) if llm.naming else ""
    if not source and not dtype and not fname:
        log.warning("llm: all fields unusable; rules fallback. answer: %.200s", answer)
    elif (source, dtype, fname) != (obj.get("source"), obj.get("type"), obj.get("filename")):
        log.info("llm: raw answer %s", str(obj)[:160])
    return source, dtype, fname or None


def build_llm_settings(args) -> LlmSettings:
    llm = LlmSettings(
        base_url=args.llm_url,
        model=args.llm_model,
        timeout=args.llm_timeout,
        max_tokens=args.llm_max_tokens,
        think=args.llm_think,
    )
    if args.no_llm:
        log.info("llm: disabled (--no-llm); rules-only classification and naming")
        return llm
    key, src = find_api_key(args.llm_api_key)
    if not key:
        log.warning("llm: no API key found (env LLAMA_SWAP_API_KEY, --llm-api-key, %s, "
                    "llama-swap process env); using rules only", LLAMA_SWAP_SECRETS)
        return llm
    llm.enabled = True
    llm.naming = not args.no_naming
    llm.api_key, llm.key_source = key, src
    log.info("llm: enabled (key from %s, model %s, %s, timeout %.0fs, naming %s)",
             src, llm.model, llm.base_url, llm.timeout,
             "on" if llm.naming else "off")
    return llm


# --------------------------------------------------------------------------
# deterministic naming (rules-based layer under the LLM filename)
# --------------------------------------------------------------------------
MONTH_NUM = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}

RE_ISO_DATE = re.compile(r"\b((?:19|20)\d{2})-([01]?\d)-([0-3]?\d)\b")
RE_US_DATE = re.compile(r"\b([0-1]?\d)/([0-3]?\d)/((?:19|20)\d{2})\b")
RE_LONG_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+((?:19|20)\d{2})\b",
    re.IGNORECASE)
RE_LONG_MONTH = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+((?:19|20)\d{2})\b",
    re.IGNORECASE)
RE_YEAR_MONTH = re.compile(r"\b((?:19|20)\d{2})-([01]?\d)\b")
RE_TAX_YEAR = re.compile(r"\btax\s*[- ]?\s*year\D{0,4}((?:19|20)\d{2})\b", re.IGNORECASE)
RE_DATED = re.compile(
    r"\b(?:statement\s+date|invoice\s+date|bill\s+date|date|issued(?:\s+on)?|remit\s+by|"
    r"payment\s+due|due)\s*[:\-]?\s*"
    r"((?:(?:19|20)\d{2}-[01]?\d-[0-3]?\d)|(?:[0-1]?\d/[0-3]?\d/(?:19|20)\d{2})|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+(?:19|20)\d{2})",
    re.IGNORECASE)


def _valid_date(y: int, mo: int, d: int):
    if not (1900 <= y <= 2099 and 1 <= mo <= 12):
        return None
    dim = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
           31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1]
    return (y, mo, d) if 1 <= d <= dim else None


def _parse_date_str(s: "str | None"):
    if not s:
        return None
    s = s.strip()
    m = re.fullmatch(r"([12]\d{3})-([01]?\d)-([0-3]?\d)", s)
    if m:
        return _valid_date(*(int(x) for x in m.groups()))
    m = re.fullmatch(r"([0-1]?\d)/([0-3]?\d)/([12]\d{3})", s)
    if m:
        mo, d, y = (int(x) for x in m.groups())
        return _valid_date(y, mo, d)
    m = re.fullmatch(
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+([12]\d{3})",
        s, re.IGNORECASE)
    if m:
        return _valid_date(int(m.group(3)), MONTH_NUM[m.group(1).lower()], int(m.group(2)))
    return None


def find_document_date(text: str):
    """Best-guess document date as (y, m, d), or None."""
    m = RE_DATED.search(text)
    if m:
        d = _parse_date_str(m.group(1))
        if d:
            return d
    fallbacks = (
        (RE_ISO_DATE, lambda x: (x.group(1), x.group(2), x.group(3))),
        (RE_LONG_DATE, lambda x: (x.group(3), x.group(1), x.group(2))),
        (RE_US_DATE, lambda x: (x.group(3), x.group(1), x.group(2))),
    )
    for rx, conv in fallbacks:
        for mm in rx.finditer(text):
            y, mon, d = conv(mm)
            mon = MONTH_NUM[mon.lower()] if isinstance(mon, str) else int(mon)
            v = _valid_date(int(y), mon, int(d))
            if v:
                return v
    return None


def find_tax_year(text: str, doc_date) -> "int | None":
    m = RE_TAX_YEAR.search(text)
    if m:
        return int(m.group(1))
    if doc_date:
        return doc_date[0]
    m = re.search(r"\b((?:19|20)\d{2})\b", text)
    return int(m.group(1)) if m else None


def find_year_month(text: str) -> "tuple[int, int] | None":
    m = RE_LONG_MONTH.search(text)
    if m:
        return int(m.group(2)), MONTH_NUM[m.group(1).lower()]
    m = RE_YEAR_MONTH.search(text)
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(1)), int(m.group(2))
    d = find_document_date(text)
    if d:
        return d[0], d[1]
    return None


def name_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "_"


def deterministic_name(source: str, dtype: str, text: str, naming_cfg: dict) -> str:
    """Rules-based filename: <source>_<type>_<date>, with tax / LES variants."""
    naming_cfg = naming_cfg or {}
    src, typ = name_slug(source), name_slug(dtype)

    les_types = {t.casefold() for t in naming_cfg.get("les_types", ["LES"])}
    les_re = [re.compile(p, re.IGNORECASE)
              for p in naming_cfg.get("les_text_indicators", []) if p]
    if dtype.casefold() in les_types or any(p.search(text) for p in les_re):
        ym = find_year_month(text)
        return f"les_{ym[0]}-{ym[1]:02d}.pdf" if ym else "les.pdf"

    doc_date = find_document_date(text)
    date_str = f"{doc_date[0]:04d}-{doc_date[1]:02d}-{doc_date[2]:02d}" if doc_date else ""
    if any(dtype.casefold() == t.casefold() for t in naming_cfg.get("tax_types", [])):
        year = find_tax_year(text, doc_date)
        # mirror of LLM instruction 3): no printed date -> end of the tax year
        if not date_str and year:
            date_str = f"{year}-12-31"
        parts = ([str(year)] if year else []) + [typ, src]
        if len(src) > 2 and typ.startswith(src):
            parts.remove(src)  # e.g. "irs" + "irs-statement" -> no duplication
        if date_str:
            parts.append(date_str)
        return "_".join(parts) + ".pdf"

    parts = [src, typ]
    if len(src) > 2 and typ.startswith(src):
        parts.remove(src)  # "bank" + "bank-statement" -> no duplication
    if date_str:
        parts.append(date_str)
    return "_".join(parts) + ".pdf"


# --------------------------------------------------------------------------
# one file
# --------------------------------------------------------------------------
def looks_like_pdf(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def process_file(path: str, args, rules: dict, llm: LlmSettings) -> int:
    """Process one scanner file; returns the number of output documents written."""
    name = os.path.basename(path)
    log.info("processing %s", name)

    if not looks_like_pdf(path):
        if args.leave_crud:
            log.info("not a PDF, leaving in place: %s", name)
        else:
            dest_dir = os.path.join(args.watch_dir, "skipped")
            os.makedirs(dest_dir, exist_ok=True)
            moved = unique_path(os.path.join(dest_dir, name))
            shutil.move(path, moved)
            log.info("moved non-PDF to %s", moved)
        return 0

    pages = extract_pages(path, args.dpi, rules)
    docs = find_documents(pages, rules)
    log.info("%s: %d page(s), %d document(s)", name, len(pages), len(docs))

    stem = os.path.splitext(name)[0]
    processed_dir = os.path.join(args.watch_dir, "processed")

    naming_cfg = rules.get("naming", {})
    all_sources = [r.name for r in rules["sources"]] + [UNKNOWN]
    all_types = [r.name for r in rules["types"]] + [UNCLASSIFIED]

    for doc in docs:
        full = "".join(pages[i].text + "\n\n" for i in doc)
        source = pages[doc[0]].source or rules["default_source"]
        dtype = pages[doc[0]].type or rules["default_type"]
        tag = document_tag(pages, doc)
        out_name = None
        if llm.enabled and full.strip():
            # one LLM call per document: refines source/type and invents the filename
            ls, lt, lf = llm_ingest(llm, all_sources, all_types, naming_cfg, stem, full)
            if ls:
                source = ls
            if lt:
                dtype = lt
            out_name = lf
        if not out_name:
            out_name = deterministic_name(source, dtype, full, naming_cfg)
        if args.tag_in_name:
            base, ext = os.path.splitext(out_name)
            out_name = f"{base}_{tag}{ext}"  # program-side tag; LLM names stay clean
        out_dir = os.path.join(args.output_dir, sanitize(source), sanitize(dtype))
        out_path = unique_path(os.path.join(out_dir, out_name))
        write_document(path, doc, out_path, tag=tag)
        if args.text:
            write_text_dump(full, out_path)
        log.info("  -> %s  [source=%s type=%s | %s]", out_path, source, dtype,
                 document_kind(pages, doc))

    if not args.keep_originals:
        os.makedirs(processed_dir, exist_ok=True)
        moved = unique_path(os.path.join(processed_dir, name))
        shutil.move(path, moved)
        log.info("original moved to %s", moved)

    return len(docs)


def wait_until_stable(path: str) -> bool:
    """Wait until the file size stops changing (scanner finished writing)."""
    deadline = time.time() + MAX_WAIT_SECONDS
    last = -1
    while time.time() < deadline:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return False
        if st.st_size == last and st.st_size > 0:
            return True
        last = st.st_size
        time.sleep(SETTLE_SECONDS)
    log.warning("timed out waiting for %s to settle", os.path.basename(path))
    return True


def handle(path: str, args, rules, llm: LlmSettings, seen: set) -> int:
    """Process one file; returns the number of new output documents written."""
    if not path.startswith(os.path.abspath(args.watch_dir) + os.sep):
        return 0
    rel = os.path.relpath(path, args.watch_dir)
    if rel.split(os.sep)[0] in ("processed", "skipped"):
        return 0
    if not os.path.isfile(path):
        return 0
    st = os.stat(path)
    key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    if key in seen:
        return 0
    seen.add(key)
    if not wait_until_stable(path):
        return 0
    try:
        return process_file(path, args, rules, llm)
    except Exception:
        log.exception("failed to process %s", os.path.basename(path))
        return 0


def run_after_batch(args, n_docs: int, src_file: str = "") -> None:
    """Run the user's --after-batch command once new output docs are ready.

    Runs via `sh -c`; the output/watch dirs and batch info are passed as
    SCAN_SORTER_* env vars. Failures are logged, never raised.
    """
    cmd = args.after_batch
    if not cmd:
        return
    env = dict(os.environ)
    env.update({
        "ARCHIVIST_OUTPUT_DIR": args.output_dir,
        "ARCHIVIST_WATCH_DIR": args.watch_dir,
        "ARCHIVIST_BATCH_DOCS": str(n_docs),
        "ARCHIVIST_SRC_FILE": src_file,
    })
    what = f"{n_docs} new document(s)" if n_docs > 0 else "batch"
    if src_file:
        what += f" (from {src_file})"
    log.info("after-batch: syncing %s", what)
    try:
        r = subprocess.run(["sh", "-c", cmd], env=env, capture_output=True,
                           text=True, timeout=args.after_batch_timeout)
    except subprocess.TimeoutExpired:
        log.error("after-batch command timed out after %.0fs", args.after_batch_timeout)
        return
    except Exception:
        log.exception("after-batch command failed to start")
        return
    for line in (r.stdout or "").strip().splitlines()[-15:]:
        log.info("after-batch | %s", line)
    if r.returncode != 0:
        for line in (r.stderr or "").strip().splitlines()[-15:]:
            log.error("after-batch | %s", line)
        log.error("after-batch command exited %d", r.returncode)
    else:
        log.info("after-batch: done")


# --------------------------------------------------------------------------
# watching
# --------------------------------------------------------------------------
class Handler(FileSystemEventHandler):
    def __init__(self, args, rules, llm, seen):
        self.args, self.rules, self.llm, self.seen = args, rules, llm, seen

    def _on(self, event):
        if event.is_directory:
            return
        try:
            n = handle(event.src_path, self.args, self.rules, self.llm, self.seen)
            if n:
                run_after_batch(self.args, n, os.path.basename(event.src_path))
        except Exception:
            log.exception("event error: %s", event.src_path)

    on_created = on_close = on_moved = on_modified = _on  # close/moved: scanner renames


def main() -> int:
    ap = argparse.ArgumentParser(description="Watch a folder, OCR-scan PDFs, sort by source/type.")
    ap.add_argument("watch_dir_pos", nargs="?", help="folder to watch")
    ap.add_argument("--watch-dir", dest="watch_dir", help="folder to watch")
    ap.add_argument("--output-dir", default="output")
    ap.add_argument("--rules", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json"))
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--once", action="store_true", help="process current files, then exit")
    ap.add_argument("--keep-originals", action="store_true")
    ap.add_argument("--no-text", action="store_true", help="don't write .txt dumps")
    ap.add_argument("--leave-crud", action="store_true")
    ap.add_argument("--log-to", dest="log_to", default=None)
    ap.add_argument("--no-llm", action="store_true",
                    help="disable LLM classification/naming (rules only)")
    ap.add_argument("--no-naming", action="store_true",
                    help="use the rules-based filename even when the LLM is on")
    ap.add_argument("--llm-url", default=DEFAULT_LLM_URL,
                    help="OpenAI-compatible base URL (default %(default)s)")
    ap.add_argument("--llm-model", default=DEFAULT_LLM_MODEL,
                    help="model name (default %(default)s)")
    ap.add_argument("--llm-api-key", default=None,
                    help="API key (default: auto-discovered from env/secrets file)")
    ap.add_argument("--llm-timeout", type=float, default=120.0,
                    help="per-call HTTP timeout in seconds (default %(default)s)")
    ap.add_argument("--llm-think", action="store_true",
                    help="don't request reasoning suppression (slower, used if "
                         "your backend proves flaky without it)")
    ap.add_argument("--tag-in-name", dest="tag_in_name", action="store_true", default=True,
                    help="append the digital/scanned/digital-scanned tag to output "
                         "filenames (default on; the tag is also in the PDF metadata)")
    ap.add_argument("--no-tag-in-name", dest="tag_in_name", action="store_false",
                    help="keep filenames exactly as the naming rules dictate")
    ap.add_argument("--llm-max-tokens", type=int, default=2000,
                    help="answer budget; reasoning tokens count against it (default %(default)s)")
    ap.add_argument("--after-batch", default=None, metavar="CMD",
                    help="shell command to run after new documents are sorted "
                         "(e.g. an rclone copy to OneDrive); SCAN_SORTER_* env "
                         "vars are provided")
    ap.add_argument("--after-batch-timeout", type=float, default=600.0,
                    help="max seconds for the --after-batch command (default %(default)s)")
    args = ap.parse_args()

    args.watch_dir = os.path.abspath(args.watch_dir or args.watch_dir_pos or ".")
    args.output_dir = os.path.abspath(args.output_dir)
    args.text = not args.no_text

    logp = logging.StreamHandler(sys.stdout)
    logp.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%H:%M:%S"))
    log.addHandler(logp)
    if args.log_to:
        fh = logging.FileHandler(args.log_to)
        fh.setFormatter(logp.formatter)
        log.addHandler(fh)
    log.setLevel(logging.INFO)

    if not os.path.isdir(args.watch_dir):
        sys.exit(f"watch dir does not exist: {args.watch_dir}")
    os.makedirs(args.output_dir, exist_ok=True)

    rules = load_rules(args.rules)
    llm = build_llm_settings(args)
    seen: set = set()
    log.info("watching %s  ->  %s", args.watch_dir, args.output_dir)

    # process anything already sitting in the folder
    batch_docs = 0
    for root, _dirs, files in os.walk(args.watch_dir, topdown=True):
        rel_root = os.path.relpath(root, args.watch_dir)
        if rel_root.split(os.sep)[0] in ("processed", "skipped"):
            continue
        for f in sorted(files):
            p = os.path.join(root, f)
            batch_docs += handle(p, args, rules, llm, seen)

    if args.once:
        if batch_docs and args.after_batch:
            run_after_batch(args, batch_docs)
        log.info("--once: done, exiting")
        return 0

    if HAVE_WATCHDOG:
        observer = Observer()
        observer.schedule(Handler(args, rules, llm, seen), args.watch_dir, recursive=False)
        observer.start()
    else:
        log.info("watchdog not installed — using polling every %.1fs", POLL_INTERVAL)
        observer = None

    # The inotify handler (if watchdog is installed) is the fast path only;
    # this loop is the safety net. It re-scans the watch dir unconditionally,
    # so a missed or lost file event can never wedge the pipeline silently.
    # `handle()`'s `seen` set keeps the two paths from double-processing a
    # file. If a file stays queued past STALL_FATAL_SECONDS (hung OCR, dead
    # worker, ...) we exit non-zero so a supervisor (systemd Restart=always)
    # — or the operator — notices and restarts us.
    grace = 0.0 if observer is None else POLL_GRACE_SECONDS
    queued_at: dict = {}
    warned: set = set()

    def poll_once(now: float) -> None:
        nonlocal warned
        try:
            names = sorted(n for n in os.listdir(args.watch_dir)
                           if os.path.isfile(os.path.join(args.watch_dir, n)))
        except OSError:
            return
        for n in list(queued_at):
            if n not in names:
                queued_at.pop(n, None)
        warned = {m for m in warned if m[0] in queued_at}
        for n in names:
            queued_at.setdefault(n, now)
        if not names:
            return
        oldest = names[0]
        age = now - queued_at[oldest]
        if age >= STALL_WARN_SECONDS:
            marker = (oldest, int(age // STALL_WARN_SECONDS))
            if marker not in warned:
                warned.add(marker)
                log.warning("no pipeline progress for %.0fs — queue: %s",
                            age, ", ".join(names))
        if age >= STALL_FATAL_SECONDS:
            log.error("pipeline stalled (%.0fs on %s, %d queued) — exiting "
                      "so the supervisor restarts us", age, oldest, len(names))
            raise SystemExit(1)
        for name in names:
            if now - queued_at[name] < grace:
                continue  # still inside the observer's fast-path window
            p = os.path.join(args.watch_dir, name)
            if not os.path.isfile(p):
                continue
            log.info("poll: processing %s (queued %.0fs)", name,
                     now - queued_at[name])
            n_docs = handle(p, args, rules, llm, seen)
            if n_docs:
                run_after_batch(args, n_docs, name)

    try:
        while True:
            time.sleep(5)
            poll_once(time.time())
    except KeyboardInterrupt:
        pass
    finally:
        if observer is not None:
            observer.stop()
            observer.join()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())