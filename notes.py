"""Chart notes: dated events marked on Houston's charts, the way Ahrefs marks Google updates on its history charts.

Table chart_notes, one row per note:
- day: 'YYYY-MM-DD'
- kind: Google update | Our change | Data | Business
- channel: '' for every page, or one channel (Website, BEBETEI, ...). A channel note shows on that channel's
  pages and on Total.
- note: plain text
- source: 'google' (Google's Search Status Dashboard), 'seed' (written into Houston on 23 Oct 2026) or 'manual'
- ref: Google's incident id, so a refresh never adds the same update twice

Houston's own pushes (change_log) are added to the charts automatically, one note per day.
"""
import datetime
import json
import urllib.request
from contextlib import closing

import pandas as pd

import core

KINDS = ["Google update", "Our change", "Data", "Business"]
ALL = "All pages"
GOOGLE_URL = "https://status.search.google.com/incidents.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS chart_notes (id INTEGER PRIMARY KEY, day TEXT, kind TEXT, channel TEXT, note TEXT,
    source TEXT, ref TEXT, added_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ix_chart_notes_ref ON chart_notes (ref) WHERE ref <> '';
"""

# Google ranking updates from the Search Status Dashboard (status.search.google.com, read 23 Oct 2026), and our own
# dated changes from the project's logs. update_google() adds later Google updates.
SEED = [
    ("2025-06-30", "Google update", "Website", "June 2025 core update (17 days)", "google-2025-06-core"),
    ("2025-08-26", "Google update", "Website", "August 2025 spam update (27 days)", "google-2025-08-spam"),
    ("2025-12-11", "Google update", "Website", "December 2025 core update (18 days)", "DsirqJ1gpPRgVQeccPRv"),
    ("2026-02-05", "Google update", "Website", "February 2026 Discover update (22 days)", "mYbNTqV1ytDc2fA8hUz4"),
    ("2026-03-24", "Google update", "Website", "March 2026 spam update (1 day)", "VbnSXAH4SmEcxPtx4YSD"),
    ("2026-03-27", "Google update", "Website", "March 2026 core update (12 days)", "7eTbAa2jWdToLkraZj5y"),
    ("2026-05-21", "Google update", "Website", "May 2026 core update (12 days)", "wdAXJk6LRRihEjpzEeWE"),
    ("2026-06-24", "Google update", "Website", "June 2026 spam update (2 days)", "YUX1peHev5a4fkxLDiUQ"),
    ("2026-08-18", "Google update", "Website", "August 2026 spam update (3 days)", "LEubPCm2octf2uMqCFKE"),
    ("2026-09-24", "Google update", "Website", "September 2026 spam update (14 days)", "XhUDXP7A67iHCD2kmbVu"),
    ("2026-01-24", "Data", "Website", "Bot visits in Google Analytics, 24-27 Jan (102,814 sessions on 26 Jan)", ""),
    ("2026-06-06", "Data", "Website", "Bot visits in Google Analytics, 6-24 Jun", ""),
    ("2026-08-27", "Data", "Website", "Bot visits in Google Analytics, 27-29 Aug", ""),
    ("2026-09-19", "Our change", "Website", "On-site search switched to Fulltext", ""),
    ("2026-09-21", "Our change", "Website",
     "Homepage title and description shortened; H1 on 10 CMS pages; 5 duplicate homepages switched off", ""),
    ("2026-09-25", "Our change", "Website",
     "Canonicals on product and blog pages; category renames; search-term rules; feed brand", ""),
    ("2026-09-27", "Our change", "Website", "33 plant drinks renamed, with new descriptions and FAQ", ""),
    ("2026-09-29", "Our change", "Website", "23 flakes and muesli renamed", ""),
    ("2026-09-30", "Our change", "Website", "57 pastas, gnocchi, vinegars and cooking creams renamed", ""),
    ("2026-10-13", "Data", "Website", "Bot filter switched on in Tag Manager", ""),
]


def connect():
    c = core.connect()
    if not core.READ_ONLY:
        c.executescript(SCHEMA)
        if not c.execute("SELECT 1 FROM chart_notes LIMIT 1").fetchone():
            with c:
                c.executemany("INSERT INTO chart_notes (day, kind, channel, note, source, ref, added_at) "
                              "VALUES (?, ?, ?, ?, 'seed', ?, ?)",
                              [(d, k, ch, n, ref, core.now()) for d, k, ch, n, ref in SEED])
    return c


def update_google():
    """Add Google ranking updates from the Search Status Dashboard that Houston doesn't have yet. Returns how many."""
    try:
        with urllib.request.urlopen(GOOGLE_URL, timeout=20) as r:
            incidents = json.loads(r.read().decode("utf-8"))
    except Exception as e:                                       # noqa: BLE001  (shown on the page)
        raise RuntimeError(f"Couldn't read Google's status page: {type(e).__name__}: {e}") from None
    new = 0
    with closing(connect()) as c, c:
        for i in incidents:
            if (i.get("service_name") or "") != "Ranking" or not i.get("id") or not i.get("begin"):
                continue
            begin = datetime.datetime.fromisoformat(i["begin"].replace("Z", "+00:00"))
            text = i.get("external_desc") or "Google ranking update"
            if i.get("end"):
                end = datetime.datetime.fromisoformat(i["end"].replace("Z", "+00:00"))
                days = max(1, round((end - begin).total_seconds() / 86400))
                text += f" ({days} day{'s' if days != 1 else ''})"
            cur = c.execute("INSERT OR IGNORE INTO chart_notes (day, kind, channel, note, source, ref, added_at) "
                            "VALUES (?, 'Google update', 'Website', ?, 'google', ?, ?)",
                            (begin.date().isoformat(), text, i["id"], core.now()))
            new += cur.rowcount
    return new


