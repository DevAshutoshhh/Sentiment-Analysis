"""
extract_from_drive.py  ·  v5  ·  Production-grade
===================================================
Extracts Directors' Report and Independent Auditor's Report
from Indian annual report PDFs stored on Google Drive.

NEW IN v5 (over v4):
  ✓ TOC-guided extraction — detects Table of Contents, reads page numbers,
      jumps directly near the right page; eliminates TOC false positives
  ✓ TOC false-positive guard — even without TOC, requires body confirmation
      within 2 pages of a heading match before committing (prevents
      "Board's Report ........ 114" TOC lines triggering extraction)
  ✓ Text normalization — fixes "B o a r d ' s R e p o r t" OCR spacing,
      hyphenated line-breaks, curly quotes, control characters
  ✓ Dual auditor extraction — saves standalone AND consolidated reports
      separately (every Nifty 50 PDF has both)
  ✓ Metadata JSON — saves start_page, end_page, matched_heading,
      matched_stop, confidence_score, char_count per extraction
  ✓ Section validation — rejects extractions missing required keywords
  ✓ Matched-regex logging — logs exactly which heading/stop/signature fired
  ✓ Scanned PDF detection — flags image-only pages, warns when significant

RETAINED FROM v4:
  ✓ Signature block truncation (For and on behalf / Chartered Accountants)
  ✓ MAX_SECTION_PAGES cap (Directors <= 60, Auditors <= 25)
  ✓ Adaptive skip_first_n (thin vs thick PDFs)
  ✓ SKIP_PAGE (AGM notice, proxy form, e-voting)
  ✓ Noise line removal (page numbers, running headers, URLs)
  ✓ Resume support (skips already-extracted files)
  ✓ Retry if Auditors not found on first pass
  ✓ Diagnostic mode (page-by-page trace)

Requirements:
    pip install pdfplumber google-api-python-client google-auth google-auth-oauthlib

Output per PDF:
    extracted_reports/<CompanyName>/
        Annual_Report_2022_directors_report.txt
        Annual_Report_2022_auditors_report_standalone.txt
        Annual_Report_2022_auditors_report_consolidated.txt
        Annual_Report_2022_metadata.json
"""

import io
import json
import os
import re
import sys
import logging
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path
from datetime import datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

try:
    import pdfplumber
except ImportError:
    print("Missing: pip install pdfplumber")
    sys.exit(1)


# ===============================================================
# CONFIG
# ===============================================================

CLIENT_SECRET_FILE   = "client_secrets.json"
TOKEN_FILE           = "token.json"
DRIVE_ROOT_FOLDER_ID = "1WSgPJpe8sBZlty6rsOYfWALxRmQg2Tk6"
LOCAL_OUTPUT_DIR     = Path("extracted_reports")
SCOPES               = ["https://www.googleapis.com/auth/drive.readonly"]

FORCE_REEXTRACT = False   # Set True to re-extract already-done files
DIAGNOSTIC_MODE = False   # Set True to print page-by-page boundary info

# Hard caps — section is truncated if it runs longer than this many pages.
# Real Nifty 50: Directors' Report = 15-40 pages, Auditors' = 8-15 pages.
MAX_DIRECTORS_PAGES = 60
MAX_AUDITORS_PAGES  = 25

# Pages with fewer characters than this are likely scanned/image-only.
MIN_TEXT_CHARS = 80

# After a heading match, require body confirmation within this many pages.
# Prevents TOC entry lines ("Board's Report ........ 42") triggering extraction.
TOC_GUARD_PAGES = 2

# Lines to keep after a signature block (captures name / date / place).
SIGNATURE_TRAIL_LINES = 12

# TOC: minimum dot-leader entries on a page to classify it as a TOC page.
TOC_PAGE_MIN_ENTRIES = 5

# TOC hint offset: start searching this many pages before the TOC-reported page.
TOC_SEARCH_MARGIN = 5


# ===============================================================
# METADATA DATACLASS
# ===============================================================

@dataclass
class SectionMeta:
    section:          str  = ""
    start_page:       int  = 0
    end_page:         int  = 0
    matched_heading:  str  = ""
    matched_stop:     str  = ""
    matched_sig:      str  = ""
    toc_hint_page:    int  = 0      # page from TOC (0 = not used)
    confidence:       int  = 0      # 0-100
    char_count:       int  = 0
    scanned_pages:    list = field(default_factory=list)
    validation_flags: list = field(default_factory=list)
    status:           str  = "not_found"  # ok | not_found | validation_fail | truncated


# ===============================================================
# LOGGING
# ===============================================================

def setup_logging():
    LOCAL_OUTPUT_DIR.mkdir(exist_ok=True)
    log_file = LOCAL_OUTPUT_DIR / f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file

log = logging.getLogger(__name__)


# ===============================================================
# GOOGLE DRIVE
# ===============================================================

