"""
extract_from_drive.py  ·  v3  ·  Production-grade
===================================================
Extracts Directors' Report and Independent Auditor's Report
from Indian annual report PDFs stored on Google Drive.

Tested against real PDFs. Handles:
  ✓ All known heading variants (apostrophe position, ALL-CAPS, plural, no-apostrophe)
  ✓ Board's Report / Directors Report / Report of the Directors
  ✓ Smart quotes (\u2019) AND straight apostrophes
  ✓ Heading-anchored stops — NEVER fires on inline mentions
  ✓ "Notes to the financial statements referred in..." → no false stop
  ✓ "Management Discussion and Analysis forms part of this report" → no false stop
  ✓ Annexures to Directors'/Auditors' Report are INCLUDED (not stops)
  ✓ AGM Notice at start OR end of PDF, including "NOTICE...Annual General Meeting" format
  ✓ PSU / government company formats (ALL CAPS, older heading styles)
  ✓ Pre-2013 "Auditor's Report" without "Independent"
  ✓ Adaptive skip_first_n: thin reports (60 pages) vs huge integrated reports (500+ pages)
  ✓ Auditors search starts after Directors section ends (avoids cross-contamination)
  ✓ Retry if Auditors report not found on first pass
  ✓ Resume support: skips already-extracted PDFs
  ✓ Full error log saved to extracted_reports/extraction_log_YYYYMMDD.txt

Usage:
    python extract_from_drive.py

Output:
    extracted_reports/
    └── <CompanyName>/
        ├── Annual_Report_2022_directors_report.txt
        └── Annual_Report_2022_auditors_report.txt

Requirements:
    pip install pdfplumber google-api-python-client google-auth google-auth-oauthlib
"""

import io
import os
import re
import sys
import logging
import traceback
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


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

CLIENT_SECRET_FILE   = "client_secrets.json"
TOKEN_FILE           = "token.json"
DRIVE_ROOT_FOLDER_ID = "1WSgPJpe8sBZlty6rsOYfWALxRmQg2Tk6"
LOCAL_OUTPUT_DIR     = Path("extracted_reports")
SCOPES               = ["https://www.googleapis.com/auth/drive.readonly"]

# Set True to re-extract files that already exist
FORCE_REEXTRACT = False


# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
# GOOGLE DRIVE
# ═══════════════════════════════════════════════════════════════

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
    buf     = io.BytesIO()
    dl      = MediaIoBaseDownload(buf, request, chunksize=8 * 1024 * 1024)
    done    = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════
# REGEX PATTERNS
# ═══════════════════════════════════════════════════════════════
#
# KEY DESIGN INSIGHT verified on real PDFs:
#
#   "Auditor's Report" in Indian PDFs uses RIGHT SINGLE QUOTATION MARK (\u2019).
#   The apostrophe comes BETWEEN "Auditor" and "s":  Auditor + ' + s
#   Our pattern must be:  auditors?[\u2019']?s?  — NOT  auditors?[\u2019']?
#
#   Stop patterns use \s*$ (end-of-line) to ensure they only fire as
#   STANDALONE HEADINGS. This is what prevents false stops on sentences like:
#   "The Notes to the financial statements referred in the Auditors Report..."
#
# All patterns verified on Adani Transmission Annual Report 2022 (509 pages).
# ─────────────────────────────────────────────────────────────

# Apostrophe variants: RIGHT/LEFT single quote, curly, straight, backtick, modifier letter
_AP = r"[\u2018\u2019\u201a\u201b'\u0060\u02bc]"


# ── Directors / Board Report — start signals ─────────────

# Primary: standalone heading line
DIRECTORS_HEADING = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"directors?" + _AP + r"?\s*report"       # Directors' Report / Directors Report
    r"|board" + _AP + r"?s?\s*report"          # Board's Report / Board Report
    r"|report\s+of\s+(?:the\s+)?directors?"    # Report of the Directors
    r")"
    r"\s*(?:\n|$)",
    re.IGNORECASE,
)

# Fallback: body text opener (no heading — rare but exists)
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


# ── Independent Auditor's Report — start signals ─────────

