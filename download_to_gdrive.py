import asyncio
import io
import re
from pathlib import Path

import requests
from playwright.async_api import async_playwright
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ================= CONFIG =================

CLIENT_SECRET_FILE = "client_secrets.json"  # rename your downloaded file to this
TOKEN_FILE = "token.json"  # auto-created after first login

SCREENER_EMAIL = "ashutoshpnd463@gmail.com"       # <-- fill in
SCREENER_PASSWORD = "9450421800abc"             # <-- fill in

# Google Drive: use a service account JSON key file
# OR switch to OAuth — see comments below
# The Google Drive folder ID where reports will be uploaded
# Get this from the URL: drive.google.com/drive/folders/THIS_PART
DRIVE_ROOT_FOLDER_ID = "1WSgPJpe8sBZlty6rsOYfWALxRmQg2Tk6"  # <-- fill in

SCOPES = ["https://www.googleapis.com/auth/drive.file"]



# How many years back to download (current year minus this many)
import datetime
CURRENT_YEAR = datetime.datetime.now().year
TARGET_YEARS = list(range(CURRENT_YEAR - 10, CURRENT_YEAR + 1))  # last 10 years

SYMBOLS = {
#   "AU Small Finance Bank": "AUBANK",
#   "Bandhan Bank": "BANDHANBNK",
#   "Federal Bank": "FEDERALBNK",
#   "IDFC First Bank": "IDFCFIRSTB",
#   "RBL Bank": "RBLBANK",
#   "Yes Bank": "YESBANK",
#   "Punjab National Bank": "PNB",
#   "Bank of Baroda": "BANKBARODA",

#   "Mphasis": "MPHASIS",
#   "Persistent Systems": "PERSISTENT",
#   "Coforge": "COFORGE",
#   "L&T Technology Services": "LTTS",
#   "Oracle Financial Services Software": "OFSS",

#   "Siemens India": "SIEMENS",
#   "ABB India": "ABB",
#   "Cummins India": "CUMMINSIND",
#   "Thermax": "THERMAX",
#   "SKF India": "SKFINDIA",
#   "Bharat Forge": "BHARATFORG",
#   "AIA Engineering": "AIAENG",

#   "GAIL (India)": "GAIL",
#   "Petronet LNG": "PETRONET",
#   "NHPC": "NHPC",
#   "SJVN": "SJVN",
#   "Tata Power": "TATAPOWER",

#   "Bosch": "BOSCHLTD",
    "Motherson Sumi Wiring India": "MSUMI",
    "Exide Industries": "EXIDEIND",
    "Amara Raja Energy & Mobility": "ARE&M",

    "Dabur India": "DABUR",
    "Godrej Consumer Products": "GODREJCP",
    "Marico": "MARICO",
    "Colgate-Palmolive India": "COLPAL",
    "Tata Coffee": "TATACOFFEE",

    "DLF": "DLF",
    "Godrej Properties": "GODREJPROP",
    "Oberoi Realty": "OBEROIRLTY",
    "Prestige Estates Projects": "PRESTIGE",

    "Aurobindo Pharma": "AUROPHARMA",
    "Torrent Pharmaceuticals": "TORNTPHARM",
    "Alkem Laboratories": "ALKEM",
    "Lupin": "LUPIN",
    "Glenmark Pharmaceuticals": "GLENMARK",

    "Page Industries": "PAGEIND",
    "PI Industries": "PIIND",
    "SRF": "SRF",
    "Deepak Nitrite": "DEEPAKNTR",
    "Balkrishna Industries": "BALKRISIND"
}


# ================= YEAR MATCHING =================

def extract_year_from_text(text: str):
    """
    Returns the fiscal year end (e.g. 2020 for FY2019-20 or Annual Report 2020).
    Returns None if no year found.
    """
    text_lower = text.lower()

    # Match patterns like "2019-20", "2020-21", "fy2019-20", "fy 2020-21"
    m = re.search(r"(\d{4})[–\-](\d{2,4})", text_lower)
    if m:
        end_year = m.group(2)
        if len(end_year) == 2:
            end_year = str(int(m.group(1)[:2]) * 100 + int(end_year))
        return int(end_year)

    # Match plain year like "Annual Report 2020"
    m = re.search(r"\b(20\d{2})\b", text_lower)
    if m:
        return int(m.group(1))

    return None


