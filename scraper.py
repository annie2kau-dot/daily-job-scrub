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

KEYWORDS = [
    "Customer Success",
    "Client Coordinator",
    "Content Coordinator",
    "Influencer Marketing",
    "UGC"
]

REQUEST_DELAY_SECONDS = 1

# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------
def is_within_last_24_hours(posted_at_dt: datetime) -> bool:
    """Checks if a datetime object falls within our 24-hour lookback window."""
    if posted_at_dt.tzinfo is None:
        posted_at_dt = posted_at_dt.replace(tzinfo=timezone.utc)
    else:
        posted_at_dt = posted_at_dt.astimezone(timezone.utc)
        
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=HOURS_WINDOW)
    return posted_at_dt >= cutoff

def title_matches_keyword(title: str, keyword: str) -> bool:
    """Performs a case-insensitive check to see if keyword is in the title."""
    return keyword.lower() in title.lower()

# ---------------------------------------------------------------------------
# GLOBAL SCRAPER 1: FINDWORK AGGREGATOR ENGINE (Crawls Company Sites)
# ---------------------------------------------------------------------------
def fetch_findwork_global(keyword: str) -> list[dict]:
    """Scrapes the Findwork global index for remote postings across the web."""
    matches = []
    base_url = "https://findwork.dev/api/jobs/"
    
    params = {
        "search": keyword,
        "remote": "true",
        "sort": "date"
    }
    
    try:
        # Open global feed API endpoint
        response = requests.get(base_url, params=params, timeout=15)
        if response.status_code == 401:
            print("  [Findwork] Requires an API key header. Skipping to next engine...")
            return matches
        response.raise_for_status()
        data = response.json()
    except Exception as error:
        print(f"  [Findwork] Index sweep failed: {error}")
        return matches

    results = data.get("results", [])
    for job in results:
        title = job.get("role", "")
        if not title_matches_keyword(title, keyword):
            continue
            
        date_str = job.get("date_posted")
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
                "url": job.get("url") or job.get("source_url", ""),
                "posted_at": posted_at,
                "source": "Global Web Aggregator"
            })
    return matches

# ---------------------------------------------------------------------------
# GLOBAL SCRAPER 2: THE OPEN REMOTE INDEX (Broad Network Crawl)
# ---------------------------------------------------------------------------
def fetch_open_index_global(keyword: str) -> list[dict]:
    """Scrapes global job indexes collecting remote postings across web networks."""
    matches = []
    base_url = "https://vibrant-remote.workingnomads.com/jobs/api/v2/jobs"
    
    params = {
        "tags": keyword.replace(" ", "").lower(),
        "limit": 50
    }
    
    try:
        response = requests.get(base_url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
    except Exception as error:
        print(f"  [Network Engine] Scrape skipped or throttled: {error}")
        return matches

    if not isinstance(data, list):
        return matches

    for job in data:
        title = job.get("title", "")
        if not title_matches_keyword(title, keyword):
            continue
            
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
                "company": job.get("company_name") or job.get("company", "Unknown"),
                "url": job.get("url") or "",
                "posted_at": posted_at,
                "source": "Web Aggregator Network"
            })
    return matches

# ---------------------------------------------------------------------------
# CORE PROCESSOR
# ---------------------------------------------------------------------------
def collect_all_matching_jobs() -> list[dict]:
    """Runs global sweeps across multiple networks for all requested target fields."""
    all_jobs = []
    for keyword in KEYWORDS:
        print(f"Sweeping global listings for tracking keyword: '{keyword}'...")
        
        engine_1 = fetch_findwork_global(keyword)
        print(f"  Engine A found: {len(engine_1)} direct matches.")
        
        engine_2 = fetch_open_index_global(keyword)
        print(f"  Engine B found: {len(engine_2)} network matches.")
        
        all_jobs.extend(engine_1)
        all_jobs.extend(engine_2)
        time.sleep(REQUEST_DELAY_SECONDS)

    seen_urls = set()
    unique_jobs = []
    for job in all_jobs:
        url = job["url"]
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_jobs.append(job)
            
    return unique_jobs

# ---------------------------------------------------------------------------
# SHEET EXPORT INTEGRATION
# ---------------------------------------------------------------------------
def save_to_google_sheet(jobs: list[dict]):
    """Connects to Google Sheets using the repository Actions Secret token data."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        print("Error: GOOGLE_CREDENTIALS environment secret configuration is missing.")
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
        print(f"Google Workspace auth handshake rejected: {e}")
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

    print(f"Successfully pushed {new_rows_count} verified matching job listings to Google Sheets.")

def main():
    matching_jobs = collect_all_matching_jobs()
    print(f"Completed run. Discovered {len(matching_jobs)} unique listings across all vectors.")
    
    if matching_jobs:
        save_to_google_sheet(matching_jobs)
    else:
        print("No matches popped up on the live web filters over the last 24 hours.")

if __name__ == "__main__":
    main()
