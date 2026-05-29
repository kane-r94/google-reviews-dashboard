"""
fetch_review_text.py

Fetches the full text of every Google review for all 26 properties and saves
them to reviews_data.json. The reviews.html page reads this file to display
a filterable, searchable reviews feed.

Run locally:   python fetch_review_text.py
Run in CI:     the GitHub Actions workflow runs this daily at 06:00 UTC.
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
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
CREDENTIALS_DIR = ROOT / "credentials"
OAUTH_CLIENT_FILE = CREDENTIALS_DIR / "oauth_client.json"
TOKEN_FILE = CREDENTIALS_DIR / "token.json"
REVIEWS_JSON_PATH = ROOT / "reviews_data.json"

SCOPES = ["https://www.googleapis.com/auth/business.manage"]

# Map API star rating strings to integers
STAR_MAP = {
    "ONE": 1,
    "TWO": 2,
    "THREE": 3,
    "FOUR": 4,
    "FIVE": 5,
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
# Google Business Profile reviews
# ---------------------------------------------------------------------------

def build_reviews_service(creds: Credentials):
    """
    Reviews are fetched from the v4 mybusiness API, which is the version
    that exposes the reviews endpoint with full text.
    """
    return build(
        "mybusiness",
        "v4",
        credentials=creds,
        discoveryServiceUrl=(
            "https://mybusiness.googleapis.com/$discovery/rest?version=v4"
        ),
    )


def fetch_all_reviews_for_location(service, location_id: str) -> list:
    """
    Fetch every review for a location, following pagination.
    Returns a list of review dicts.
    """
    reviews = []
    page_token = None

    while True:
        try:
            params = {
                "parent": location_id,
                "pageSize": 50,
                "orderBy": "updateTime desc",
            }
            if page_token:
                params["pageToken"] = page_token

            response = service.accounts().locations().reviews().list(
                **params
            ).execute()

            page_reviews = response.get("reviews", [])
            reviews.extend(page_reviews)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

            time.sleep(0.5)  # polite pacing between pages

        except HttpError as e:
            print(f"  API error fetching reviews for {location_id}: {e}")
            break
        except Exception as e:
            print(f"  Unexpected error for {location_id}: {e}")
            break

    return reviews


def parse_review(raw: dict) -> dict:
    """Normalise a raw API review into a clean dict for storage."""
    star_str = raw.get("starRating", "")
    stars = STAR_MAP.get(star_str, 0)

    reviewer = raw.get("reviewer", {})
    reviewer_name = reviewer.get("displayName", "Anonymous")
    is_anonymous = reviewer.get("isAnonymous", False)
    if is_anonymous:
        reviewer_name = "Anonymous"

    comment = raw.get("comment", "").strip()
    create_time = raw.get("createTime", "")
    update_time = raw.get("updateTime", create_time)

    # Format the date nicely (e.g. "14 May 2025")
    display_date = ""
    try:
        dt = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
        display_date = dt.strftime("%-d %b %Y")
    except Exception:
        try:
            # Windows-compatible fallback (%-d not supported on Windows)
            dt = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
            display_date = dt.strftime("%d %b %Y").lstrip("0")
        except Exception:
            display_date = create_time[:10]

    reply = raw.get("reviewReply", {})
    reply_text = reply.get("comment", "").strip() if reply else ""
    reply_date = ""
    if reply and reply.get("updateTime"):
        try:
            dt2 = datetime.fromisoformat(reply["updateTime"].replace("Z", "+00:00"))
            reply_date = dt2.strftime("%d %b %Y").lstrip("0")
        except Exception:
            reply_date = reply["updateTime"][:10]

    return {
        "reviewer": reviewer_name,
        "stars": stars,
        "text": comment,
        "date": display_date,
        "raw_date": create_time,
        "has_reply": bool(reply_text),
        "reply_text": reply_text,
        "reply_date": reply_date,
    }


# ---------------------------------------------------------------------------
# GitHub push
# ---------------------------------------------------------------------------

def push_reviews_json(config: dict, json_content: str, last_updated: str):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("No GITHUB_TOKEN found — skipping GitHub push (local run).")
        return

    gh = Github(token)
    repo = gh.get_repo(f"{config['github']['repo_owner']}/{config['github']['repo_name']}")
    branch = config["github"]["branch"]
    file_path = "reviews_data.json"

    try:
        contents = repo.get_contents(file_path, ref=branch)
        repo.update_file(
            path=file_path,
            message=f"chore: update review text ({last_updated})",
            content=json_content,
            sha=contents.sha,
            branch=branch,
        )
    except Exception:
        # File doesn't exist yet — create it
        repo.create_file(
            path=file_path,
            message=f"chore: create review text data ({last_updated})",
            content=json_content,
            branch=branch,
        )

    print(f"Pushed reviews_data.json to GitHub ({repo.full_name} / {branch})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Review Text Fetcher ===\n")
    config = load_config()
    last_updated = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    print("Authenticating with Google Business Profile API...")
    creds = get_google_credentials()
    service = build_reviews_service(creds)

    output = {
        "last_updated": last_updated,
        "properties": [],
    }

    total_reviews = 0

    for prop in config["properties"]:
        name = prop["name"]
        region = prop["region"]
        location_id = prop.get("google_location_id", "")

        print(f"\nFetching reviews: {name}...", end=" ", flush=True)

        if not location_id or "YOUR_ACCOUNT_ID" in location_id:
            print("skipped (no location ID set)")
            output["properties"].append({
                "name": name,
                "region": region,
                "reviews": [],
            })
            continue

        # Reviews API uses the full accounts/.../locations/... path
        raw_reviews = fetch_all_reviews_for_location(service, location_id)
        parsed = [parse_review(r) for r in raw_reviews]
        total_reviews += len(parsed)
        print(f"{len(parsed)} reviews fetched")

        output["properties"].append({
            "name": name,
            "region": region,
            "reviews": parsed,
        })

        time.sleep(0.3)  # pacing between locations

    print(f"\nTotal reviews fetched: {total_reviews}")

    json_content = json.dumps(output, indent=2, ensure_ascii=False)
    REVIEWS_JSON_PATH.write_text(json_content, encoding="utf-8")
    print(f"Saved to {REVIEWS_JSON_PATH}")

    push_reviews_json(config, json_content, last_updated)
    print("\nDone!")


if __name__ == "__main__":
    main()
