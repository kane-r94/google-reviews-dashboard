"""
fetch_insights.py

Fetches Google Business Profile insights (search impressions, direction requests,
phone calls) for all properties using the Business Profile Performance API v1.
Data is aggregated monthly over the last 18 months and saved to insights_data.json.

Run locally:   python fetch_insights.py
Run in CI:     the GitHub Actions workflow runs this every Monday at 09:00 UTC.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, date
from pathlib import Path

from github import Github
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request, AuthorizedSession

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
CREDENTIALS_DIR = ROOT / "credentials"
OAUTH_CLIENT_FILE = CREDENTIALS_DIR / "oauth_client.json"
TOKEN_FILE = CREDENTIALS_DIR / "token.json"
INSIGHTS_JSON_PATH = ROOT / "insights_data.json"

SCOPES = ["https://www.googleapis.com/auth/business.manage"]

BASE_URL = "https://businessprofileperformance.googleapis.com/v1"

DAILY_METRICS = [
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS",
    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS",
    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
    "CALL_CLICKS",
    "BUSINESS_DIRECTION_REQUESTS",
]

# Metrics that contribute to "search impressions" total
IMPRESSION_METRICS = {
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS",
    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS",
    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
}


# ---------------------------------------------------------------------------
# Config & auth
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_google_credentials() -> Credentials:
    creds = None

    token_env = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_env:
        import base64
        token_data = json.loads(base64.b64decode(token_env).decode("utf-8"))
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    elif TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not OAUTH_CLIENT_FILE.exists():
                sys.exit(
                    f"\nERROR: OAuth client file not found at {OAUTH_CLIENT_FILE}\n"
                    "See README.md for setup instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(OAUTH_CLIENT_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.parent.mkdir(exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds


# ---------------------------------------------------------------------------
# Date range
# ---------------------------------------------------------------------------

def get_date_range():
    """Return (start_date, end_date) as date objects covering the last 18 months."""
    today = date.today()
    # Start = first day of the month 18 months ago
    month = today.month - 18
    year  = today.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    start = date(year, month, 1)
    return start, today


# ---------------------------------------------------------------------------
# API calls — Business Profile Performance API v1
# ---------------------------------------------------------------------------

def fetch_insights_for_location(session, location_id_numeric: str, start_date, end_date) -> dict | None:
    """
    Fetch daily metrics for a single location.
    location_id_numeric: numeric portion of the location ID only, e.g. "3927651401574716321"
    """
    url = (
        f"{BASE_URL}/locations/{location_id_numeric}"
        f":fetchMultiDailyMetricsTimeSeries"
    )

    # Build query string manually — multiple values for dailyMetrics
    parts = [f"dailyMetrics={m}" for m in DAILY_METRICS]
    parts += [
        f"dailyRange.startDate.year={start_date.year}",
        f"dailyRange.startDate.month={start_date.month}",
        f"dailyRange.startDate.day={start_date.day}",
        f"dailyRange.endDate.year={end_date.year}",
        f"dailyRange.endDate.month={end_date.month}",
        f"dailyRange.endDate.day={end_date.day}",
    ]

    full_url = url + "?" + "&".join(parts)

    try:
        resp = session.get(full_url, timeout=30)
        if resp.status_code != 200:
            print(f"  API error {resp.status_code}: {resp.text[:200]}")
            return None
        return resp.json()
    except Exception as e:
        print(f"  Request error: {e}")
        return None


def parse_daily_to_monthly(response_data: dict) -> list:
    """Aggregate daily API response into a sorted list of monthly dicts."""
    monthly = {}

    # Response structure: multiDailyMetricTimeSeries → dailyMetricTimeSeries → metrics
    for outer in (response_data or {}).get("multiDailyMetricTimeSeries", []):
        for series in outer.get("dailyMetricTimeSeries", []):
            metric     = series.get("dailyMetric", "")
            dated_vals = series.get("timeSeries", {}).get("datedValues", [])

            for item in dated_vals:
                d     = item.get("date", {})
                yr    = d.get("year",  0)
                mo    = d.get("month", 0)
                value = int(item.get("value", 0) or 0)

                key = f"{yr}-{str(mo).zfill(2)}"
                if key not in monthly:
                    monthly[key] = {"month": key, "search_impressions": 0,
                                    "direction_requests": 0, "calls": 0}

                if metric in IMPRESSION_METRICS:
                    monthly[key]["search_impressions"] += value
                elif metric == "BUSINESS_DIRECTION_REQUESTS":
                    monthly[key]["direction_requests"] += value
                elif metric == "CALL_CLICKS":
                    monthly[key]["calls"] += value

    return sorted(monthly.values(), key=lambda x: x["month"])


# ---------------------------------------------------------------------------
# GitHub push
# ---------------------------------------------------------------------------

def push_insights_json(config: dict, json_content: str, last_updated: str):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("No GITHUB_TOKEN found — skipping GitHub push (local run).")
        return

    gh = Github(token)
    repo = gh.get_repo(f"{config['github']['repo_owner']}/{config['github']['repo_name']}")
    branch    = config["github"]["branch"]
    file_path = "insights_data.json"

    try:
        contents = repo.get_contents(file_path, ref=branch)
        repo.update_file(
            path=file_path,
            message=f"chore: update insights ({last_updated})",
            content=json_content,
            sha=contents.sha,
            branch=branch,
        )
    except Exception:
        repo.create_file(
            path=file_path,
            message=f"chore: create insights ({last_updated})",
            content=json_content,
            branch=branch,
        )
    print(f"Pushed insights_data.json to GitHub ({config['github']['repo_name']})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Insights Fetcher ===\n")
    config       = load_config()
    last_updated = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    print("Authenticating with Google Business Profile API...")
    creds   = get_google_credentials()
    session = AuthorizedSession(creds)

    start_date, end_date = get_date_range()
    print(f"Date range: {start_date.strftime('%b %Y')} → {end_date.strftime('%b %Y')}\n")

    output = {
        "last_updated": last_updated,
        "date_range": {
            "start": start_date.strftime("%Y-%m"),
            "end":   end_date.strftime("%Y-%m"),
        },
        "properties": [],
    }

    for prop in config["properties"]:
        name    = prop["name"]
        loc_id  = prop.get("google_location_id", "")
        monthly = []

        if not loc_id:
            print(f"  {name}: skipped (no location ID)")
        else:
            # Extract just the numeric ID: "accounts/.../locations/1234" → "1234"
            numeric_id = loc_id.split("/locations/")[-1]
            print(f"  Fetching: {name}...", end=" ", flush=True)

            data = fetch_insights_for_location(session, numeric_id, start_date, end_date)

            monthly = parse_daily_to_monthly(data)

            total_impr  = sum(m["search_impressions"] for m in monthly)
            total_dirs  = sum(m["direction_requests"]  for m in monthly)
            total_calls = sum(m["calls"]               for m in monthly)

            if total_impr or total_dirs or total_calls:
                print(f"{total_impr:,} impressions, {total_dirs:,} directions, {total_calls:,} calls")
            else:
                print("no data returned")

            time.sleep(0.3)   # polite pacing

        output["properties"].append({
            "name":    name,
            "region":  prop["region"],
            "monthly": monthly,
        })

    json_content = json.dumps(output, indent=2, ensure_ascii=False)
    INSIGHTS_JSON_PATH.write_text(json_content, encoding="utf-8")
    print(f"\nSaved to {INSIGHTS_JSON_PATH}")

    push_insights_json(config, json_content, last_updated)
    print("\nDone!")


if __name__ == "__main__":
    main()