def is_target_year(text: str) -> tuple[bool, int | None]:
    year = extract_year_from_text(text)
    if year and year in TARGET_YEARS:
        return True, year
    return False, None


# ================= GOOGLE DRIVE =================
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import os


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

def get_or_create_folder(service, name: str, parent_id: str) -> str:
    """Returns folder ID, creating it if it doesn't exist."""
    query = (
        f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def upload_pdf_to_drive(service, pdf_bytes: bytes, filename: str, folder_id: str):
    """Upload PDF bytes directly to a Drive folder."""
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaIoBaseUpload(
        io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=True
    )
    file = service.files().create(
        body=file_metadata, media_body=media, fields="id, name, webViewLink"
    ).execute()
    print(f"  Uploaded to Drive → {file.get('webViewLink')}")
    return file


# ================= SCREENER LOGIN =================

async def login_screener(page):
    print("Logging in to Screener...")
    await page.goto("https://www.screener.in/login/", timeout=30000)
    await page.fill('input[name="username"]', SCREENER_EMAIL)
    await page.fill('input[name="password"]', SCREENER_PASSWORD)
    await page.click('button[type="submit"]')
    await page.wait_for_timeout(3000)

    # Verify login success by checking URL or page content
    if "login" in page.url:
        raise Exception("Login failed — check your credentials")
    print("Login successful!")


# ================= PDF DOWNLOAD =================

def fetch_pdf_bytes(url: str, referer: str, cookies: dict) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": referer,
    }
    response = requests.get(
        url, headers=headers, cookies=cookies, stream=True, timeout=60
    )
    response.raise_for_status()
    return response.content


# ================= MAIN PROCESSOR =================

async def process_company(page, company, symbol, drive_service):
    print(f"\n{'='*50}")
    print(f"Processing: {company} ({symbol})")

    url = f"https://www.screener.in/company/{symbol}/"
    await page.goto(url, timeout=45000)
    await page.wait_for_timeout(2000)

    # Extract browser cookies to pass to requests for authenticated downloads
    cookies_list = await page.context.cookies()
    cookies = {c["name"]: c["value"] for c in cookies_list}

    links = page.locator("#documents a, .documents a")
    count = await links.count()
    print(f"  Found {count} document links")

    # Collect all matching links first (year → url)
    matched = {}
    for i in range(count):
        link = links.nth(i)
        text = (await link.inner_text()).strip()
        href = await link.get_attribute("href")

        if not href:
            continue

        hit, year = is_target_year(text)
        if hit and year not in matched:
            matched[year] = (href, text)

    if not matched:
        print("  No reports found for target years")
        return

    print(f"  Matched years: {sorted(matched.keys())}")

    # Get or create company folder on Drive
    company_folder_name = company.replace("/", "-")
    company_folder_id = get_or_create_folder(
        drive_service, company_folder_name, DRIVE_ROOT_FOLDER_ID
    )

    for year, (href, link_text) in sorted(matched.items()):
        # Resolve relative URLs
        if href.startswith("//"):
            target_url = "https:" + href
        elif href.startswith("/"):
            target_url = "https://www.screener.in" + href
        else:
            target_url = href

        filename = f"Annual_Report_{year}.pdf"
        print(f"  Downloading {year}: {link_text[:60]}...")

        try:
            pdf_bytes = fetch_pdf_bytes(target_url, url, cookies)
            size_mb = len(pdf_bytes) / (1024 * 1024)
            print(f"  Downloaded {size_mb:.2f} MB → uploading to Drive...")
            upload_pdf_to_drive(drive_service, pdf_bytes, filename, company_folder_id)
        except Exception as e:
            print(f"  Error for {year}: {e}")


# ================= ENTRY POINT =================

async def main():
    drive_service = get_drive_service()
    print(f"Target years: {TARGET_YEARS}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await login_screener(page)

        for company, symbol in SYMBOLS.items():
            try:
                await process_company(page, company, symbol, drive_service)
            except Exception as e:
                print(f"  Error processing {company}: {e}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())