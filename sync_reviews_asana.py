"""
sync_reviews_asana.py

Syncs Google Business Profile reviews to Asana as tasks.
Each review creates one task in the configured project, assigned to the
regional manager for that property's region.

First run (no asana_synced.json): processes all reviews from the past 3 months.
Subsequent runs: processes only reviews not yet synced.

Run locally:   python sync_reviews_asana.py
Run in CI:     GitHub Actions — daily at 07:00 UTC, after review text fetch.
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from github import Github

ROOT         = Path(__file__).parent
REVIEWS_JSON = ROOT / "reviews_data.json"
SYNCED_JSON  = ROOT / "asana_synced.json"
CONFIG_PATH  = ROOT / "config.json"

ASANA_BASE    = "https://app.asana.com/api/1.0"
WORKSPACE_GID = "11321289793968"
PROJECT_GID   = "1219151532451213"

REGIONAL_MANAGERS = {
    "Region 1": {"name": "Joshua Hodson",   "gid": "1204727493984535"},
    "Region 2": {"name": "Thomas Lomax",    "gid": "1203046251507796"},
    "Region 3": {"name": "Krizia Teixeira", "gid": "1211077766072351"},
    "Region 4": {"name": "Josh Lanyon",     "gid": "1204908264262839"},
}

INITIAL_LOOKBACK_DAYS = 92  # ~3 months on first run


def star_display(n: int) -> str:
    n = max(1, min(5, int(n)))
    return "★" * n + "☆" * (5 - n)


def review_hash(property_name: str, reviewer: str, raw_date: str) -> str:
    key = f"{property_name}|{reviewer}|{raw_date}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def load_synced() -> dict:
    if SYNCED_JSON.exists():
        return json.loads(SYNCED_JSON.read_text(encoding="utf-8"))
    return {"last_sync": None, "synced_reviews": {}}


def save_synced(synced: dict):
    SYNCED_JSON.write_text(json.dumps(synced, indent=2, ensure_ascii=False), encoding="utf-8")


def load_reviews() -> list:
    if not REVIEWS_JSON.exists():
        raise SystemExit(f"ERROR: {REVIEWS_JSON} not found. Run fetch_review_text.py first.")
    data = json.loads(REVIEWS_JSON.read_text(encoding="utf-8"))
    reviews = []
    for prop in data.get("properties", []):
        for r in prop.get("reviews", []):
            reviews.append({**r, "property": prop["name"], "region": prop["region"]})
    return reviews


def build_task(r: dict) -> dict:
    stars   = star_display(r["stars"])
    replied = "Yes" if r.get("has_reply") else "No"
    text    = (r.get("text") or "").strip() or "(No written review)"

    notes = (
        f"Property: {r['property']}\n"
        f"Region: {r['region']}\n"
        f"Reviewer: {r['reviewer']}\n"
        f"Rating: {stars} ({r['stars']}/5)\n"
        f"Date posted: {r['date']}\n"
        f"Owner responded: {replied}\n"
        f"\nReview:\n{text}\n"
        f"\n—\nSource: Google Business Profile"
    )

    body = {
        "data": {
            "name":      f"{stars} {r['property']} — {r['reviewer']}",
            "notes":     notes,
            "projects":  [PROJECT_GID],
            "workspace": WORKSPACE_GID,
        }
    }

    manager = REGIONAL_MANAGERS.get(r["region"])
    if manager:
        body["data"]["assignee"] = manager["gid"]

    return body


def create_task(token: str, task_body: dict) -> str | None:
    resp = requests.post(
        f"{ASANA_BASE}/tasks",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=task_body,
        timeout=15,
    )
    if resp.status_code in (200, 201):
        return resp.json()["data"]["gid"]
    print(f"  Asana error {resp.status_code}: {resp.text[:200]}")
    return None


def push_synced_json(config: dict, synced: dict, timestamp: str):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("No GITHUB_TOKEN — skipping push (local run).")
        return

    gh        = Github(token)
    repo      = gh.get_repo(f"{config['github']['repo_owner']}/{config['github']['repo_name']}")
    branch    = config["github"]["branch"]
    file_path = "asana_synced.json"
    content   = json.dumps(synced, indent=2, ensure_ascii=False)

    for attempt in range(3):
        try:
            existing = repo.get_contents(file_path, ref=branch)
            repo.update_file(
                path=file_path,
                message=f"chore: update asana sync log ({timestamp})",
                content=content,
                sha=existing.sha,
                branch=branch,
            )
            break
        except Exception as e:
            if "404" in str(e):
                repo.create_file(
                    path=file_path,
                    message=f"chore: create asana sync log ({timestamp})",
                    content=content,
                    branch=branch,
                )
                break
            if attempt == 2:
                raise

    print("Pushed asana_synced.json to GitHub")


def main():
    print("=== Review → Asana Sync ===\n")

    asana_token = os.environ.get("ASANA_TOKEN")
    if not asana_token:
        raise SystemExit("ERROR: ASANA_TOKEN environment variable not set.")

    config  = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    synced  = load_synced()
    reviews = load_reviews()

    is_first_run = not synced["synced_reviews"]
    cutoff = None
    if is_first_run:
        cutoff = datetime.now(timezone.utc) - timedelta(days=INITIAL_LOOKBACK_DAYS)
        print(f"First run — syncing reviews since {cutoff.strftime('%d %b %Y')}\n")
    else:
        print(f"Incremental run — last sync: {synced['last_sync']}\n")

    created = 0
    skipped = 0
    failed  = 0

    for r in reviews:
        if r["region"] == "HQ":
            skipped += 1
            continue

        h = review_hash(r["property"], r["reviewer"], r.get("raw_date", ""))

        if h in synced["synced_reviews"]:
            skipped += 1
            continue

        if cutoff and r.get("raw_date"):
            try:
                review_dt = datetime.fromisoformat(r["raw_date"].replace("Z", "+00:00"))
                if review_dt < cutoff:
                    skipped += 1
                    continue
            except ValueError:
                pass

        task_body = build_task(r)
        task_gid  = create_task(asana_token, task_body)

        if task_gid:
            synced["synced_reviews"][h] = task_gid
            manager = REGIONAL_MANAGERS.get(r["region"], {})
            print(f"  ✓ {star_display(r['stars'])} {r['property']} — {r['reviewer']} → {manager.get('name', 'Unassigned')}")
            created += 1
            time.sleep(0.2)
        else:
            print(f"  ✗ Failed: {r['property']} — {r['reviewer']}")
            failed += 1

    synced["last_sync"] = datetime.now(timezone.utc).isoformat()
    save_synced(synced)

    print(f"\nDone — created: {created}, skipped: {skipped}, failed: {failed}")

    timestamp = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    push_synced_json(config, synced, timestamp)
    print("\nFinished.")


if __name__ == "__main__":
    main()