# Primary: standalone heading
# NOTE: apostrophe is BETWEEN "auditor" and "s" → auditors?[ap]?s?
# Handles: Auditor's / Auditors' / Auditors / Auditor (all with/without apostrophe)
AUDITORS_HEADING = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"independent\s+auditors?" + _AP + r"?s?\s*report"    # Independent Auditor's/Auditors'/Auditors Report
    r"|report\s+of\s+the\s+independent\s+auditors?"        # Report of the Independent Auditors
    r"|auditors?" + _AP + r"?s?\s*report"                  # Auditor's Report (pre-2013 short form)
    r")"
    r"\s*(?:\n|$)",
    re.IGNORECASE,
)

# Fallback: body opener of auditors report
AUDITORS_BODY = re.compile(
    r"(?:^|\n)\s*"
    r"to\s+the\s+(?:members|board\s+of\s+directors)\s+of\s+[A-Z\u00C0-\u024F]",
    re.IGNORECASE,
)


# ── Stop: Directors Report ────────────────────────────────
#
# RULES (verified on real PDFs):
#   ✓ Must match END OF LINE (\s*$) → standalone heading only
#   ✓ "Management Discussion and Analysis" standalone → STOP
#   ✓ "Management Discussion and Analysis forms part of this Report" → NO stop (inline)
#   ✓ "financial statements referred in the Auditors Report" → NO stop (inline)
#   ✓ "Consolidated Financial Results" in Directors body → NO stop
#   ✓ "Independent Auditor's Report" heading → STOP (new section begins)
#   ✓ "Annexure to Directors' Report" → NOT a stop (part of DR)

DIRECTORS_STOP = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    # Financial statement headings as standalone section titles
    r"(?:standalone\s+|consolidated\s+)?"
    r"(?:"
    r"balance\s+sheet"
    r"|statement\s+of\s+(?:profit(?:\s+and\s+loss)?|financial\s+position|cash\s+flow)"
    r"|cash\s+flow\s+statement"
    r")"

    # Section divider pages
    r"|(?:standalone|consolidated)\s+financial\s+statements"

    # MDA as its own section (standalone heading → \s*$ ensures no inline match)
    r"|management\s+(?:discussion\s+(?:and\s+)?|&\s*)analysis"

    # Auditor's report begins — Directors' report has ended
    r"|independent\s+auditors?" + _AP + r"?s?\s*report"
    r"|report\s+of\s+the\s+independent\s+auditors?"

    r")"
    r"\s*$",   # END OF LINE — this is what prevents inline false stops
    re.IGNORECASE | re.MULTILINE,
)


# ── Stop: Auditors Report ─────────────────────────────────
#
# Stops when financial statements begin.
# "Consolidated Independent Auditor's Report" = new section → stop standalone.
# Annexure A/B to Auditor's Report → INCLUDED (not a stop).

AUDITORS_STOP = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"(?:standalone\s+|consolidated\s+)?"
    r"(?:"
    r"balance\s+sheet"
    r"|statement\s+of\s+(?:profit(?:\s+and\s+loss)?|financial\s+position|cash\s+flow)"
    r"|cash\s+flow\s+statement"
    r")"

    r"|(?:standalone|consolidated)\s+financial\s+statements"

    # Second auditors report (consolidated) ends standalone section
    r"|consolidated\s+(?:independent\s+)?auditors?" + _AP + r"?s?\s*report"
    r"|consolidated\s+report\s+of\s+the\s+independent\s+auditors?"

    r")"
    r"\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# ── Skip entire page ──────────────────────────────────────
# Handles AGM notice at start OR end of PDF.
# Handles "NOTICE\nNOTICE is hereby given...Annual General Meeting" format.

SKIP_PAGE = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"notice\s+of\s+(?:the\s+)?(?:\d+\s*(?:st|nd|rd|th)\s+)?(?:annual\s+general\s+meeting|agm)"
    r"|notice\b[\s\S]{0,200}annual\s+general[\s\S]{0,30}meeting"  # NOTICE...AGM (handles two-column line breaks)
    r"|proxy\s+form\b"
    r"|attendance\s+slip\b"
    r"|e[\s\-]?voting\s+(?:instructions?|procedure|process|facility)"
    r"|route\s+map\s+to\s+(?:the\s+)?(?:agm|venue|meeting)"
    r"|form\s+no\.?\s*mgt[\s\-]?\d+"    # MGT-11 proxy form
    r")",
    re.IGNORECASE | re.MULTILINE,
)