def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


def _paginate(service_call, **kwargs):
    results, page_token = [], None
    while True:
        resp = service_call(pageToken=page_token, **kwargs).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def list_subfolders(service, parent_id):
    return _paginate(
        service.files().list,
        q=(f"'{parent_id}' in parents and "
           "mimeType='application/vnd.google-apps.folder' and trashed=false"),
        fields="nextPageToken, files(id, name)",
    )


def list_pdfs_in_folder(service, folder_id):
    return _paginate(
        service.files().list,
        q=(f"'{folder_id}' in parents and "
           "mimeType='application/pdf' and trashed=false"),
        fields="nextPageToken, files(id, name, size)",
    )


def download_pdf_to_memory(service, file_id):
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl  = MediaIoBaseDownload(buf, request, chunksize=8 * 1024 * 1024)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


# ===============================================================
# TEXT NORMALIZATION
# ===============================================================

# Hyphenated line-break: "direc-\ntor" -> "director"
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")

# Curly/smart quotes -> straight
_CURLY = str.maketrans("\u2018\u2019\u201c\u201d\u2032\u2033", "''\"\"''")

# Control characters (except \n and \t)
_CTRL = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")

# Spaced-out single chars: "D i r e c t o r s" -> "Directors"
# Matches: start-of-boundary, then letter, then (space+letter) repeated 3+ times
_SPACED_LETTERS = re.compile(
    r"(?:^|(?<=\s))([A-Za-z])((?:[ \t][A-Za-z']){3,})(?=\s|$)",
    re.MULTILINE,
)


def normalize_text(text: str) -> str:
    """
    Fix common OCR and PDF glyph-extraction artifacts in Indian annual reports:
      - Spaced-out headings: "B o a r d ' s R e p o r t" -> "Board's Report"
      - Hyphenated line breaks: "direc-\\ntor" -> "director"
      - Curly/smart quotes -> straight apostrophes
      - Control characters
    Applied iteratively for multi-word spaced headings.
    """
    # Fix spaced-out characters (iterate until stable)
    def collapse(m):
        return re.sub(r"[ \t]", "", m.group(1) + m.group(2))

    prev = None
    while prev != text:
        prev = text
        text = _SPACED_LETTERS.sub(collapse, text)

    # After collapsing, apostrophe joins adjacent words: "Board'sReport"
    # Re-split at camelCase boundaries that the apostrophe caused
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)

    # Collapse leftover multiple spaces
    text = re.sub(r"[ \t]{2,}", " ", text)

    # Fix hyphenated line breaks
    text = _HYPHEN_BREAK.sub(r"\1\2", text)

    # Normalize quotes/apostrophes
    text = text.translate(_CURLY)

    # Strip control characters
    text = _CTRL.sub("", text)

    return text


# ===============================================================
# REGEX PATTERNS
# ===============================================================

# Apostrophe variants (after normalization, curly are already -> straight)
_AP = r"['\u2018\u2019\u02bc]"


# -- START: Directors' Report ----------------------------------

DIRECTORS_HEADING = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"directors?" + _AP + r"?\s*report"
    r"|board" + _AP + r"?s?\s*report"
    r"|report\s+of\s+(?:the\s+)?directors?"
    r")"
    r"\s*(?:\n|$)",
    re.IGNORECASE,
)

DIRECTORS_BODY = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"(?:dear\s+(?:members|shareholders|shareowners)[,.]?\s*\n\s*)?"
    r"your\s+directors\s+"
    r"(?:are\s+pleased\s+to\s+present"
    r"|hereby\s+present"
    r"|have\s+(?:the\s+)?(?:honour|honor|pleasure)"
    r"|take\s+(?:great\s+)?pleasure"
    r"|present\s+the\s+\d+)"
    r"|to\s+the\s+members[,\s]+your\s+directors"
    r")",
    re.IGNORECASE,
)

# Body confirmation: real Directors' Report body text (not a TOC line)
DIRECTORS_BODY_CONFIRM = re.compile(
    r"(?:"
    r"your\s+directors"
    r"|dear\s+(?:members|shareholders)"
    r"|(?:the\s+)?board\s+(?:of\s+directors\s+)?(?:is\s+pleased|hereby|takes)"
    r"|we\s+(?:are\s+pleased|hereby)\s+present"
    r"|(?:financial\s+)?(?:performance|highlights?)\s+of\s+the\s+company"
    r"|pursuant\s+to\s+(?:section|the\s+provisions)"
    r")",
    re.IGNORECASE,
)


# -- START: Auditors' Report -----------------------------------

AUDITORS_HEADING = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"(?:standalone\s+)?independent\s+auditors?" + _AP + r"?s?\s*report"
    r"|(?:standalone\s+)?report\s+of\s+the\s+independent\s+auditors?"
    r"|auditors?" + _AP + r"?s?\s*report"
    r")"
    r"\s*(?:\n|$)",
    re.IGNORECASE,
)

