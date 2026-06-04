"""
fetch_insights.py

Fetches Google Business Profile insights (search impressions, direction requests,
phone calls) for all properties over the last 18 months and saves to insights_data.json.

Run locally:   python fetch_insights.py
Run in CI:     the GitHub Actions workflow runs this every Monday at 09:00 UTC.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
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

METRICS = [
    "QUERIES_DIRECT",
    "QUERIES_INDIRECT",
    "ACTIONS_PHONE",
    "ACTIONS_DRIVING_DIRECTIONS",
]


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
    """Return (start, end) covering the last 18 months, aligned to month boundaries."""
    now = datetime.now(timezone.utc)
    # End = start of the current month
    end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Start = 18 months back
    month = end.month - 18
    year = end.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    start = end.replace(year=year, month=month)
    return start, end


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def fetch_insights_batch(session, account_id: str, location_names: list, start_time, end_time) -> list:
    """Fetch insights for up to 10 locations in one API call."""
    url = f"https://mybusiness.googleapis.com/v4/{account_id}/locations:reportInsights"

    payload = {
        "locationNames": location_names,
        "basicRequest": {
            "metricRequests": [
                {"metric": m, "options": ["AGGREGATED_MONTHLY"]}
                for m in METRICS
            ],
            "timeRange": {
                "startTime": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime":   end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        },
    }

    resp = session.post(url, json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"  Insights API error {resp.status_code}: {resp.text[:300]}")
        return []

    return resp.json().get("locationMetrics", [])


def parse_location_metrics(location_metrics: dict) -> list:
    """Parse one location's metricValues into a list of monthly dicts."""
    monthly = {}

    for metric_data in location_metrics.get("metricValues", []):
        metric = metric_data.get("metric", "")
        for dim_val in metric_data.get("dimensionalValues", []):
            time_range = dim_val.get("timeDimension", {}).get("timeRange", {})
            start_str  = time_range.get("startTime", "")[:7]   # "YYYY-MM"
            value      = int(dim_val.get("value", 0) or 0)

            if start_str not in monthly:
                monthly[start_str] = {
                    "month": start_str,
                    "search_impressions": 0,
                    "direction_requests": 0,
                    "calls": 0,
                }

            if metric in ("QUERIES_DIRECT", "QUERIES_INDIRECT"):
                monthly[start_str]["search_impressions"] += value
            elif metric == "ACTIONS_DRIVING_DIRECTIONS":
                monthly[start_str]["direction_requests"] += value
            elif metric == "ACTIONS_PHONE":
                monthly[start_str]["calls"] += value

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
    branch   = config["github"]["branch"]
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
    config = load_config()
    last_updated = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    print("Authenticating with Google Business Profile API...")
    creds   = get_google_credentials()
    session = AuthorizedSession(creds)

    start_time, end_time = get_date_range()
    print(f"Date range: {start_time.strftime('%b %Y')} → {end_time.strftime('%b %Y')}\n")

    # Only process properties with a location ID
    valid_props = [p for p in config["properties"] if p.get("google_location_id")]
    if not valid_props:
        sys.exit("ERROR: No properties with google_location_id found in config.json")

    # Extract account ID from the first location path
    account_id = valid_props[0]["google_location_id"].split("/locations/")[0]

    # Batch API calls — max 10 locations per request
    all_metrics = {}
    batch_size  = 10
    location_ids = [p["google_location_id"] for p in valid_props]

    for i in range(0, len(location_ids), batch_size):
        batch = location_ids[i : i + batch_size]
        print(f"Fetching batch {i // batch_size + 1} ({len(batch)} locations)...", end=" ", flush=True)
        metrics = fetch_insights_batch(session, account_id, batch, start_time, end_time)
        for m in metrics:
            all_metrics[m["locationName"]] = m
        print(f"{len(metrics)} returned")
        time.sleep(1)

    # Build output
    output = {
        "last_updated": last_updated,
        "date_range": {
            "start": start_time.strftime("%Y-%m"),
            "end":   end_time.strftime("%Y-%m"),
        },
        "properties": [],
    }

    for prop in config["properties"]:
        loc_id  = prop.get("google_location_id", "")
        monthly = []
        if loc_id and loc_id in all_metrics:
            monthly = parse_location_metrics(all_metrics[loc_id])
            total_impr = sum(m["search_impressions"] for m in monthly)
            print(f"  {prop['name']}: {total_impr:,} search impressions")
        else:
            print(f"  {prop['name']}: no data")

        output["properties"].append({
            "name":    prop["name"],
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
