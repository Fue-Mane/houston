# Houston

Start it from this folder in PowerShell:

    pip install -r requirements.txt
    streamlit run app.py

It opens in your browser. Enter the API key at the bottom of the left sidebar; the badge turns green when the store answers.

- Houston starts in dark mode. To switch to white/light mode, open the top-right menu (⋮) → **Settings** → **Theme**. The choice is stored by Streamlit for this browser, and can be changed back to dark mode there.
- Nothing is written to the store until you press Push on "Review and push".
- Every push saves the old values in `data/backups`, reads each change back, and logs it in the change log.
- The API key is never saved. Closing the browser tab disconnects.
- Everything Houston keeps lives in one database file, `data/houston.db`: the catalogue copy, the review queue and the change log. Don't delete the `data` folder.
- The first start after updating moves the old `catalogue.json`, `pending.json` and `changelog.jsonl` into the database and renames them `*.old`.

## Use with Claude Desktop

Houston has a read-only MCP server (`mcp_server.py`) that lets Claude Desktop answer questions from `data/houston.db` (e.g. missing meta descriptions, finding products by SKU, or summarizing recent pushes). 

**The MCP server does not connect to the live store and cannot push changes.** It opens the database read-only, so it can run while Houston is open. Besides the named tools, `describe_database` and `run_sql` let Claude query any table.

To connect Claude Desktop, add this to its config file (`%APPDATA%\Claude\claude_desktop_config.json`), then fully quit and restart Claude Desktop:

```json
{
  "mcpServers": {
    "houston": {
      "command": "D:\\vscode\\projects\\houston\\.venv\\Scripts\\python.exe",
      "args": ["D:\\vscode\\projects\\houston\\mcp_server.py"]
    }
  }
}
```

## Google sign-in

One sign-in, as nicoleta@biomania.ro, covers Search Console and Google Analytics (both read-only). If Houston asks you to sign in again after an update, it needs a permission that the old sign-in didn't have.

## Google Search Console

1. Save the OAuth file from Google Cloud (project `houston`, Desktop app) as `secrets/client_secret.json`.
2. Open Houston → Google → Google Search Console → **Sign in with Google**, as nicoleta@biomania.ro.
3. **Pull 16 months** the first time, then **Update from Google**. Each update re-pulls the last 3 days.

- Data lands in `gsc_totals`, `gsc_pages` and `gsc_queries`, and every pull is recorded in `gsc_pulls`.
- `secrets/` holds the Google login. Don't share it. The MCP server never reads it.

## Google Analytics

Houston → Google → Google Analytics → choose the GA4 property → **Pull 16 months**, then **Update from Google**. Data lands in `ga4_totals`, `ga4_landing`, `ga4_items` and `ga4_site_search`; every pull is recorded in `ga4_pulls`.


## Storage (since 17 October 2026)

Everything lives in one file, `data/houston.db` (SQLite). The three big Google tables are stored compactly:
every page address, search, device, channel and property is stored once in `dict`, and the data rows
(`gsc_pages_c`, `gsc_queries_c`, `ga4_landing_c`) point to it by number. Views with the old names
(`gsc_pages`, `gsc_queries`, `ga4_landing`) give the familiar columns back, so pages and SQL work as before.
`ctr` is calculated (clicks / impressions); the per-row pull time is gone (see `gsc_pulls`, `ga4_pulls`).

The first start after this update reorganises an older database once, with a progress bar. Every table's
totals are checked before its old version is removed, and a full copy is kept first as
`data/houston-before-compaction.db`. Delete that copy after a few days if everything looks right.

## Backup

Sidebar → **Backup** → choose a folder inside Google Drive (e.g. `G:\My Drive\Houston backups`) → **Back up now**.
Each backup is one zip: a clean snapshot of the database, `data/backups/`, `data/uploads/` and `notes/worklog.md`.
`secrets/` is never included. Houston keeps the newest 8 zips in that folder. Don't put the live `data/`
folder itself in Google Drive: Drive could copy the database halfway through a write.

**Restore:** close Houston and the Claude app. Unzip the backup. Replace `data/houston.db` with the one from
the zip and delete `data/houston.db-wal` and `data/houston.db-shm` if they exist. Copy back `data/backups/`,
`data/uploads/` and `notes/worklog.md` if needed. Start Houston.
