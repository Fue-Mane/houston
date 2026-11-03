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


def _table_resume(prop, table, today):
    """3 days before the latest day stored in this table (Google can finalise late days later), or 16 months back.
    Each table resumes from its own data, so a pull that stopped after the totals still fetches the detail."""
    with closing(core.connect()) as c:
        if table == "gsc_totals":
            row = c.execute("SELECT MAX(date) FROM gsc_totals WHERE property = ?", (prop,)).fetchone()
        else:
            pid = c.execute("SELECT id FROM dict WHERE kind = 'property' AND value = ?", (prop,)).fetchone()
            row = c.execute(f"SELECT MAX(date) FROM {table}_c WHERE prop = ?", pid).fetchone() if pid else None
    if row and row[0]:
        return datetime.date.fromisoformat(row[0]) - datetime.timedelta(days=3)
    return today - datetime.timedelta(days=HISTORY_DAYS)


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
    Default: the full 16 months the first time; afterwards each table from 3 days before its own latest stored date."""
    svc = svc or service()
    today = datetime.date.today()
    end = end or today - datetime.timedelta(days=1)
    starts = {t: start or _table_resume(prop, t, today) for t in TABLES}
    order = list(TABLES)
    jobs = sorted((m_start, order.index(t), m_end, t) for t in TABLES
                  for m_start, m_end in google_auth.month_chunks(starts[t], end))
    pulled_at, counts = core.now(), {t: 0 for t in TABLES}
    for step, (m_start, _, m_end, table) in enumerate(jobs, 1):
        dims, cols = TABLES[table]
        progress(step / max(len(jobs), 1), f"{table}: {m_start:%b %Y}")
        rows = _fetch(svc, prop, m_start, m_end, dims)
        with closing(core.connect()) as c, c:
            if table == "gsc_totals":
                records = [(prop, *r["keys"], int(r.get("clicks", 0)), int(r.get("impressions", 0)),
                            float(r.get("ctr", 0)), float(r.get("position", 0)), pulled_at) for r in rows]
                c.execute("DELETE FROM gsc_totals WHERE property = ? AND date BETWEEN ? AND ?",
                          (prop, m_start.isoformat(), m_end.isoformat()))
                c.executemany(f"INSERT OR REPLACE INTO gsc_totals VALUES ({', '.join('?' * 7)})", records)
            else:  # compact storage: see core.write_compact
                records = [dict(zip(dims, r["keys"]), clicks=int(r.get("clicks", 0)),
                                impressions=int(r.get("impressions", 0)), position=float(r.get("position", 0)))
                           for r in rows]
                core.write_compact(c, table, prop, m_start.isoformat(), m_end.isoformat(), records)
            c.execute("INSERT INTO gsc_pulls VALUES (?, ?, ?, ?, ?, ?)",
                      (pulled_at, prop, table, m_start.isoformat(), m_end.isoformat(), len(records)))
        counts[table] += len(records)
    return {"start": min(min(starts.values()), end).isoformat(), "end": end.isoformat(), "rows": counts}


def missing_ranges(prop):
    """Runs of dates with no stored rows, between the first and last stored day."""
    return google_auth.missing_ranges("gsc_totals", prop)


def incomplete_ranges(prop):
    """Runs of days that have site totals but whose page or query detail was never pulled successfully
    (a pull that stopped partway). Judged from gsc_pulls, which records only completed fetches; days before a
    table's first recorded pull aren't judged. [(from, to, days)]."""
    with closing(core.connect()) as c:
        days = [r[0] for r in c.execute("SELECT date FROM gsc_totals WHERE property = ? ORDER BY date", (prop,))]
        spans = {t: c.execute("SELECT start_date, end_date FROM gsc_pulls WHERE property = ? AND table_name = ?",
                              (prop, t)).fetchall() for t in ("gsc_pages", "gsc_queries")}
    covered, first = {}, {}
    for t, rows in spans.items():
        if not rows:
            continue
        first[t] = min(a for a, _ in rows)
        cov = covered[t] = set()
        for a, b in rows:
            d, stop = datetime.date.fromisoformat(a), datetime.date.fromisoformat(b)
            while d <= stop:
                cov.add(d.isoformat())
                d += datetime.timedelta(days=1)
    bad = [datetime.date.fromisoformat(d) for d in days
           if any(d >= first[t] and d not in covered[t] for t in covered)]
    runs = []
    for d in bad:
        if runs and (d - runs[-1][1]).days == 1:
            runs[-1][1] = d
        else:
            runs.append([d, d])
    return [(a, b, (b - a).days + 1) for a, b in runs]


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
