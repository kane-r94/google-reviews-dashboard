"""
fetch_reviews.py

Fetches Google Business Profile ratings, Trustpilot ratings, and AllAgents ratings
for each property, then injects the data into google_reviews_dashboard.html and
pushes the updated file to GitHub so GitHub Pages publishes it automatically.

Run locally:   python fetch_reviews.py
Run in CI:     the GitHub Actions workflow calls this automatically every Monday.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from github import Github
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.errors import HttpError

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
CREDENTIALS_DIR = ROOT / "credentials"
OAUTH_CLIENT_FILE = CREDENTIALS_DIR / "oauth_client.json"
TOKEN_FILE = CREDENTIALS_DIR / "token.json"
HTML_PATH = ROOT / "google_reviews_dashboard.html"

SCOPES = ["https://www.googleapis.com/auth/business.manage"]

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------

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
                    "Please download it from Google Cloud Console and place it there.\n"
                    "See README.md for step-by-step instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(OAUTH_CLIENT_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.parent.mkdir(exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        print(f"Token saved to {TOKEN_FILE}")

    return creds


# ---------------------------------------------------------------------------
# Google Business Profile
# ---------------------------------------------------------------------------

def build_google_service(creds: Credentials):
    # Returns an authorised session for direct HTTP calls.
    from google.auth.transport.requests import AuthorizedSession
    return AuthorizedSession(creds)


def fetch_google_rating(session, location_id: str) -> dict:
    """
    Fetches rating and review count via the v4 reviews endpoint.
    The reviews list response includes averageRating and totalReviewCount
    at the top level, so we only need to fetch one page (pageSize=1).
    """
    if not location_id:
        return {"rating": None, "review_count": None}
    try:
        url = f"https://mybusiness.googleapis.com/v4/{location_id}/reviews"
        resp = session.get(url, params={"pageSize": 1}, timeout=15)

        if resp.status_code != 200:
            print(f"  API error {resp.status_code}: {resp.text[:300]}")
            return {"rating": None, "review_count": None}

        data = resp.json()
        avg_rating = data.get("averageRating")
        total_reviews = data.get("totalReviewCount")

        if avg_rating is None:
            return {"rating": None, "review_count": None}

        return {
            "rating": round(float(avg_rating), 1),
            "review_count": int(total_reviews) if total_reviews else 0,
        }

    except Exception as e:
        print(f"  Unexpected error for {location_id}: {e}")
        return {"rating": None, "review_count": None}


# ---------------------------------------------------------------------------
# Trustpilot
# ---------------------------------------------------------------------------

def fetch_trustpilot_rating(url: str) -> dict:
    if not url:
        return {"rating": None, "review_count": None}
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    agg = item.get("aggregateRating", {})
                    rating = agg.get("ratingValue")
                    count = agg.get("reviewCount")
                    if rating is not None:
                        return {
                            "rating": round(float(rating), 1),
                            "review_count": int(count) if count else 0,
                        }
            except (json.JSONDecodeError, AttributeError):
                continue

        return {"rating": None, "review_count": None}

    except Exception as e:
        print(f"  Trustpilot error for {url}: {e}")
        return {"rating": None, "review_count": None}


# ---------------------------------------------------------------------------
# AllAgents
# ---------------------------------------------------------------------------

def fetch_all_agents_rating(url: str) -> dict:
    if not url:
        return {"rating": None, "review_count": None}
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    agg = item.get("aggregateRating", {})
                    rating = agg.get("ratingValue")
                    count = agg.get("reviewCount")
                    if rating is not None:
                        return {
                            "rating": round(float(rating), 1),
                            "review_count": int(count) if count else 0,
                        }
            except (json.JSONDecodeError, AttributeError):
                continue

        return {"rating": None, "review_count": None}

    except Exception as e:
        print(f"  AllAgents error for {url}: {e}")
        return {"rating": None, "review_count": None}


# ---------------------------------------------------------------------------
# HTML injection
# ---------------------------------------------------------------------------

def extract_previous_scores(html_content: str) -> dict:
    """
    Read the current 'score' values from the HTML before overwriting them,
    so they become the 'last' (previous week) values in the next update.
    Returns a dict of {property_name: score_float_or_None}.
    """
    previous = {}
    prop_block = re.search(r"const properties\s*=\s*\[([\s\S]*?)\];", html_content)
    if not prop_block:
        return previous
    for m in re.finditer(r'name:\s*"([^"]+)"[\s\S]*?score:\s*([0-9.]+|null)', prop_block.group(1)):
        name = m.group(1)
        score_str = m.group(2)
        previous[name] = float(score_str) if score_str != "null" else None
    return previous


def extract_previous_platform_scores(html_content: str) -> dict:
    """
    Read current otherPlatforms scores to use as 'last' values next week.
    Returns a dict of {platform_name: score_float_or_None}.
    """
    previous = {}
    plat_block = re.search(r"const otherPlatforms\s*=\s*\[([\s\S]*?)\];", html_content)
    if not plat_block:
        return previous
    for m in re.finditer(r'name:\s*"([^"]+)"[\s\S]*?score:\s*([0-9.]+|null)', plat_block.group(1)):
        name = m.group(1)
        score_str = m.group(2)
        previous[name] = float(score_str) if score_str != "null" else None
    return previous


def build_js_properties_array(properties_data: list, previous_scores: dict) -> str:
    """
    Build the `const properties = [...]` block.
    Uses score/last/reviews format to match the dashboard's existing JS.
    The previous week's score becomes 'last' automatically.
    """
    lines = ["const properties = ["]
    for p in properties_data:
        name = p["name"]
        new_score = p["google"]["rating"]
        last_score = previous_scores.get(name, new_score)  # fall back to current if no history
        reviews = p["google"]["review_count"]

        def fmt(v):
            return "null" if v is None else (f'"{v}"' if isinstance(v, str) else str(v))

        lines.append("  {")
        lines.append(f'    name: {fmt(name)},')
        lines.append(f'    region: {fmt(p["region"])},')
        lines.append(f'    score: {fmt(new_score)},')
        lines.append(f'    last: {fmt(last_score)},')
        lines.append(f'    reviews: {fmt(reviews)},')
        lines.append("  },")
    lines.append("];")
    return "\n".join(lines)


def build_js_platforms_array(tp_data: dict, previous_platform_scores: dict) -> str:
    """
    Build the `const otherPlatforms = [...]` block using company-level platform scores.
    """
    platforms = [
        {"name": "Trustpilot", "score": tp_data["rating"], "reviews": tp_data["review_count"]},
    ]

    lines = ["const otherPlatforms = ["]
    for p in platforms:
        last = previous_platform_scores.get(p["name"], p["score"])
        def fmt(v):
            return "null" if v is None else (f'"{v}"' if isinstance(v, str) else str(v))
        lines.append("  {")
        lines.append(f'    name: {fmt(p["name"])},')
        lines.append(f'    score: {fmt(p["score"])},')
        lines.append(f'    last: {fmt(last)},')
        lines.append(f'    reviews: {fmt(p["reviews"])},')
        lines.append("  },")
    lines.append("];")
    return "\n".join(lines)


def inject_into_html(html_content: str, js_properties: str, js_platforms: str, last_updated: str) -> str:
    # Replace properties array
    pattern_props = r"const properties\s*=\s*\[[\s\S]*?\];"
    if not re.search(pattern_props, html_content):
        sys.exit(
            "\nERROR: Could not find 'const properties = [...]' in the HTML file.\n"
            "Make sure the dashboard HTML contains that variable name exactly."
        )
    updated = re.sub(pattern_props, js_properties, html_content, count=1)

    # Replace otherPlatforms array
    pattern_plat = r"const otherPlatforms\s*=\s*\[[\s\S]*?\];"
    if re.search(pattern_plat, updated):
        updated = re.sub(pattern_plat, js_platforms, updated, count=1)

    # Update last-updated timestamp in the header
    updated = re.sub(
        r'(<span[^>]*class="last-updated"[^>]*>)[^<]*(</span>)',
        rf'\g<1>{last_updated}\g<2>',
        updated,
    )

    return updated


# ---------------------------------------------------------------------------
# GitHub push
# ---------------------------------------------------------------------------

def push_to_github(config: dict, html_content: str, last_updated: str):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("No GITHUB_TOKEN found — skipping GitHub push (local run).")
        return

    gh = Github(token)
    repo = gh.get_repo(f"{config['github']['repo_owner']}/{config['github']['repo_name']}")
    branch = config["github"]["branch"]
    file_path = config["github"]["html_file_path"]

    contents = repo.get_contents(file_path, ref=branch)
    repo.update_file(
        path=file_path,
        message=f"chore: update review data ({last_updated})",
        content=html_content,
        sha=contents.sha,
        branch=branch,
    )
    print(f"Pushed updated HTML to GitHub ({repo.full_name} / {branch})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Google Reviews Dashboard Updater ===\n")
    config = load_config()
    last_updated = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    print("Authenticating with Google Business Profile API...")
    creds = get_google_credentials()
    service = build_google_service(creds)

    properties_data = []
    for prop in config["properties"]:
        name = prop["name"]
        print(f"\nFetching: {name}")

        print("  → Google Business Profile...", end=" ", flush=True)
        google_data = fetch_google_rating(service, prop.get("google_location_id", ""))
        print(f"Rating: {google_data['rating'] or 'N/A'}")

        properties_data.append({
            "name": name,
            "region": prop["region"],
            "google": google_data,
        })

    # Fetch company-level platform scores
    platforms_config = config.get("platforms", {})

    tp_url = platforms_config.get("trustpilot_url", "")
    if tp_url:
        print(f"\nFetching: Trustpilot (company)...", end=" ", flush=True)
        tp_data = fetch_trustpilot_rating(tp_url)
        print(f"Rating: {tp_data['rating'] or 'N/A'}")
        time.sleep(1)
    else:
        tp_data = {"rating": None, "review_count": None}

    print("\nUpdating HTML file...")
    if not HTML_PATH.exists():
        sys.exit(
            f"\nERROR: {HTML_PATH} not found.\n"
            "Place your google_reviews_dashboard.html file in the project root."
        )

    html_content = HTML_PATH.read_text(encoding="utf-8")

    # Extract previous scores BEFORE overwriting — these become "last week"
    previous_scores = extract_previous_scores(html_content)
    previous_platform_scores = extract_previous_platform_scores(html_content)

    js_properties = build_js_properties_array(properties_data, previous_scores)
    js_platforms  = build_js_platforms_array(tp_data, previous_platform_scores)
    updated_html  = inject_into_html(html_content, js_properties, js_platforms, last_updated)
    HTML_PATH.write_text(updated_html, encoding="utf-8")
    print(f"HTML updated. Last updated: {last_updated}")

    push_to_github(config, updated_html, last_updated)
    print("\nDone!")


if __name__ == "__main__":
    main()
