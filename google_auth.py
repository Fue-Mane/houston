"""One Google sign-in for Houston (Internal desktop app in Google Cloud project "houston").

- secrets/client_secret.json  downloaded from Google Cloud
- secrets/google_token.json   written at sign-in; lets Houston refresh access by itself
Read-only permissions: Search Console, Google Analytics and Cloud Storage (the WinMENTOR bucket).
The MCP server never reads these files.
"""
import json
from pathlib import Path

SECRETS = Path(__file__).parent / "secrets"
CLIENT_FILE = SECRETS / "client_secret.json"
TOKEN_FILE = SECRETS / "google_token.json"
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly",
          "https://www.googleapis.com/auth/analytics.readonly",
          "https://www.googleapis.com/auth/devstorage.read_only"]


class GoogleError(Exception):
    pass


def granted_scopes():
    try:
        return set(json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("scopes") or [])
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def status(scope=None):
    """no_client / not_signed_in / needs_permission / signed_in (for the given scope)."""
    if not CLIENT_FILE.exists():
        return "no_client"
    if not TOKEN_FILE.exists():
        return "not_signed_in"
    if scope and scope not in granted_scopes():
        return "needs_permission"
    return "signed_in"


def sign_in():
    """Opens the browser for the Google sign-in (all Houston permissions), then saves the token."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    SECRETS.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def sign_out():
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()


def credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not TOKEN_FILE.exists():
        raise GoogleError("Not signed in to Google yet.")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))  # keeps the permissions actually granted
    if not creds.valid:
        if not creds.refresh_token:
            raise GoogleError("Google sign-in has no refresh token. Sign in again.")
        try:
            creds.refresh(Request())
        except Exception as e:
            raise GoogleError(f"Google sign-in expired or was revoked ({e}). Sign in again.") from None
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def execute(request, what, attempts=6, sleep=None, give_up=lambda e: False):
    """Run one API request, waiting and retrying when Google asks to slow down.
    give_up(e) True means don't retry (e.g. an hourly allowance that waiting a minute won't fix)."""
    import time
    sleep = sleep or time.sleep
    for attempt in range(attempts):
        try:
            return request.execute()
        except Exception as e:
            code = getattr(getattr(e, "resp", None), "status", None)
            if code in (429, 500, 503) and attempt < attempts - 1 and not give_up(e):
                sleep(min(60, 5 * 2 ** attempt))
                continue
            raise GoogleError(f"{what} API error{f' {code}' if code else ''}: {e}") from None
