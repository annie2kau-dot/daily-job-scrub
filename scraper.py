"""
find_remote_jobs.py

WHAT THIS SCRIPT DOES
----------------------
1. Fetches remote job listings from two free, public, no-auth-required JSON APIs:
     - Himalayas:      https://himalayas.app/jobs/api
     - RemoteJobs.org:  https://remotejobs.org/api/v1/jobs
2. Keeps only jobs that were POSTED IN THE LAST 24 HOURS.
3. Keeps only jobs whose TITLE contains one of our target keywords:
     "Customer Success", "Client Coordinator", "Content Coordinator",
     "Influencer Marketing", "UGC"
4. Prints a clean, readable text block listing the matches (and saves it
   to a file called job_alerts.txt so GitHub Actions can upload it as an
   artifact, or you can email/read it later).

WHY IT'S STRUCTURED THIS WAY (for beginners)
---------------------------------------------
- Both APIs let us pass our own search keyword ("q=...") in the request.
  We use that to avoid downloading their *entire* job database (which can
  be 100,000+ jobs). It also means we make far fewer API calls.
- BUT: their keyword search can match text in the job DESCRIPTION too, not
  just the title. So after fetching results, we double-check the keyword
  actually appears in the job TITLE before keeping it. This gives us the
  "precise keyword in the title" behavior you asked for.
- We also filter by date ourselves (rather than fully trusting the API),
  because that's the safest way to guarantee only truly-recent jobs show up.

HOW TO RUN THIS ON GITHUB ACTIONS (every 24 hours)
-----------------------------------------------------
Create a file at .github/workflows/job_alert.yml in your repo with:

    name: Daily Remote Job Alert
    on:
      schedule:
        - cron: "0 13 * * *"   # runs every day at 13:00 UTC - edit as you like
      workflow_dispatch: {}     # lets you also trigger it manually
    jobs:
      run-script:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - uses: actions/setup-python@v5
            with:
              python-version: "3.11"
          - run: pip install requests
          - run: python find_remote_jobs.py
          - uses: actions/upload-artifact@v4
            with:
              name: job-alerts
              path: job_alerts.txt

That's it -- no API keys or secrets needed, since both APIs are free and public.
"""

import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# STEP 0: CONFIGURATION
# ---------------------------------------------------------------------------

# The exact phrases we want to match inside a job TITLE.
# Matching is case-insensitive (e.g. "customer success" also matches
# "Customer Success Manager").
KEYWORDS = [
    "Customer Success",
    "Client Coordinator",
    "Content Coordinator",
    "Influencer Marketing",
    "UGC",
]

# Only keep jobs posted within this many hours.
HOURS_WINDOW = 24

# How many result pages to check per keyword, per API, at most.
# This is a safety cap so the script can't accidentally loop forever or
# hammer the API with requests. Since we filter to "last 24 hours" and the
# APIs return newest jobs first, a handful of pages is always more than enough.
MAX_PAGES_PER_KEYWORD = 3

# Be polite to the free APIs: pause briefly between requests.
REQUEST_DELAY_SECONDS = 1

# A standard "who is making this request" header. Some APIs like to see this.
HEADERS = {"User-Agent": "job-alert-script/1.0 (personal automation)"}


# ---------------------------------------------------------------------------
# STEP 1: HELPER FUNCTIONS
# ---------------------------------------------------------------------------

def is_within_last_24_hours(posted_at: datetime) -> bool:
    """
    Returns True if `posted_at` (a timezone-aware datetime) falls within the
    last HOURS_WINDOW hours, compared to right now (UTC).
    """
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=HOURS_WINDOW)
    return posted_at >= cutoff


def title_matches_keyword(title: str, keyword: str) -> bool:
    """
    Case-insensitive check that `keyword` appears somewhere in `title`.
    e.g. title_matches_keyword("Customer Success Manager", "customer success") -> True
    """
    return keyword.lower() in title.lower()


# ---------------------------------------------------------------------------
# STEP 2: FETCH JOBS FROM HIMALAYAS
# ---------------------------------------------------------------------------

def fetch_himalayas_jobs(keyword: str) -> list[dict]:
    """
    Queries the Himalayas search endpoint for a single keyword, sorted by
    most recent first, and returns a list of matching job dicts in our own
    simplified format:
        {"title", "company", "url", "posted_at", "source"}

    Docs: https://himalayas.app/docs/remote-jobs-api
    """
    matches = []
    base_url = "https://himalayas.app/jobs/api/search"

    for page in range(1, MAX_PAGES_PER_KEYWORD + 1):
        params = {
            "q": keyword,
            "sort": "recent",  # newest jobs first, so we can stop early
            "page": page,
        }

        try:
            response = requests.get(base_url, params=params, headers=HEADERS, timeout=15)
            response.raise_for_status()  # raises an error if the request failed
            data = response.json()
        except requests.RequestException as error:
            print(f"  [Himalayas] Request failed for keyword '{keyword}': {error}")
            break

        jobs = data.get("jobs", [])
        if not jobs:
            break  # no more results, stop paginating

        stop_paginating = False

        for job in jobs:
            # Himalayas gives pubDate as a Unix timestamp in MILLISECONDS.
            posted_at = datetime.fromtimestamp(job["pubDate"] / 1000, tz=timezone.utc)

            if not is_within_last_24_hours(posted_at):
                # Since results are sorted newest-first, once we hit a job
                # older than our window, every job after it will be older
                # too -- so we can safely stop looking at more pages.
                stop_paginating = True
                break

            title = job.get("title", "")
            if title_matches_keyword(title, keyword):
                matches.append({
                    "title": title,
                    "company": job.get("companyName", "Unknown company"),
                    "url": job.get("applicationLink", ""),
                    "posted_at": posted_at,
                    "source": "Himalayas",
                })

        if stop_paginating:
            break

        time.sleep(REQUEST_DELAY_SECONDS)

    return matches


