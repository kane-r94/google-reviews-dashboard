"""
list_locations.py

Run this once after setting up your OAuth credentials to discover all your
Google Business Profile account IDs and location IDs.

Usage:  python list_locations.py

Copy the location 'name' values (e.g. accounts/123/locations/456) into config.json.
"""

import json
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

ROOT = Path(__file__).parent
CREDENTIALS_DIR = ROOT / "credentials"
OAUTH_CLIENT_FILE = CREDENTIALS_DIR / "oauth_client.json"
TOKEN_FILE = CREDENTIALS_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/business.manage"]


def get_credentials():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not OAUTH_CLIENT_FILE.exists():
                sys.exit(
                    f"\nERROR: {OAUTH_CLIENT_FILE} not found.\n"
                    "Download oauth_client.json from Google Cloud Console first.\n"
                    "See README.md Step 3 for instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(OAUTH_CLIENT_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def main():
    print("Authenticating...\n")
    creds = get_credentials()

    # List accounts
    account_service = build(
        "mybusinessaccountmanagement",
        "v1",
        credentials=creds,
        discoveryServiceUrl=(
            "https://mybusinessaccountmanagement.googleapis.com/$discovery/rest?version=v1"
        ),
    )

    accounts_response = account_service.accounts().list().execute()
    accounts = accounts_response.get("accounts", [])

    if not accounts:
        print("No Google Business Profile accounts found for this Google account.")
        return

    print(f"Found {len(accounts)} account(s):\n")

    location_service = build(
        "mybusinessbusinessinformation",
        "v1",
        credentials=creds,
        discoveryServiceUrl=(
            "https://mybusinessbusinessinformation.googleapis.com/$discovery/rest?version=v1"
        ),
    )

    for account in accounts:
        account_name = account.get("name")
        account_display = account.get("accountName", account_name)
        print(f"Account: {account_display}")
        print(f"  Account ID: {account_name}\n")

        try:
            locations_response = location_service.accounts().locations().list(
                parent=account_name,
                readMask="name,title",
                pageSize=100,
            ).execute()

            locations = locations_response.get("locations", [])
            if not locations:
                print("  No locations found for this account.\n")
                continue

            print(f"  {len(locations)} location(s) found:\n")
            for loc in locations:
                title = loc.get("title", "(no title)")
                loc_name = loc.get("name", "")
                print(f"  Property : {title}")
                print(f"  Location ID: {loc_name}")
                print()

        except HttpError as e:
            print(f"  Error listing locations: {e}\n")

    print("Copy the 'Location ID' values above into config.json.")


if __name__ == "__main__":
    main()