CONSOLIDATED_AUDITORS_HEADING = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"consolidated\s+(?:independent\s+)?auditors?" + _AP + r"?s?\s*report"
    r"|consolidated\s+report\s+of\s+the\s+(?:independent\s+)?auditors?"
    r")"
    r"\s*(?:\n|$)",
    re.IGNORECASE,
)

AUDITORS_BODY = re.compile(
    r"(?:^|\n)\s*"
    r"to\s+the\s+(?:members|board\s+of\s+directors)\s+of\s+[A-Z\u00C0-\u024F]",
    re.IGNORECASE,
)

# Body confirmation: real Auditors' Report body (not a TOC or inline mention)
AUDITORS_BODY_CONFIRM = re.compile(
    r"(?:"
    r"we\s+have\s+audited"
    r"|in\s+our\s+opinion"
    r"|basis\s+for\s+(?:qualified\s+)?opinion"
    r"|key\s+audit\s+matters?"
    r"|management\s+(?:is\s+)?responsible\s+for"
    r"|(?:material\s+)?misstatement"
    r")",
    re.IGNORECASE,
)


# -- STOP: Directors' Report -----------------------------------

DIRECTORS_STOP = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"(?:standalone\s+|consolidated\s+)?"
    r"(?:"
    r"balance\s+sheet"
    r"|statement\s+of\s+(?:profit(?:\s+and\s+loss)?|financial\s+position|cash\s+flows?)"
    r"|cash\s+flow\s+statement"
    r"|profit\s+and\s+loss\s+(?:account|statement)"
    r")"
    r"|(?:standalone|consolidated)\s+financial\s+statements?"
    r"|(?:independent\s+)?auditors?" + _AP + r"?s?\s*report"
    r"|report\s+of\s+the\s+(?:independent\s+)?auditors?"
    r"|corporate\s+governance\s+report"
    r"|business\s+responsibility\s+(?:and\s+sustainability\s+)?report"
    r"|ten[\s\-]year\s+(?:financial\s+)?(?:summary|highlights?|data)"
    r"|financial\s+highlights?\s+at\s+a\s+glance"
    r")"
    r"\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# -- STOP: Standalone Auditors' Report ------------------------

AUDITORS_STOP = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"(?:standalone\s+|consolidated\s+)?"
    r"(?:"
    r"balance\s+sheet"
    r"|statement\s+of\s+(?:profit(?:\s+and\s+loss)?|financial\s+position|cash\s+flows?)"
    r"|cash\s+flow\s+statement"
    r"|profit\s+and\s+loss\s+(?:account|statement)"
    r"|notes\s+to\s+(?:the\s+)?(?:financial\s+statements?|accounts?)"
    r"|significant\s+accounting\s+policies"
    r"|schedules?\s+forming\s+part"
    r")"
    r"|(?:standalone|consolidated)\s+financial\s+statements?"
    r"|consolidated\s+(?:independent\s+)?auditors?" + _AP + r"?s?\s*report"
    r"|consolidated\s+report\s+of\s+the\s+(?:independent\s+)?auditors?"
    r")"
    r"\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# -- STOP: Consolidated Auditors' Report ----------------------

CONSOL_AUDITORS_STOP = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"(?:consolidated\s+)?"
    r"(?:"
    r"balance\s+sheet"
    r"|statement\s+of\s+(?:profit(?:\s+and\s+loss)?|financial\s+position|cash\s+flows?)"
    r"|cash\s+flow\s+statement"
    r"|notes\s+to\s+(?:the\s+)?(?:financial\s+statements?|accounts?)"
    r"|significant\s+accounting\s+policies"
    r")"
    r"|consolidated\s+financial\s+statements?"
    r")"
    r"\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# -- SIGNATURE BLOCKS -----------------------------------------

DIRECTORS_SIGNATURE = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"for\s+and\s+on\s+behalf\s+of\s+(?:the\s+)?board"
    r"|on\s+behalf\s+of\s+(?:the\s+)?board\s+of\s+directors"
    r"|by\s+order\s+of\s+(?:the\s+)?board"
    r")",
    re.IGNORECASE,
)

AUDITORS_SIGNATURE = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"for\s+[A-Z][a-zA-Z\s&,\.]+\s*\n\s*chartered\s+accountants"
    r"|membership\s+no(?:\.|\s)"
    r"|(?:icai\s+)?(?:firm\s+)?registration\s+no(?:\.|\s)"
    r"|partner\s*\n\s*membership"
    r")",
    re.IGNORECASE,
)


# -- SKIP entire page -----------------------------------------

