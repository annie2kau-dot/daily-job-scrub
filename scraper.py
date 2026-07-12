import os
import json
import time
from datetime import datetime, timedelta, timezone
import requests
import gspread
from google.oauth2.service_account import Credentials
from dateutil import parser

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
SPREADSHEET_ID = "1fKcgtPY3gzBAEoLChUdfbNYduHLDGSFd7r3WRHJmlS0"

HOURS_WINDOW = 24

# The precise phrases to scan for inside job titles
KEYWORDS = [
    "Customer Success",
    "Client Coordinator",
    "Content Coordinator",
    "Influencer Marketing",
    "UGC"
]

# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------
def is_within_last_24_hours(posted_at_dt: datetime) -> bool:
    """Checks if a datetime object falls within our lookback window."""
    if posted_at_dt.tzinfo is None:
        posted_at_dt = posted_at_dt.replace(tzinfo=timezone.utc)
    else:
        posted_at_dt = posted_at_dt.astimezone(timezone.utc)
        
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=HOURS_WINDOW)
    return posted_at_dt >= cutoff

def title_matches_keyword(title: str) -> bool:
    """Returns True if ANY of our target tracking keywords are in the job title."""
    title_clean = title.lower()
    for kw in KEYWORDS:
        if kw.lower() in title_clean:
            return True
    return False

# ---------------------------------------------------------------------------
# LIVE SOURCE 1: WORKING NOMADS DIRECT DATA FEED
# ---------------------------------------------------------------------------
def fetch_working_nomads() -> list[dict]:
    """Pulls the entire live daily remote feed directly from Working Nomads."""
    matches = []
    # This feed returns all active postings across the entire web index
    base_url = "https://www.workingnomads.com/jobs/api/v2/jobs"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        response = requests.get(base_url, headers=headers, timeout=20)
        response.raise_for_status()
        jobs = response.json()
    except Exception as error:
        print(f"  [Working Nomads] Connection skipped or timed out: {error}")
        return matches

    if not isinstance(jobs, list):
        return matches

    print(f"  [Working Nomads] Processing {len(jobs)} total platform listings...")
    for job in jobs:
        title = job.get("title", "")
        if title_matches_keyword(title):
            date_str = job.get("pub_date") or job.get("created_at")
            if not date_str:
                continue
                
            try:
                posted_at = parser.parse(date_str)
            except Exception:
                continue

            if is_within_last_24_hours(posted_at):
                matches.append({
                    "title": title,
                    "company": job.get("company_name") or "Remote Company",
                    "url": job.get("url") or "",
                    "source": "Working Nomads Network"
                })
    return matches

# ---------------------------------------------------------------------------
# LIVE SOURCE 2: WE WORK REMOTELY DIRECT FEED
# ---------------------------------------------------------------------------
def fetch_we_work_remotely() -> list[dict]:
    """Pulls the direct daily public tracking stream from We Work Remotely."""
    matches = []
    # Hits their open public json collection data path directly
    base_url = "https://weworkremotely.com/api/v1/posts"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        response = requests.get(base_url, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
    except Exception as error:
        print(f"  [We Work Remotely] Connection skipped or timed out: {error}")
        return matches

    jobs = data.get("posts", [])
    print(f"  [We Work Remotely] Processing {len(jobs)} total platform listings...")
    
    for job in jobs:
        title = job.get("title", "")
        if title_matches_keyword(title):
            date_str = job.get("pub_date")
            if not date_str:
                continue
                
            try:
                posted_at = parser.parse(date_str)
            except Exception:
                continue

            if is_within_last_24_hours(posted_at):
                matches.append({
                    "title": title,
                    "company": job.get("company") or "Remote Company",
                    "url": job.get("url") or "",
                    "source": "We Work Remotely"
                })
    return matches

# ---------------------------------------------------------------------------
# CORE PROCESSOR
# ---------------------------------------------------------------------------
def collect_all_matching_jobs() -> list[dict]:
    """Sweeps whole target raw feeds instantly to circumvent IP proxy block limits."""
    all_jobs = []
    
    print("Beginning comprehensive direct web source scan...")
    wn_jobs = fetch_working_nomads()
    print(f"  -> Working Nomads filtered: {len(wn_jobs)} match(es)")
    all_jobs.extend(wn_jobs)
    
    wwr_jobs = fetch_we_work_remotely()
    print(f"  -> We Work Remotely filtered: {len(wwr_jobs)} match(es)")
    all_jobs.extend(wwr_jobs)

    # De-duplicate items sharing identical destinations
    seen_urls = set()
    unique_jobs = []
    for job in all_jobs:
        url = job["url"]
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_jobs.append(job)
            
    return unique_jobs

# ---------------------------------------------------------------------------
# GOOGLE SPREADSHEET EXPORT EXECUTOR
# ---------------------------------------------------------------------------
def save_to_google_sheet(jobs: list[dict]):
    """Connects to Google Sheets and appends unique items to rows safely."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        print("Error: GOOGLE_CREDENTIALS environment variable is empty.")
        return

    try:
        creds_data = json.loads(creds_json)
        scope = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        credentials = Credentials.from_service_account_info(creds_data, scopes=scope)
        client = gspread.authorize(credentials)
        
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
    except Exception as e:
        print(f"Google Sheet connection handshake failed: {e}")
        return

    try:
        existing_urls = set(sheet.col_values(4))
    except Exception:
        existing_urls = set()

    new_rows_count = 0
    for job in jobs:
        if job["url"] in existing_urls:
            continue
            
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row_data = [date_str, job["title"], job["company"], job["url"]]
        sheet.append_row(row_data)
        new_rows_count += 1

    print(f"Successfully appended {new_rows_count} new job tracking rows to Google Sheets.")

def main():
    matching_jobs = collect_all_matching_jobs()
    print(f"Found {len(matching_jobs)} total unique matching job postings.")
    
    if matching_jobs:
        save_to_google_sheet(matching_jobs)
    else:
        print("No matches discovered within the lookback window today.")

if __name__ == "__main__":
    main()
