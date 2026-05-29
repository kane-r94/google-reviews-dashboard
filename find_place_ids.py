"""
find_place_ids.py

Search for the Google Place ID for each property in config.json.
Run this once to populate the place_id fields, then delete or ignore it.

Usage:  python find_place_ids.py
"""

import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"

FIND_PLACE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"


def get_api_key() -> str:
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        key_file = ROOT / "credentials" / "places_api_key.txt"
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8").strip()
    if not key:
        sys.exit(
            "\nERROR: API key not found.\n"
            "Paste your Places API key into credentials/places_api_key.txt first."
        )
    return key


def find_place_id(name: str, api_key: str) -> list:
    """Return a list of candidate matches for a property name."""
    try:
        resp = requests.get(
            FIND_PLACE_URL,
            params={
                "input": name,
                "inputtype": "textquery",
                "fields": "place_id,name,formatted_address,rating",
                "key": api_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            print(f"  API error: {data.get('status')} — {data.get('error_message', '')}")
            return []

        return data.get("candidates", [])

    except Exception as e:
        print(f"  Request error: {e}")
        return []


def main():
    print("=== Place ID Finder ===\n")
    print("Paste your Places API key into credentials/places_api_key.txt before running.\n")

    api_key = get_api_key()

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    print(f"Searching for {len(config['properties'])} properties...\n")
    print("-" * 60)

    for prop in config["properties"]:
        name = prop["name"]
        existing_id = prop.get("place_id", "")

        if existing_id:
            print(f"SKIP (already set): {name}")
            print(f"  place_id: {existing_id}\n")
            continue

        print(f"Searching: {name}")
        candidates = find_place_id(name, api_key)

        if not candidates:
            print("  No results found. Try a more specific search term.\n")
            continue

        for i, c in enumerate(candidates[:3]):
            print(f"  [{i+1}] {c.get('name')} — {c.get('formatted_address')}")
            print(f"       Rating: {c.get('rating', 'N/A')}  |  place_id: {c.get('place_id')}")

        print()

    print("-" * 60)
    print("\nCopy the correct place_id for each property into config.json.")
    print("If a property has no results, try searching with its full address.")


if __name__ == "__main__":
    main()