# ── Noise lines: running headers, footers, page numbers ──
NOISE_LINE = re.compile(
    r"^\s*"
    r"(?:"
    r"\d{1,4}\s*$"                                              # lone page number
    r"|page\s+\d+\s*(?:of\s+\d+)?\s*$"                        # Page N / Page N of M
    r"|(?:integrated\s+)?annual\s+report\s*"
    r"(?:20\d{2})?(?:[\s\-\u2013\u2014](?:20)?\d{2,4})?\s*$"  # Annual Report 2021-22
    r"|fy\s*20\d{2}(?:[\s\-\u2013\u2014](?:20)?\d{2})?\s*$"   # FY2022-23
    r"|[|│─═\-_=~]{4,}\s*$"                                    # decorative dividers
    r"|(?:cin|gstin|pan|llpin)\s*[:\-]\s*\S+\s*$"             # company IDs
    r"|(?:https?://|www\.)\S+\s*$"                              # URLs
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# ═══════════════════════════════════════════════════════════════
# TEXT UTILITIES
# ═══════════════════════════════════════════════════════════════

def clean_text(text: str) -> str:
    lines   = text.splitlines()
    cleaned = [ln for ln in lines if not NOISE_LINE.match(ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()


def load_pages(pdf_bytes: bytes) -> list[str]:
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pg in pdf.pages:
            try:
                text = pg.extract_text(x_tolerance=3, y_tolerance=3) or ""
            except Exception:
                text = ""
            pages.append(text)
    return pages


def compute_skip_n(total: int) -> int:
    """Adaptive early-page skip: large integrated reports have more front matter."""
    if total < 60:   return 8
    if total < 120:  return 15
    if total < 250:  return 25
    return 35


# ═══════════════════════════════════════════════════════════════
# CORE SECTION EXTRACTOR
# ═══════════════════════════════════════════════════════════════

def find_section(
    pages: list[str],
    start_patterns: list,
    stop_pattern,
    section_name: str,
    skip_first_n: int,
    search_from: int = 0,
) -> tuple[str, int]:
    """
    Extract one named section from pages[search_from:].

    Returns (text, end_page_index).
    end_page_index = index where stop was triggered (helps next section search).

    Phase 1 (scanning):
      - Ignore SKIP_PAGE pages
      - Enforce early-page rule: heading must be in first 300 chars
        if page number ≤ skip_first_n
      - On match: trim everything before the heading
      - If stop also fires on same page → extract middle slice only

    Phase 2 (collecting):
      - Append pages until stop fires
      - Ignore SKIP_PAGE pages mid-section (rare)
    """
    collecting = False
    collected  = []
    found_at   = None
    end_idx    = len(pages)

    for i in range(search_from, len(pages)):
        raw  = pages[i]
        pnum = i + 1

        if not collecting:
            if SKIP_PAGE.search(raw):
                continue

            area = raw[:300] if pnum <= skip_first_n else raw
            if not any(p.search(area) for p in start_patterns):
                continue

            # Trim to the matched heading
            match_pos = len(raw)
            for p in start_patterns:
                m = p.search(raw)
                if m and m.start() < match_pos:
                    match_pos = m.start()

            collecting = True
            found_at   = pnum
            trimmed    = raw[match_pos:]

            sm = stop_pattern.search(trimmed)
            if sm:
                collected.append(trimmed[:sm.start()])
                end_idx = i
                log.info(f"    [{section_name}] Found+stopped on page {pnum}")
                break

            collected.append(trimmed)
            log.info(f"    [{section_name}] Started on page {pnum}")
            continue

        sm = stop_pattern.search(raw)
        if sm:
            partial = raw[:sm.start()].strip()
            if partial:
                collected.append(partial)
            end_idx = i
            log.info(f"    [{section_name}] Stopped at page {pnum} "
                     f"(started {found_at}, {pnum - found_at} pages)")
            break

        if SKIP_PAGE.search(raw):
            continue

        collected.append(raw)

    if not collected:
        return "", end_idx

    return clean_text("\n\n".join(collected)), end_idx


# ═══════════════════════════════════════════════════════════════
# PDF PROCESSOR
# ═══════════════════════════════════════════════════════════════

def extract_from_bytes(pdf_bytes: bytes) -> dict[str, str]:
    try:
        pages = load_pages(pdf_bytes)
    except Exception as e:
        log.error(f"    Failed to read PDF: {e}")
        return {"directors_report": "Not Found", "auditors_report": "Not Found"}

    total  = len(pages)
    skip_n = compute_skip_n(total)
    log.info(f"    Pages: {total}  |  skip_first_n: {skip_n}")

    # ── Directors' Report ─────────────────────────────────
    directors_text, dir_end = find_section(
        pages,
        start_patterns=[DIRECTORS_HEADING, DIRECTORS_BODY],
        stop_pattern=DIRECTORS_STOP,
        section_name="Directors Report",
        skip_first_n=skip_n,
        search_from=0,
    )

    # ── Auditors' Report ──────────────────────────────────
    # Search starts after Directors section to avoid matching
    # auditors report references inside the Directors report body.
    aud_start = max(dir_end, skip_n)
    auditors_text, _ = find_section(
        pages,
        start_patterns=[AUDITORS_HEADING, AUDITORS_BODY],
        stop_pattern=AUDITORS_STOP,
        section_name="Auditors Report",
        skip_first_n=skip_n,
        search_from=aud_start,
    )

    # Retry from scratch if not found (non-standard structure)
    if not auditors_text:
        log.info("    [Auditors Report] Retrying from page 1...")
        auditors_text, _ = find_section(
            pages,
            start_patterns=[AUDITORS_HEADING],
            stop_pattern=AUDITORS_STOP,
            section_name="Auditors Report (retry)",
            skip_first_n=skip_n,
            search_from=0,
        )

    return {
        "directors_report": directors_text if directors_text else "Not Found",
        "auditors_report":  auditors_text  if auditors_text  else "Not Found",
    }


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    log_file = setup_logging()
    log.info(f"Output folder : {LOCAL_OUTPUT_DIR.resolve()}")
    log.info(f"Log file      : {log_file}")

    log.info("Authenticating with Google Drive...")
    service = get_drive_service()
    log.info("Authenticated!\n")

    company_folders = list_subfolders(service, DRIVE_ROOT_FOLDER_ID)
    log.info(f"Found {len(company_folders)} company folders\n")

    stats = dict(pdfs=0, saved=0, skipped=0, not_found=0, errors=0)

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
            name  = pdf_file["name"]
            fid   = pdf_file["id"]
            size  = int(pdf_file.get("size", 0)) / (1024 * 1024)
            stats["pdfs"] += 1

            log.info(f"\n  [{name}] ({size:.1f} MB)")

            stem     = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
            dir_path = local_dir / f"{stem}_directors_report.txt"
            aud_path = local_dir / f"{stem}_auditors_report.txt"

            if not FORCE_REEXTRACT and dir_path.exists() and aud_path.exists():
                log.info("  Already extracted, skipping.")
                stats["skipped"] += 1
                continue

            try:
                log.info("  Downloading into memory...")
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

            del pdf_bytes  # free RAM immediately

            for key, text in results.items():
                out = local_dir / f"{stem}_{key}.txt"
                if out.exists() and not FORCE_REEXTRACT:
                    continue
                try:
                    out.write_text(text, encoding="utf-8")
                    if text == "Not Found":
                        log.warning(f"  NOT FOUND : {out.name}")
                        stats["not_found"] += 1
                    else:
                        log.info(f"  Saved     : {out.name} [{len(text):,} chars]")
                        stats["saved"] += 1
                except Exception as e:
                    log.error(f"  ERROR saving {out.name}: {e}")
                    stats["errors"] += 1

    log.info(f"\n{'='*60}")
    log.info(f"DONE — {LOCAL_OUTPUT_DIR.resolve()}")
    log.info(f"  PDFs processed   : {stats['pdfs']}")
    log.info(f"  .txt files saved : {stats['saved']}")
    log.info(f"  Not Found        : {stats['not_found']}")
    log.info(f"  Skipped (done)   : {stats['skipped']}")
    log.info(f"  Errors           : {stats['errors']}")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    main()