# ---------------------------------------------------------------------------
# STEP 3: FETCH JOBS FROM REMOTEJOBS.ORG
# ---------------------------------------------------------------------------

def fetch_remotejobs_org_jobs(keyword: str) -> list[dict]:
    """
    Queries the RemoteJobs.org endpoint for a single keyword and returns a
    list of matching job dicts in our own simplified format.

    Docs: https://remotejobs.org/api-access
    """
    matches = []
    base_url = "https://remotejobs.org/api/v1/jobs"
    limit = 50  # max allowed per their docs

    for page in range(MAX_PAGES_PER_KEYWORD):
        offset = page * limit
        params = {
            "q": keyword,
            "limit": limit,
            "offset": offset,
        }

        try:
            response = requests.get(base_url, params=params, headers=HEADERS, timeout=15)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as error:
            print(f"  [RemoteJobs.org] Request failed for keyword '{keyword}': {error}")
            break

        jobs = data.get("data", [])
        if not jobs:
            break

        for job in jobs:
            # RemoteJobs.org gives posted_at as an ISO 8601 string, e.g.
            # "2026-04-05T00:00:00Z". Python can parse this once we swap
            # the trailing "Z" for "+00:00" (which means UTC).
            posted_at_str = job.get("posted_at")
            if not posted_at_str:
                continue

            from dateutil import parser
posted_at = parser.parse(posted_at_str)

            if not is_within_last_24_hours(posted_at):
                continue  # this API isn't guaranteed sorted, so just skip, don't stop

            title = job.get("title", "")
            if title_matches_keyword(title, keyword):
                company_info = job.get("company", {})
                matches.append({
                    "title": title,
                    "company": company_info.get("name", "Unknown company"),
                    "url": job.get("apply_url") or job.get("url", ""),
                    "posted_at": posted_at,
                    "source": "RemoteJobs.org",
                })

        # Check if there are more pages worth fetching.
        pagination = data.get("pagination", {})
        if not pagination.get("has_more", False):
            break

        time.sleep(REQUEST_DELAY_SECONDS)

    return matches


# ---------------------------------------------------------------------------
# STEP 4: PUT IT ALL TOGETHER
# ---------------------------------------------------------------------------

def collect_all_matching_jobs() -> list[dict]:
    """
    Loops over every keyword, queries both APIs, and returns one combined,
    de-duplicated list of matching jobs.
    """
    all_jobs = []

    for keyword in KEYWORDS:
        print(f"Searching for jobs matching keyword: '{keyword}'...")

        himalayas_jobs = fetch_himalayas_jobs(keyword)
        print(f"  Himalayas: {len(himalayas_jobs)} match(es) found")

        remotejobs_org_jobs = fetch_remotejobs_org_jobs(keyword)
        print(f"  RemoteJobs.org: {len(remotejobs_org_jobs)} match(es) found")

        all_jobs.extend(himalayas_jobs)
        all_jobs.extend(remotejobs_org_jobs)

    # De-duplicate: the same job could show up twice if it matches more than
    # one keyword (e.g. a title containing both "UGC" and "Content Coordinator"),
    # or if it appears from a query overlap. We treat (title, company, url) as
    # a unique fingerprint for a job.
    seen = set()
    unique_jobs = []
    for job in all_jobs:
        fingerprint = (job["title"], job["company"], job["url"])
        if fingerprint not in seen:
            seen.add(fingerprint)
            unique_jobs.append(job)

    # Sort newest-first so the most recent postings appear at the top.
    unique_jobs.sort(key=lambda j: j["posted_at"], reverse=True)

    return unique_jobs


# ---------------------------------------------------------------------------
# STEP 5: FORMAT THE RESULTS INTO A NEAT TEXT BLOCK
# ---------------------------------------------------------------------------

def format_jobs_as_text(jobs: list[dict]) -> str:
    """
    Turns a list of job dicts into a readable, plain-text summary block.
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not jobs:
        return (
            f"Remote Job Alert - {now_str}\n"
            f"{'=' * 60}\n"
            f"No matching jobs were posted in the last {HOURS_WINDOW} hours.\n"
        )

    lines = [
        f"Remote Job Alert - {now_str}",
        "=" * 60,
        f"Found {len(jobs)} matching job(s) posted in the last {HOURS_WINDOW} hours:",
        "",
    ]

    for index, job in enumerate(jobs, start=1):
        posted_str = job["posted_at"].strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"{index}. {job['title']}")
        lines.append(f"   Company: {job['company']}")
        lines.append(f"   Posted:  {posted_str}")
        lines.append(f"   Source:  {job['source']}")
        lines.append(f"   Apply:   {job['url']}")
        lines.append("")  # blank line between jobs

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# STEP 6: MAIN ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    matching_jobs = collect_all_matching_jobs()
    report = format_jobs_as_text(matching_jobs)

    # Print to the console/GitHub Actions log so you can see it immediately.
    print("\n" + report)

    # Also save to a text file. In GitHub Actions, you can upload this file
    # as a workflow artifact (see the docstring at the top of this file),
    # or extend this script later to email/Slack it to yourself.
    with open("job_alerts.txt", "w", encoding="utf-8") as f:
        f.write(report)


if __name__ == "__main__":
    main()