def _pushes(c, first, last):
    """Houston's own pushes to the store, one note per day."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'change_log'").fetchone():
        return []
    return [(day, "Our change", "Website", f"Houston push: {n} change{'s' if n != 1 else ''} to the store")
            for day, n in c.execute("SELECT substr(pushed_at, 1, 10), COUNT(*) FROM change_log "
                                    "WHERE substr(pushed_at, 1, 10) BETWEEN ? AND ? GROUP BY 1", (first, last))]


def for_chart(first, last, chans=None):
    """Notes dated first..last ('YYYY-MM-DD'): DataFrame day, kind, note. chans: the page's channels, None = all."""
    with closing(connect()) as c:
        rows = c.execute("SELECT day, kind, channel, note FROM chart_notes WHERE day BETWEEN ? AND ?",
                         (first, last)).fetchall() + _pushes(c, first, last)
    keep = [(d, k, n) for d, k, ch, n in rows if chans is None or not ch or ch in chans]
    df = pd.DataFrame(keep, columns=["day", "kind", "note"])
    return df.sort_values(["day", "kind"]).reset_index(drop=True)


def table():
    """Every stored note, for the editor."""
    with closing(connect()) as c:
        df = pd.read_sql_query("SELECT id, day, kind, channel, note, source, ref FROM chart_notes ORDER BY day", c)
    df["day"] = pd.to_datetime(df["day"], errors="coerce").dt.date
    df["channel"] = df["channel"].replace("", ALL)
    return df


def save(df):
    """Write the editor's rows back: changed rows are updated, new rows added, deleted rows deleted. A stored note
    whose date or text was cleared is kept unchanged rather than deleted. Returns (saved, kept unchanged)."""
    rows, blank = [], []
    for r in df.to_dict("records"):
        day, note = r.get("day"), str(r.get("note") or "").strip()
        if not day or not note or pd.isna(day):
            if not pd.isna(r.get("id")):
                blank.append(int(r["id"]))
            continue
        ch = r.get("channel") or ALL
        rows.append((None if pd.isna(r.get("id")) else int(r["id"]),
                     day.isoformat() if hasattr(day, "isoformat") else str(day)[:10],
                     r.get("kind") if r.get("kind") in KINDS else "Business",
                     "" if ch == ALL else ch, note,
                     r.get("source") if isinstance(r.get("source"), str) and r.get("source") else "manual",
                     r.get("ref") if isinstance(r.get("ref"), str) else ""))
    with closing(connect()) as c, c:
        keep = [r[0] for r in rows if r[0] is not None] + blank
        if keep:
            c.execute(f"DELETE FROM chart_notes WHERE id NOT IN ({','.join('?' * len(keep))})", keep)
        else:
            c.execute("DELETE FROM chart_notes")
        for rid, day, kind, ch, note, source, ref in rows:
            if rid is None:
                c.execute("INSERT INTO chart_notes (day, kind, channel, note, source, ref, added_at) "
                          "VALUES (?, ?, ?, ?, 'manual', '', ?)", (day, kind, ch, note, core.now()))
            else:
                c.execute("UPDATE chart_notes SET day = ?, kind = ?, channel = ?, note = ? WHERE id = ?",
                          (day, kind, ch, note, rid))
    return len(rows), len(blank)
