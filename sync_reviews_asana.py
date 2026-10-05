"""
sync_reviews_asana.py

Syncs Google Business Profile reviews to Asana as tasks.
Each review creates one task in the configured project, assigned to the
regional manager for that property's region, with structured custom fields.

Custom fields created on first run:
  Property (text), Region (text), Reviewer (text),
  Star Rating (number), Date Posted (date), Owner Responded (enum Yes/No)

First run (no asana_synced.json): processes reviews from the past 3 months.
Subsequent runs: incremental — only unsynced reviews.

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

# Fields to create and attach to the project on first run.
# Cached GIDs are stored in asana_synced.json under "custom_fields".
FIELD_SPECS = {
    "property":    {"name": "Property",        "resource_subtype": "text"},
    "region":      {"name": "Region",          "resource_subtype": "text"},
    "reviewer":    {"name": "Reviewer",        "resource_subtype": "text"},
    "rating":      {"name": "Star Rating",     "resource_subtype": "number", "precision": 0},
    "date_posted": {"name": "Date Posted",     "resource_subtype": "date"},
    "responded":   {
        "name": "Owner Responded",
        "resource_subtype": "enum",
        "enum_options": [
            {"name": "Yes", "color": "green", "enabled": True},
            {"name": "No",  "color": "red",   "enabled": True},
        ],
    },
}

INITIAL_LOOKBACK_DAYS = 92  # ~3 months on first run


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def star_display(n: int) -> str:
    n = max(1, min(5, int(n)))
    return "★" * n + "☆" * (5 - n)


def review_hash(property_name: str, reviewer: str, raw_date: str) -> str:
    key = f"{property_name}|{reviewer}|{raw_date}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def asana_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def asana_get(token: str, path: str, params: dict = None) -> dict:
    resp = requests.get(f"{ASANA_BASE}{path}", headers=asana_headers(token),
                        params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def asana_post(token: str, path: str, body: dict) -> dict:
    resp = requests.post(f"{ASANA_BASE}{path}", headers=asana_headers(token),
                         json=body, timeout=15)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Asana POST {path} failed {resp.status_code}: {resp.text[:300]}")
    return resp.json()


# ─────────────────────────────────────────────
# Custom field setup
# ─────────────────────────────────────────────

def get_project_custom_fields(token: str) -> dict:
    """Return {field_name: field_gid} for fields already on the project."""
    data = asana_get(token, f"/projects/{PROJECT_GID}",
                     {"opt_fields": "custom_field_settings.custom_field.name,"
                                    "custom_field_settings.custom_field.gid"})
    result = {}
    for setting in data["data"].get("custom_field_settings", []):
        cf = setting["custom_field"]
        result[cf["name"]] = cf["gid"]
    return result


def create_custom_field(token: str, spec: dict) -> str:
    """Create a workspace-level custom field and return its GID."""
    body = {"data": {"workspace": WORKSPACE_GID, **spec}}
    result = asana_post(token, "/custom_fields", body)
    return result["data"]["gid"]


def attach_field_to_project(token: str, field_gid: str):
    """Attach a custom field to the project."""
    asana_post(token, f"/projects/{PROJECT_GID}/addCustomFieldSetting",
               {"data": {"custom_field": field_gid, "is_important": True}})


def get_enum_options(token: str, field_gid: str) -> dict:
    """Return {option_name: option_gid} for an enum field."""
    data = asana_get(token, f"/custom_fields/{field_gid}",
                     {"opt_fields": "enum_options.name,enum_options.gid"})
    return {opt["name"]: opt["gid"] for opt in data["data"].get("enum_options", [])}


def ensure_custom_fields(token: str, synced: dict) -> bool:
    """
    Creates any missing custom fields and caches their GIDs in synced.
    Returns True if synced was modified (needs saving).
    """
    if "custom_fields" in synced and synced["custom_fields"]:
        return False  # already set up

    print("Setting up custom fields for the first time...")
    existing = get_project_custom_fields(token)
    fields = {}

    for key, spec in FIELD_SPECS.items():
        field_name = spec["name"]
        if field_name in existing:
            print(f"  Found existing field: {field_name}")
            fields[key] = {"gid": existing[field_name]}
        else:
            print(f"  Creating field: {field_name}")
            gid = create_custom_field(token, spec)
            attach_field_to_project(token, gid)
            fields[key] = {"gid": gid}
            time.sleep(0.3)

        # Cache enum option GIDs
        if spec.get("resource_subtype") == "enum" or spec.get("enum_options"):
            opts = get_enum_options(token, fields[key]["gid"])
            fields[key]["options"] = opts

    synced["custom_fields"] = fields
    print("Custom fields ready.\n")
    return True


# ─────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────

def load_synced() -> dict:
    if SYNCED_JSON.exists():
        return json.loads(SYNCED_JSON.read_text(encoding="utf-8"))
    return {"last_sync": None, "synced_reviews": {}, "custom_fields": {}}


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


# ─────────────────────────────────────────────
# Task creation
# ─────────────────────────────────────────────

def parse_date(raw_date: str) -> str | None:
    """Return YYYY-MM-DD from an ISO timestamp, or None."""
    if not raw_date:
        return None
    try:
        return raw_date[:10]  # "2026-09-15T..." → "2026-09-15"
    except Exception:
        return None


def build_custom_fields(r: dict, fields: dict) -> dict:
    """Build the custom_fields dict for an Asana task."""
    cf = {}

    if "property" in fields:
        cf[fields["property"]["gid"]] = r["property"]

    if "region" in fields:
        cf[fields["region"]["gid"]] = r["region"]

    if "reviewer" in fields:
        cf[fields["reviewer"]["gid"]] = r["reviewer"]

    if "rating" in fields:
        cf[fields["rating"]["gid"]] = int(r["stars"])

    if "date_posted" in fields:
        date_str = parse_date(r.get("raw_date", ""))
        if date_str:
            cf[fields["date_posted"]["gid"]] = {"date": date_str}

    if "responded" in fields:
        opts = fields["responded"].get("options", {})
        answer = "Yes" if r.get("has_reply") else "No"
        if answer in opts:
            cf[fields["responded"]["gid"]] = opts[answer]

    return cf


def build_task(r: dict, fields: dict) -> dict:
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
            "name":          f"{stars} {r['property']} — {r['reviewer']}",
            "notes":         notes,
            "projects":      [PROJECT_GID],
            "workspace":     WORKSPACE_GID,
            "custom_fields": build_custom_fields(r, fields),
        }
    }

    manager = REGIONAL_MANAGERS.get(r["region"])
    if manager:
        body["data"]["assignee"] = manager["gid"]

    return body


def create_task(token: str, task_body: dict) -> str | None:
    try:
        result = asana_post(token, "/tasks", task_body)
        return result["data"]["gid"]
    except Exception as e:
        print(f"  Error: {e}")
        return None


# ─────────────────────────────────────────────
# GitHub push
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    print("=== Review → Asana Sync ===\n")

    asana_token = os.environ.get("ASANA_TOKEN")
    if not asana_token:
        raise SystemExit("ERROR: ASANA_TOKEN environment variable not set.")

    config  = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    synced  = load_synced()
    reviews = load_reviews()

    # Set up custom fields if this is the first run
    fields_changed = ensure_custom_fields(asana_token, synced)
    if fields_changed:
        save_synced(synced)

    fields = synced.get("custom_fields", {})

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

        task_body = build_task(r, fields)
        task_gid  = create_task(asana_token, task_body)

        if task_gid:
            synced["synced_reviews"][h] = task_gid
            manager = REGIONAL_MANAGERS.get(r["region"], {})
            print(f"  ✓ {star_display(r['stars'])} {r['property']} — {r['reviewer']} "
                  f"→ {manager.get('name', 'Unassigned')}")
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