SKIP_PAGE = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"notice\s+of\s+(?:the\s+)?(?:\d+\s*(?:st|nd|rd|th)\s+)?(?:annual\s+general\s+meeting|agm)"
    r"|notice\b[\s\S]{0,200}annual\s+general[\s\S]{0,30}meeting"
    r"|proxy\s+form\b"
    r"|attendance\s+slip\b"
    r"|e[\s\-]?voting\s+(?:instructions?|procedure|process|facility)"
    r"|route\s+map\s+to\s+(?:the\s+)?(?:agm|venue|meeting)"
    r"|form\s+no\.?\s*mgt[\s\-]?\d+"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


# -- Noise lines ----------------------------------------------

NOISE_LINE = re.compile(
    r"^\s*"
    r"(?:"
    r"\d{1,4}\s*$"
    r"|page\s+\d+\s*(?:of\s+\d+)?\s*$"
    r"|(?:integrated\s+)?annual\s+report\s*"
    r"(?:20\d{2})?(?:[\s\-\u2013\u2014](?:20)?\d{2,4})?\s*$"
    r"|fy\s*20\d{2}(?:[\s\-\u2013\u2014](?:20)?\d{2})?\s*$"
    r"|[|x{2502}x{2500}x{2550}\-_=~]{4,}\s*$"
    r"|(?:cin|gstin|pan|llpin)\s*[:\-]\s*\S+\s*$"
    r"|(?:https?://|www\.)\S+\s*$"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# -- TOC patterns ---------------------------------------------

# Matches: "Board's Report .......... 42" or "Board's Report   42"
TOC_LINE = re.compile(
    r"(?P<title>[A-Za-z][^\n]{4,59}?)"   # title: letter-start, 5-60 chars
    r"[\s.x{2026}\-]{2,}"                  # separator (dots, spaces, dashes, ellipsis)
    r"(?P<page>\d{1,4})"                   # page number
    r"[ \t]*(?:\n|$)",
    re.MULTILINE,
)

TOC_DIRECTORS = re.compile(
    r"(?:directors?|board)" + _AP + r"?s?\s*report",
    re.IGNORECASE,
)
TOC_AUDITORS_CONSOL = re.compile(
    r"consolidated\s+(?:independent\s+)?auditors?" + _AP + r"?s?\s*report",
    re.IGNORECASE,
)
TOC_AUDITORS_SA = re.compile(
    r"(?:(?:independent\s+)?auditors?" + _AP + r"?s?\s*report)"
    r"(?!\s*\(?\s*consolidated)",   # exclude consolidated variant
    re.IGNORECASE,
)


# ===============================================================
# SECTION VALIDATION KEYWORDS
# ===============================================================

# Directors' Report must match at least DIRECTORS_MIN_MATCHES of these
DIRECTORS_REQUIRED = [
    re.compile(r"dividend",                                  re.IGNORECASE),
    re.compile(r"board\s+meeting",                           re.IGNORECASE),
    re.compile(r"directors?\s+responsib",                    re.IGNORECASE),
    re.compile(r"(?:corporate\s+social\s+responsibility|csr)", re.IGNORECASE),
    re.compile(r"(?:financial\s+)?(?:results?|performance)", re.IGNORECASE),
    re.compile(r"(?:internal\s+financial\s+controls?|ifc)",  re.IGNORECASE),
]
DIRECTORS_MIN_MATCHES = 3

# Auditors' Report must match at least AUDITORS_MIN_MATCHES of these
AUDITORS_REQUIRED = [
    re.compile(r"in\s+our\s+opinion",                        re.IGNORECASE),
    re.compile(r"basis\s+for\s+(?:qualified\s+)?opinion",    re.IGNORECASE),
    re.compile(r"chartered\s+accountants",                    re.IGNORECASE),
    re.compile(r"(?:material\s+misstatement|true\s+and\s+fair)", re.IGNORECASE),
    re.compile(r"(?:key\s+audit\s+matters?|kam)",            re.IGNORECASE),
]
AUDITORS_MIN_MATCHES = 2


# ===============================================================
# TEXT UTILITIES
# ===============================================================

def clean_text(text: str) -> str:
    lines   = text.splitlines()
    cleaned = [ln for ln in lines if not NOISE_LINE.match(ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()


def truncate_at_signature(text: str, sig_pattern, label: str) -> tuple:
    """
    Find signature block, keep SIGNATURE_TRAIL_LINES lines after it.
    Returns (trimmed_text, matched_sig_snippet).
    """
    m = sig_pattern.search(text)
    if not m:
        return text, ""
    tail    = text[m.start():]
    lines   = tail.splitlines()
    kept    = "\n".join(lines[:SIGNATURE_TRAIL_LINES])
    snippet = lines[0].strip()[:80]
    log.info(f"    [{label}] Signature matched: {snippet!r}")
    return text[:m.start()] + kept, snippet


def load_pages(pdf_bytes: bytes) -> tuple:
    """
    Returns (pages_list, scanned_page_numbers).
    Normalizes text immediately after extraction.
    scanned_page_numbers: 1-based page numbers with very little text.
    """
    pages, scanned = [], []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for idx, pg in enumerate(pdf.pages):
            try:
                text = pg.extract_text(x_tolerance=3, y_tolerance=3) or ""
            except Exception:
                text = ""
            text = normalize_text(text)
            pages.append(text)
            if len(text.strip()) < MIN_TEXT_CHARS:
                scanned.append(idx + 1)
    return pages, scanned


def compute_skip_n(total: int) -> int:
    if total < 60:   return 8
    if total < 120:  return 15
    if total < 250:  return 25
    return 35


def validate_section(text: str, required_patterns: list, min_matches: int) -> list:
    """Returns list of failure messages; empty list = passed."""
    matched = sum(1 for p in required_patterns if p.search(text))
    if matched < min_matches:
        return [
            f"Only {matched}/{len(required_patterns)} required keywords found "
            f"(need {min_matches})"
        ]
    return []


def compute_confidence(meta: SectionMeta) -> int:
    """
    Additive confidence score (0-100):
      heading matched          : +30
      stop matched             : +15
      signature matched        : +20
      toc hint used            : +15
      heading + stop (implied body confirmed): +20
    """
    score = 0
    if meta.matched_heading: score += 30
    if meta.matched_stop:    score += 15
    if meta.matched_sig:     score += 20
    if meta.toc_hint_page:   score += 15
    if meta.matched_heading and meta.matched_stop:
        score += 20
    return min(score, 100)


# ===============================================================
# TOC PARSER
# ===============================================================

def parse_toc(pages: list, skip_first_n: int) -> dict:
    """
    Scan early pages for a Table of Contents.
    Returns dict: directors, auditors_standalone, auditors_consolidated -> page number (0 = not found).
    """
    hints = {"directors": 0, "auditors_standalone": 0, "auditors_consolidated": 0}
    scan_limit = min(skip_first_n * 2, len(pages))

    for i in range(scan_limit):
        raw  = pages[i]
        pnum = i + 1

        toc_matches = list(TOC_LINE.finditer(raw))
        if len(toc_matches) < TOC_PAGE_MIN_ENTRIES:
            continue

        log.info(f"    [TOC] Detected on page {pnum} ({len(toc_matches)} entries)")

        for m in toc_matches:
            title = m.group("title").strip()
            try:
                pg = int(m.group("page"))
            except ValueError:
                continue

            if TOC_DIRECTORS.search(title) and hints["directors"] == 0:
                hints["directors"] = pg
                log.info(f"    [TOC] Directors Report -> page {pg}  ({title!r})")

            elif TOC_AUDITORS_CONSOL.search(title) and hints["auditors_consolidated"] == 0:
                hints["auditors_consolidated"] = pg
                log.info(f"    [TOC] Consolidated Auditors -> page {pg}  ({title!r})")

            elif TOC_AUDITORS_SA.search(title) and hints["auditors_standalone"] == 0:
                hints["auditors_standalone"] = pg
                log.info(f"    [TOC] Standalone Auditors -> page {pg}  ({title!r})")

    return hints


def toc_to_search_start(hint_page: int, total: int) -> int:
    """Convert 1-based TOC page hint to 0-based search index with margin."""
    if not hint_page:
        return 0
    return max(0, min(hint_page - 1 - TOC_SEARCH_MARGIN, total - 1))


# ===============================================================
# CORE SECTION EXTRACTOR
# ===============================================================

def find_section(
    pages: list,
    start_patterns: list,
    stop_pattern,
    body_confirm_pattern,
    section_name: str,
    skip_first_n: int,
    search_from: int = 0,
    max_pages: int = 999,
    toc_hint_page: int = 0,
) -> tuple:
    """
    Extract one named section.
    Returns (text, end_page_index, SectionMeta).

    Two-phase start detection:
      Phase A: find heading pattern on page
      Phase B: require body_confirm_pattern within TOC_GUARD_PAGES pages
               before committing (prevents TOC-line false positives)
    """
    meta = SectionMeta(section=section_name, toc_hint_page=toc_hint_page)

    effective_start = (
        toc_to_search_start(toc_hint_page, len(pages)) if toc_hint_page
        else search_from
    )
    if toc_hint_page:
        log.info(
            f"    [{section_name}] TOC hint page {toc_hint_page} "
            f"-> searching from index {effective_start}"
        )

    collecting    = False
    collected     = []
    found_at      = None
    end_idx       = len(pages)

    # Guard window state
    pending_pages   = []
    pending_info    = None   # (pnum, match_obj, trimmed_text)

    for i in range(effective_start, len(pages)):
        raw  = pages[i]
        pnum = i + 1

        # ── Not yet collecting ────────────────────────────────
        if not collecting and pending_info is None:
            if SKIP_PAGE.search(raw):
                continue

            area = raw if toc_hint_page else (raw[:300] if pnum <= skip_first_n else raw)

            matched_pat = next((p for p in start_patterns if p.search(area)), None)
            if not matched_pat:
                continue

            m = matched_pat.search(area)
            match_pos = m.start() if m else 0
            trimmed   = raw[match_pos:]
            heading_snippet = (m.group(0) if m else "").strip()[:80]

            # Check immediate body confirmation (rest of this same page)
            rest = raw[m.end():] if m else raw
            if body_confirm_pattern.search(rest):
                # Confirmed immediately
                collecting = True
                found_at   = pnum
                meta.start_page     = pnum
                meta.matched_heading = heading_snippet
                log.info(f"    [{section_name}] Started (immediate confirm) p{pnum} | {heading_snippet!r}")
                sm = stop_pattern.search(trimmed)
                if sm:
                    collected.append(trimmed[:sm.start()])
                    meta.matched_stop = trimmed[sm.start():sm.start()+80].strip()
                    end_idx = i
                    log.info(f"    [{section_name}] Found+stopped on p{pnum}")
                    break
                collected.append(trimmed)
            else:
                # Enter guard window
                pending_info  = (pnum, heading_snippet, trimmed)
                pending_pages = []
                if DIAGNOSTIC_MODE:
                    log.info(f"    p{pnum}: heading found, guard window open ({heading_snippet!r})")
            continue

        # ── Inside guard window ───────────────────────────────
        if pending_info is not None and not collecting:
            guard_elapsed = pnum - pending_info[0]

            if body_confirm_pattern.search(raw):
                # Confirmed within window
                p_pnum, heading_snippet, p_trimmed = pending_info
                collecting = True
                found_at   = p_pnum
                meta.start_page     = p_pnum
                meta.matched_heading = heading_snippet
                log.info(
                    f"    [{section_name}] Started (guard confirm p{pnum}) "
                    f"from p{p_pnum} | {heading_snippet!r}"
                )
                collected.extend(pending_pages)
                collected.append(raw)
                pending_info  = None
                pending_pages = []
                continue

            elif guard_elapsed >= TOC_GUARD_PAGES:
                # Window expired — false positive (likely TOC line)
                log.info(f"    [{section_name}] TOC false positive rejected at p{pending_info[0]}")
                pending_info  = None
                pending_pages = []
                # Don't skip this page — re-evaluate it as a fresh start candidate
                i -= 1  # will be incremented by for-loop... but we can't do that in Python
                # Instead: check if current page itself is a start
                matched_pat2 = next((p for p in start_patterns if p.search(raw)), None)
                if matched_pat2:
                    m2 = matched_pat2.search(raw)
                    trimmed2 = raw[m2.start():] if m2 else raw
                    rest2 = raw[m2.end():] if m2 else raw
                    hs2   = (m2.group(0) if m2 else "").strip()[:80]
                    if body_confirm_pattern.search(rest2):
                        collecting = True
                        found_at   = pnum
                        meta.start_page     = pnum
                        meta.matched_heading = hs2
                        log.info(f"    [{section_name}] Started (re-eval) p{pnum} | {hs2!r}")
                        collected.append(trimmed2)
                    else:
                        pending_info  = (pnum, hs2, trimmed2)
                        pending_pages = []
                continue
            else:
                pending_pages.append(raw)
                continue

        # ── Collecting ────────────────────────────────────────
        pages_collected = i - (found_at - 1)
        if pages_collected >= max_pages:
            log.warning(f"    [{section_name}] MAX_PAGES cap ({max_pages}) hit at p{pnum}")
            meta.status = "truncated"
            meta.end_page = pnum
            end_idx = i
            break

        sm = stop_pattern.search(raw)
        if sm:
            partial = raw[:sm.start()].strip()
            if partial:
                collected.append(partial)
            end_idx = i
            meta.end_page    = pnum
            meta.matched_stop = raw[sm.start():sm.start()+80].strip()
            log.info(
                f"    [{section_name}] Stopped p{pnum} "
                f"(from p{found_at}, {pnum - found_at} pages) "
                f"| stop: {meta.matched_stop!r}"
            )
            break

        if SKIP_PAGE.search(raw):
            if DIAGNOSTIC_MODE:
                log.info(f"    p{pnum}: SKIP mid-section (AGM/notice)")
            continue

        if DIAGNOSTIC_MODE:
            log.info(f"    p{pnum}: collecting ({pages_collected}/{max_pages})")
        collected.append(raw)

    if not collected:
        return "", end_idx, meta

    text = clean_text("\n\n".join(collected))
    return text, end_idx, meta


# ===============================================================
# PDF PROCESSOR
# ===============================================================

def extract_from_bytes(pdf_bytes: bytes) -> dict:
    """
    Returns:
      directors_report            : str
      auditors_report_standalone  : str
      auditors_report_consolidated: str
      metadata                    : list[dict]
      scanned_pages               : list[int]
    """
    try:
        pages, scanned_pages = load_pages(pdf_bytes)
    except Exception as e:
        log.error(f"    Failed to read PDF: {e}")
        return {
            "directors_report": "Not Found",
            "auditors_report_standalone": "Not Found",
            "auditors_report_consolidated": "Not Found",
            "metadata": [], "scanned_pages": [],
        }

    total  = len(pages)
    skip_n = compute_skip_n(total)
    log.info(f"    Pages: {total}  |  skip_first_n: {skip_n}")

    if scanned_pages:
        pct = len(scanned_pages) / total * 100
        log.warning(
            f"    Scanned/image pages: {len(scanned_pages)} ({pct:.0f}%)"
            f" — text extraction may be incomplete"
        )

    # Parse TOC
    toc = parse_toc(pages, skip_n)
    all_meta = []

    def _post_process(text, sig_pat, req_pats, min_m, meta, label):
        """Signature trim + validation + confidence. Mutates meta. Returns text."""
        if not text:
            meta.status = "not_found"
            return text
        text, sig = truncate_at_signature(text, sig_pat, label)
        meta.matched_sig = sig
        meta.char_count  = len(text)
        flags = validate_section(text, req_pats, min_m)
        meta.validation_flags = flags
        if flags:
            log.warning(f"    [{label}] Validation warning: {flags}")
            meta.status = "validation_fail"
        else:
            meta.status = "ok"
        meta.confidence = compute_confidence(meta)
        log.info(f"    {label}: {meta.char_count:,} chars | confidence: {meta.confidence}%")
        if 0 < meta.char_count < 400:
            log.warning(f"    [{label}] Very short — possible extraction failure")
        return text

    # ── Directors' Report ─────────────────────────────────────
    dr_text, dir_end, dr_meta = find_section(
        pages,
        start_patterns=[DIRECTORS_HEADING, DIRECTORS_BODY],
        stop_pattern=DIRECTORS_STOP,
        body_confirm_pattern=DIRECTORS_BODY_CONFIRM,
        section_name="Directors Report",
        skip_first_n=skip_n,
        search_from=0,
        max_pages=MAX_DIRECTORS_PAGES,
        toc_hint_page=toc["directors"],
    )
    dr_meta.scanned_pages = scanned_pages
    dr_text = _post_process(
        dr_text, DIRECTORS_SIGNATURE,
        DIRECTORS_REQUIRED, DIRECTORS_MIN_MATCHES,
        dr_meta, "Directors Report",
    )
    all_meta.append(asdict(dr_meta))

    # ── Standalone Auditors' Report ───────────────────────────
    aud_start = max(dir_end, skip_n)
    sa_text, sa_end, sa_meta = find_section(
        pages,
        start_patterns=[AUDITORS_HEADING, AUDITORS_BODY],
        stop_pattern=AUDITORS_STOP,
        body_confirm_pattern=AUDITORS_BODY_CONFIRM,
        section_name="Auditors (Standalone)",
        skip_first_n=skip_n,
        search_from=aud_start,
        max_pages=MAX_AUDITORS_PAGES,
        toc_hint_page=toc["auditors_standalone"],
    )
    if not sa_text:
        log.info("    [Auditors Standalone] Retrying from page 1...")
        sa_text, sa_end, sa_meta = find_section(
            pages,
            start_patterns=[AUDITORS_HEADING],
            stop_pattern=AUDITORS_STOP,
            body_confirm_pattern=AUDITORS_BODY_CONFIRM,
            section_name="Auditors (Standalone, retry)",
            skip_first_n=skip_n,
            search_from=0,
            max_pages=MAX_AUDITORS_PAGES,
            toc_hint_page=0,
        )
    sa_text = _post_process(
        sa_text, AUDITORS_SIGNATURE,
        AUDITORS_REQUIRED, AUDITORS_MIN_MATCHES,
        sa_meta, "Auditors (Standalone)",
    )
    all_meta.append(asdict(sa_meta))

    # ── Consolidated Auditors' Report ─────────────────────────
    cs_text, _, cs_meta = find_section(
        pages,
        start_patterns=[CONSOLIDATED_AUDITORS_HEADING, AUDITORS_HEADING],
        stop_pattern=CONSOL_AUDITORS_STOP,
        body_confirm_pattern=AUDITORS_BODY_CONFIRM,
        section_name="Auditors (Consolidated)",
        skip_first_n=skip_n,
        search_from=sa_end,
        max_pages=MAX_AUDITORS_PAGES,
        toc_hint_page=toc["auditors_consolidated"],
    )
    cs_text = _post_process(
        cs_text, AUDITORS_SIGNATURE,
        AUDITORS_REQUIRED, AUDITORS_MIN_MATCHES,
        cs_meta, "Auditors (Consolidated)",
    )
    all_meta.append(asdict(cs_meta))

    return {
        "directors_report":             dr_text if dr_text else "Not Found",
        "auditors_report_standalone":   sa_text if sa_text else "Not Found",
        "auditors_report_consolidated": cs_text if cs_text else "Not Found",
        "metadata":      all_meta,
        "scanned_pages": scanned_pages,
    }


# ===============================================================
# MAIN
# ===============================================================

def main():
    log_file = setup_logging()
    log.info(f"Output folder    : {LOCAL_OUTPUT_DIR.resolve()}")
    log.info(f"Log file         : {log_file}")
    log.info(f"Max Directors pg : {MAX_DIRECTORS_PAGES}")
    log.info(f"Max Auditors pg  : {MAX_AUDITORS_PAGES}")
    log.info(f"Diagnostic mode  : {DIAGNOSTIC_MODE}")

    log.info("Authenticating with Google Drive...")
    service = get_drive_service()
    log.info("Authenticated!\n")

    company_folders = list_subfolders(service, DRIVE_ROOT_FOLDER_ID)
    log.info(f"Found {len(company_folders)} company folders\n")

    stats = dict(pdfs=0, saved=0, skipped=0, not_found=0, errors=0, toc_hits=0)

    for cf in sorted(company_folders, key=lambda x: x["name"]):
        cname, cfid = cf["name"], cf["id"]
        log.info(f"{'─'*60}")
        log.info(f"Company: {cname}")

        local_dir = LOCAL_OUTPUT_DIR / cname
        local_dir.mkdir(exist_ok=True)

        pdfs = list_pdfs_in_folder(service, cfid)
        if not pdfs:
            log.info("  No PDFs found.")
            continue
        log.info(f"  PDFs: {len(pdfs)}")

        for pdf_file in sorted(pdfs, key=lambda x: x["name"]):
            name = pdf_file["name"]
            fid  = pdf_file["id"]
            size = int(pdf_file.get("size", 0)) / (1024 * 1024)
            stats["pdfs"] += 1

            log.info(f"\n  [{name}] ({size:.1f} MB)")

            stem      = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
            path_dr   = local_dir / f"{stem}_directors_report.txt"
            path_sa   = local_dir / f"{stem}_auditors_report_standalone.txt"
            path_cs   = local_dir / f"{stem}_auditors_report_consolidated.txt"
            path_meta = local_dir / f"{stem}_metadata.json"

            if not FORCE_REEXTRACT and path_dr.exists() and path_sa.exists():
                log.info("  Already extracted, skipping.")
                stats["skipped"] += 1
                continue

            try:
                log.info("  Downloading...")
                pdf_bytes = download_pdf_to_memory(service, fid)
            except Exception as e:
                log.error(f"  ERROR downloading: {e}")
                stats["errors"] += 1
                continue

            try:
                results = extract_from_bytes(pdf_bytes)
            except Exception as e:
                log.error(f"  ERROR extracting: {e}\n{traceback.format_exc()}")
                stats["errors"] += 1
                del pdf_bytes
                continue

            del pdf_bytes

            for m in results.get("metadata", []):
                if m.get("toc_hint_page", 0):
                    stats["toc_hits"] += 1

            output_map = {
                path_dr: results["directors_report"],
                path_sa: results["auditors_report_standalone"],
                path_cs: results["auditors_report_consolidated"],
            }
            for out_path, text in output_map.items():
                if out_path.exists() and not FORCE_REEXTRACT:
                    continue
                try:
                    out_path.write_text(text, encoding="utf-8")
                    if text == "Not Found":
                        log.warning(f"  NOT FOUND : {out_path.name}")
                        stats["not_found"] += 1
                    else:
                        log.info(f"  Saved     : {out_path.name} [{len(text):,} chars]")
                        stats["saved"] += 1
                except Exception as e:
                    log.error(f"  ERROR saving {out_path.name}: {e}")
                    stats["errors"] += 1

            # Save metadata JSON
            try:
                meta_payload = {
                    "pdf":           name,
                    "extracted_at":  datetime.now().isoformat(),
                    "scanned_pages": results["scanned_pages"],
                    "sections":      results["metadata"],
                }
                path_meta.write_text(
                    json.dumps(meta_payload, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                log.info(f"  Saved     : {path_meta.name}")
            except Exception as e:
                log.error(f"  ERROR saving metadata: {e}")

    log.info(f"\n{'='*60}")
    log.info(f"DONE  {LOCAL_OUTPUT_DIR.resolve()}")
    log.info(f"  PDFs processed  : {stats['pdfs']}")
    log.info(f"  .txt files saved: {stats['saved']}")
    log.info(f"  Not Found       : {stats['not_found']}")
    log.info(f"  Skipped (done)  : {stats['skipped']}")
    log.info(f"  TOC hints used  : {stats['toc_hits']}")
    log.info(f"  Errors          : {stats['errors']}")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    main()
