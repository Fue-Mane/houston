"""Google Search Console: sign-in, and pulls into data/houston.db.

Sign-in is shared with Google Analytics: see google_auth.py.

Three tables, all daily, all with the date each row was pulled:
- gsc_totals   whole site per day. Matches the Search Console graph.
- gsc_pages    per page and device. Includes anonymised queries.
- gsc_queries  per query, page and device. Google leaves out anonymised queries here,
               so these sums are lower than gsc_totals. That is expected.
"""
import datetime
import time
from contextlib import closing
from pathlib import Path

import core

import google_auth
from google_auth import GoogleError, sign_in, sign_out  # noqa: F401  (used by the Search Console page)

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
HISTORY_DAYS = 486          # Search Console keeps about 16 months
ROW_LIMIT = 25000           # the API's maximum per request

TABLES = {
    "gsc_totals": (["date"], ["date"]),
    "gsc_pages": (["date", "page", "device"], ["date", "page", "device"]),
    "gsc_queries": (["date", "query", "page", "device"], ["date", "query", "page", "device"]),
}


def status():
    st = google_auth.status(SCOPE)
    return "not_signed_in" if st == "needs_permission" else st


def service():
    from googleapiclient.discovery import build
    return build("searchconsole", "v1", credentials=google_auth.credentials(), cache_discovery=False)


def _execute(request, attempts=6):
    return google_auth.execute(request, "Search Console", attempts, sleep=lambda s: time.sleep(s))


def properties(svc=None):
    svc = svc or service()
    entries = _execute(svc.sites().list()).get("siteEntry", [])
    return sorted((e["siteUrl"], e.get("permissionLevel", "")) for e in entries)


# --- pulling -------------------------------------------------------------------
def latest_date(prop):
    with closing(core.connect()) as c:
        row = c.execute("SELECT MAX(date) FROM gsc_totals WHERE property = ?", (prop,)).fetchone()
    return row[0] if row and row[0] else None


def _months(start, end):
    out, cur = [], start
    while cur <= end:
        nxt = (cur.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        out.append((cur, min(end, nxt - datetime.timedelta(days=1))))
        cur = nxt
    return out


def _fetch(svc, prop, start, end, dimensions):
    rows, start_row = [], 0
    while True:
        body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": dimensions,
                "rowLimit": ROW_LIMIT, "startRow": start_row, "dataState": "final", "type": "web"}
        resp = _execute(svc.searchanalytics().query(siteUrl=prop, body=body))
        batch = resp.get("rows", [])
        rows += batch
        if len(batch) < ROW_LIMIT:
            return rows
        start_row += ROW_LIMIT


def pull(prop, start=None, end=None, progress=lambda f, t: None, svc=None):
    """Pull [start, end] into all three tables, replacing any rows already stored for those dates.
    Default: the full 16 months the first time; afterwards from 3 days before the latest stored date."""
    svc = svc or service()
    today = datetime.date.today()
    end = end or today - datetime.timedelta(days=1)
    if start is None:
        last = latest_date(prop)
        start = (datetime.date.fromisoformat(last) - datetime.timedelta(days=3)) if last \
            else today - datetime.timedelta(days=HISTORY_DAYS)
    months = _months(start, end)
    pulled_at, counts = core.now(), {t: 0 for t in TABLES}
    steps, step = len(months) * len(TABLES), 0
    for m_start, m_end in months:
        for table, (dims, cols) in TABLES.items():
            step += 1
            progress(step / max(steps, 1), f"{table}: {m_start:%b %Y}")
            rows = _fetch(svc, prop, m_start, m_end, dims)
            records = [(prop, *r["keys"], int(r.get("clicks", 0)), int(r.get("impressions", 0)),
                        float(r.get("ctr", 0)), float(r.get("position", 0)), pulled_at) for r in rows]
            marks = ", ".join("?" * (len(cols) + 6))
            with closing(core.connect()) as c, c:
                c.execute(f"DELETE FROM {table} WHERE property = ? AND date BETWEEN ? AND ?",
                          (prop, m_start.isoformat(), m_end.isoformat()))
                c.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({marks})", records)
                c.execute("INSERT INTO gsc_pulls VALUES (?, ?, ?, ?, ?, ?)",
                          (pulled_at, prop, table, m_start.isoformat(), m_end.isoformat(), len(records)))
            counts[table] += len(records)
    return {"start": start.isoformat(), "end": end.isoformat(), "rows": counts}


def missing_ranges(prop):
    """Runs of dates with no stored rows, between the first and last stored day."""
    with closing(core.connect()) as c:
        days = [datetime.date.fromisoformat(r[0]) for r in
                c.execute("SELECT date FROM gsc_totals WHERE property = ? ORDER BY date", (prop,))]
    gaps = []
    for a, b in zip(days, days[1:]):
        if (b - a).days > 1:
            gaps.append((a + datetime.timedelta(days=1), b - datetime.timedelta(days=1), (b - a).days - 1))
    return gaps


def totals(prop, start, end):
    """Sums for a date range, from gsc_totals, to compare with the Search Console graph."""
    with closing(core.connect()) as c:
        clicks, impr, pos_x_impr, days = c.execute(
            "SELECT SUM(clicks), SUM(impressions), SUM(position * impressions), COUNT(*) FROM gsc_totals "
            "WHERE property = ? AND date BETWEEN ? AND ?", (prop, start, end)).fetchone()
    if not days:
        return None
    return {"days": days, "clicks": clicks or 0, "impressions": impr or 0,
            "ctr": (clicks or 0) / impr if impr else 0.0, "position": pos_x_impr / impr if impr else 0.0}
