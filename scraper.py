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
# FIXED: Clean isolated spreadsheet key ID string
SPREADSHEET_ID = "1fKcgtPY3gzBAEoLChUdfbNYduHLDGSFd7r3WRHJmlS0"

# How far back to look for jobs (in hours)
HOURS_WINDOW = 24

# The precise keywords to match in job titles
KEYWORDS = [
    "Customer Success",
    "Client Coordinator",
    "Content Coordinator",
    "Influencer Marketing",
    "UGC"
]

# Delay between API requests to be polite to servers
REQUEST_DELAY_SECONDS = 2

# Maximum pages to traverse per keyword query to prevent infinite loops
MAX_PAGES_PER_KEYWORD = 5

# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------
def is_within_last_24_hours(posted_at_dt: datetime) -> bool:
    """Checks if a datetime object falls within our lookback window."""
    # Ensure posted_at_dt is timezone-aware in UTC to prevent TypeError comparisons
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
# STEP 1: FETCH FROM HIMALAYAS API
# ---------------------------------------------------------------------------
def fetch_himalayas_jobs(keyword: str) -> list[dict]:
    """Queries the Himalayas endpoint and returns structured matches."""
    matches = []
    base_url = "https://himalayas.app"
    
    for page in range(1, MAX_PAGES_PER_KEYWORD + 1):
        params = {
            "query": keyword,
            "page": page
        }
        try:
            response = requests.get(base_url, params=params, timeout=15)
            if response.status_code == 404:
                break
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as error:
            print(f"  [Himalayas] Request failed for keyword '{keyword}': {error}")
            break

        jobs = data.get("jobs", [])
        if not jobs:
            break

        for job in jobs:
            pub_date_timestamp = job.get("pub_date")
            if not pub_date_timestamp:
                continue
            
            try:
                posted_at = datetime.fromtimestamp(int(pub_date_timestamp), tz=timezone.utc)
            except (ValueError, TypeError):
                continue

            if not is_within_last_24_hours(posted_at):
                # Himalayas is typically sorted newest first; we can halt parsing safely here
                break

            title = job.get("title", "")
            if title_matches_keyword(title, keyword):
                matches.append({
                    "title": title,
                    "company": job.get("company", {}).get("name", "Unknown company"),
                    "url": job.get("application_link") or job.get("link", ""),
                    "posted_at": posted_at,
                    "source": "Himalayas"
                })
        
        time.sleep(REQUEST_DELAY_SECONDS)
    return matches

# ---------------------------------------------------------------------------
# STEP 2: FETCH FROM REMOTEJOBS.ORG API
# ---------------------------------------------------------------------------
def fetch_remotejobs_org_jobs(keyword: str) -> list[dict]:
    """Queries the RemoteJobs.org endpoint and returns structured matches."""
    matches = []
    base_url = "https://remotejobs.org"
    limit = 50
    
    for page in range(MAX_PAGES_PER_KEYWORD):
        offset = page * limit
        params = {
            "q": keyword,
            "limit": limit,
            "offset": offset
        }
        try:
            response = requests.get(base_url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as error:
            print(f"  [RemoteJobs.org] Request failed for keyword '{keyword}': {error}")
            break

        jobs = data.get("data", [])
        if not jobs:
            break

        for job in jobs:
            posted_at_str = job.get("posted_at")
            if not posted_at_str:
                continue

            try:
                posted_at = parser.parse(posted_at_str)
            except Exception:
                continue

            if not is_within_last_24_hours(posted_at):
                continue  # Skip entry but continue looking

            title = job.get("title", "")
            if title_matches_keyword(title, keyword):
                company_info = job.get("company", {})
                matches.append({
                    "title": title,
                    "company": company_info.get("name", "Unknown company"),
                    "url": job.get("apply_url") or job.get("url", ""),
                    "posted_at": posted_at,
                    "source": "RemoteJobs.org"
                })
        
        pagination = data.get("pagination", {})
        if not pagination.get("has_more", False):
            break
            
        time.sleep(REQUEST_DELAY_SECONDS)
    return matches

# ---------------------------------------------------------------------------
# STEP 3: COMBINE AND DE-DUPLICATE
# ---------------------------------------------------------------------------
def collect_all_matching_jobs() -> list[dict]:
    """Runs search over all keywords and returns a de-duplicated combined list."""
    all_jobs = []
    for keyword in KEYWORDS:
        print(f"Searching for jobs matching keyword: '{keyword}'...")
        h_jobs = fetch_himalayas_jobs(keyword)
        print(f"  Himalayas: {len(h_jobs)} match(es) found")
        
        r_jobs = fetch_remotejobs_org_jobs(keyword)
        print(f"  RemoteJobs.org: {len(r_jobs)} match(es) found")
        
        all_jobs.extend(h_jobs)
        all_jobs.extend(r_jobs)

    seen_urls = set()
    unique_jobs = []
    for job in all_jobs:
        url = job["url"]
        if url not in seen_urls:
            seen_urls.add(url)
            unique_jobs.append(job)
            
    return unique_jobs

# ---------------------------------------------------------------------------
# STEP 4: WRITE DATA TO GOOGLE SHEETS
# ---------------------------------------------------------------------------
def save_to_google_sheet(jobs: list[dict]):
    """Connects to Google Sheets using Actions Secret and appends unique items."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        print("Error: GOOGLE_CREDENTIALS environment variable is empty.")
        return

    try:
        creds_data = json.loads(creds_json)
        # FIXED: Correct and precise Google API target scopes
        scope = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        credentials = Credentials.from_service_account_info(creds_data, scopes=scope)
        client = gspread.authorize(credentials)
        
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
    except Exception as e:
        print(f"Google Sheet authentication or connection failed: {e}")
        return

    # Extract existing URLs from Column D safely to prevent duplicates
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

# ---------------------------------------------------------------------------
# MAIN ENTRY POINT
# ---------------------------------------------------------------------------
def main():
    matching_jobs = collect_all_matching_jobs()
    print(f"Found {len(matching_jobs)} total unique matching job postings.")
    
    if matching_jobs:
        save_to_google_sheet(matching_jobs)
    else:
        print("No matches discovered within the lookback window today.")

if __name__ == "__main__":
    main()
