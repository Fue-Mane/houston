"""Houston: one-off check that the Instagram token works. Read-only.

Reads secrets/instagram_token.txt and asks Instagram for the account's
username, type, followers and number of posts. Never prints the token.

Run from the Houston folder in PowerShell:
    .venv\\Scripts\\python.exe instagram_check.py
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SECRETS = Path(__file__).parent / "secrets"
TOKEN_FILE = SECRETS / "instagram_token.txt"

if not TOKEN_FILE.exists():
    if (SECRETS / "instagram_token.txt.txt").exists():
        raise SystemExit("Windows saved it as instagram_token.txt.txt. Rename it to instagram_token.txt.")
    raise SystemExit("Not found: secrets/instagram_token.txt")

token = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
params = urllib.parse.urlencode({
    "fields": "username,account_type,followers_count,media_count",
    "access_token": token,
})
url = "https://graph.instagram.com/v25.0/me?" + params

try:
    with urllib.request.urlopen(url, timeout=30) as response:
        data = json.load(response)
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", "replace").replace(token, "<token>")
    raise SystemExit(f"Instagram refused the request ({e.code}):\n{body}")
except urllib.error.URLError as e:
    raise SystemExit(f"Could not reach Instagram: {e.reason}")

print("Token works.")
print("Account:   ", data.get("username"))
print("Type:      ", data.get("account_type"))
print("Followers: ", data.get("followers_count"))
print("Posts:     ", data.get("media_count"))
