"""Google Analytics 4: pulls into data/houston.db, daily.

- ga4_totals       whole site per day. To check against GA4's own reports.
- ga4_landing      per landing page and channel (Organic Search, Direct, Paid Search ...).
- ga4_items        per product as the shop sends it to GA4 (item ID and name).
- ga4_site_search  searches typed into the shop's own search box (view_search_results events).
Every pull is recorded in ga4_pulls, every refusal in ga4_pull_errors. Each table resumes where its own
pulls ended, so a pull that stopped halfway is finished by the next Update.

Visits that start on Magento's action links (add to compare / wishlist / stock alert / send to a friend)
are stored as one line per day and channel, landing_page = '(Magento action links)'. Each such link is
unique, so storing them one by one only filled the table; the totals stay the same.
"""
import datetime
import time
from contextlib import closing

import core
import google_auth
from google_auth import GoogleError  # noqa: F401

SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
HISTORY_DAYS = 486
PAGE = 100000

ACTION_LINKS = "(Magento action links)"
ACTION_PREFIXES = ["/catalog/product_compare/", "/wishlist/", "/productalert/", "/sendfriend/"]
LOW_TOKENS = 1500     # stop before Google's hourly allowance runs out


def _prefix_filter(field):
    return {"orGroup": {"expressions": [
        {"filter": {"fieldName": field, "stringFilter": {"matchType": "BEGINS_WITH", "value": p}}}
        for p in ACTION_PREFIXES]}}


class QuotaReached(Exception):
    pass


SESSION_METRICS = ["sessions", "activeUsers", "engagedSessions", "keyEvents", "ecommercePurchases", "purchaseRevenue"]
REPORTS = {
    "ga4_totals": {"dims": ["date"], "metrics": SESSION_METRICS},
    "ga4_landing": {"dims": ["date", "landingPage", "sessionDefaultChannelGroup"], "metrics": SESSION_METRICS,
                    "filter": {"notExpression": _prefix_filter("landingPage")},
                    "also": {"dims": ["date", "sessionDefaultChannelGroup"], "metrics": SESSION_METRICS,
                             "filter": _prefix_filter("landingPage"), "insert_dim": ACTION_LINKS}},
    "ga4_items": {"dims": ["date", "itemId", "itemName"],
                  "metrics": ["itemsViewed", "itemsAddedToCart", "itemsPurchased", "itemRevenue"]},
    "ga4_site_search": {"dims": ["date", "searchTerm", "eventName"], "metrics": ["eventCount"],
                        "filter": {"filter": {"fieldName": "eventName",
                                              "stringFilter": {"matchType": "EXACT", "value": "view_search_results"}}},
                        "drop_last_dim": True},
}


def status():
    return google_auth.status(SCOPE)


def services():
    from googleapiclient.discovery import build
    creds = google_auth.credentials()
    return (build("analyticsdata", "v1beta", credentials=creds, cache_discovery=False),
            build("analyticsadmin", "v1beta", credentials=creds, cache_discovery=False))


def properties(admin=None):
    """[(property, label)] e.g. ('properties/390051981', 'GA4 - Biomania · www.biomania.ro')."""
    admin = admin or services()[1]
    out, token = [], None
    while True:
        resp = google_auth.execute(admin.accountSummaries().list(pageSize=200, pageToken=token), "Analytics Admin")
        for acc in resp.get("accountSummaries", []):
            for p in acc.get("propertySummaries", []):
                out.append((p["property"], f"{p.get('displayName', '')} · {acc.get('displayName', '')}"))
        token = resp.get("nextPageToken")
        if not token:
            return out


def latest_date(prop):
    with closing(core.connect()) as c:
        row = c.execute("SELECT MAX(date) FROM ga4_totals WHERE property = ?", (prop,)).fetchone()
    return row[0] if row and row[0] else None


def _months(start, end):
    out, cur = [], start
    while cur <= end:
        nxt = (cur.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        out.append((cur, min(end, nxt - datetime.timedelta(days=1))))
        cur = nxt
    return out


def _is_quota(err):
    text = str(err).lower()
    return "exhausted" in text and ("per hour" in text or "per day" in text or "tokens" in text)


def _fetch(data, prop, start, end, spec, quota):
    rows, offset = [], 0
    while True:
        body = {"dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
                "dimensions": [{"name": d} for d in spec["dims"]],
                "metrics": [{"name": m} for m in spec["metrics"]],
                "limit": PAGE, "offset": offset, "returnPropertyQuota": True}
        if spec.get("filter"):
            body["dimensionFilter"] = spec["filter"]
        try:
            resp = google_auth.execute(data.properties().runReport(property=prop, body=body), "Google Analytics",
                                       attempts=4, sleep=lambda s: time.sleep(s), give_up=_is_quota)
        except GoogleError as e:
            if _is_quota(e):
                raise QuotaReached(f"Google's Analytics allowance for this hour (or day) is used up. Google said: {e}") from None
            raise
        q = resp.get("propertyQuota") or {}
        for kind in ("tokensPerHour", "tokensPerDay"):
            left = (q.get(kind) or {}).get("remaining")
            if left is not None:
                quota[kind] = int(left)
        if quota.get("tokensPerHour", LOW_TOKENS + 1) < LOW_TOKENS or quota.get("tokensPerDay", LOW_TOKENS + 1) < LOW_TOKENS:
            quota["hit"] = True
        batch = resp.get("rows", [])
        rows += batch
        total = int(resp.get("rowCount", 0) or 0)
        offset += len(batch)
        if not batch or offset >= total:
            return rows


def _value(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    return int(f) if f.is_integer() else f


def _record(prop, row, spec, pulled_at):
    dims = [d.get("value", "") for d in row.get("dimensionValues", [])]
    if spec.get("drop_last_dim"):
        dims = dims[:-1]
    if spec.get("insert_dim"):
        dims = dims[:1] + [spec["insert_dim"]] + dims[1:]
    d = dims[0]
    dims[0] = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d
    mets = [_value(m.get("value")) for m in row.get("metricValues", [])]
    return (prop, *dims, *mets, pulled_at)


def _table_resume(prop, table, today):
    with closing(core.connect()) as c:
        row = c.execute("SELECT MAX(end_date) FROM ga4_pulls WHERE property = ? AND table_name = ?",
                        (prop, table)).fetchone()
    if row and row[0]:
        return datetime.date.fromisoformat(row[0]) - datetime.timedelta(days=3)
    return today - datetime.timedelta(days=HISTORY_DAYS)


def _log_error(prop, table, m_start, m_end, pulled_at, error):
    with closing(core.connect()) as c, c:
        c.execute("INSERT INTO ga4_pull_errors VALUES (?, ?, ?, ?, ?, ?)",
                  (pulled_at, prop, table, m_start.isoformat(), m_end.isoformat(), str(error)))


def pull(prop, start=None, end=None, progress=lambda f, t: None, data=None):
    """Pull into the four tables, replacing rows already stored for those dates.
    With no start given, each table resumes where its own pulls ended (the first time: about 16 months).
    Stops cleanly when Google's hourly or daily Analytics allowance is nearly used up."""
    data = data or services()[0]
    today = datetime.date.today()
    end = end or today - datetime.timedelta(days=1)
    plan = {t: _months(start or _table_resume(prop, t, today), end) for t in REPORTS}
    pulled_at = core.now()
    counts, errors, quota = {t: 0 for t in REPORTS}, {}, {}
    steps, step, stopped = sum(len(m) for m in plan.values()), 0, None
    for table, spec in REPORTS.items():
        for m_start, m_end in plan[table]:
            step += 1
            progress(step / max(steps, 1), f"{table}: {m_start:%b %Y}")
            if stopped or table in errors:
                continue
            if quota.get("hit"):
                stopped = (f"Google's Analytics allowance is nearly used up ({quota.get('tokensPerHour', '?')} tokens "
                           "left this hour). Press Update again in an hour: Houston continues where it stopped.")
                continue
            try:
                rows = _fetch(data, prop, m_start, m_end, spec, quota)
                records = [_record(prop, r, spec, pulled_at) for r in rows]
                if spec.get("also"):
                    extra = spec["also"]
                    records += [_record(prop, r, extra, pulled_at) for r in _fetch(data, prop, m_start, m_end, extra, quota)]
            except QuotaReached as e:
                _log_error(prop, table, m_start, m_end, pulled_at, e)
                stopped = f"{e} Press Update again in an hour: Houston continues where it stopped."
                continue
            except GoogleError as e:
                _log_error(prop, table, m_start, m_end, pulled_at, e)
                errors[table] = str(e)
                continue
            width = 1 + (len(spec["dims"]) - (1 if spec.get("drop_last_dim") else 0)) + len(spec["metrics"]) + 1
            bad = [r for r in records if len(r) != width]
            if bad:  # an answer shaped differently from what was asked: store nothing for this month, say why
                _log_error(prop, table, m_start, m_end, pulled_at, f"unexpected answer shape: {bad[0]!r}")
                errors[table] = "Google's answer had an unexpected shape; nothing stored for this month."
                continue
            with closing(core.connect()) as c, c:
                c.execute(f"DELETE FROM {table} WHERE property = ? AND date BETWEEN ? AND ?",
                          (prop, m_start.isoformat(), m_end.isoformat()))
                c.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({', '.join('?' * width)})", records)
                c.execute("INSERT INTO ga4_pulls VALUES (?, ?, ?, ?, ?, ?)",
                          (pulled_at, prop, table, m_start.isoformat(), m_end.isoformat(), len(records)))
            counts[table] += len(records)
    first = min((m[0][0] for m in plan.values() if m), default=end)
    return {"start": first.isoformat(), "end": end.isoformat(), "rows": counts, "errors": errors,
            "stopped": stopped, "tokens_left_this_hour": quota.get("tokensPerHour")}


def missing_ranges(prop):
    with closing(core.connect()) as c:
        days = [datetime.date.fromisoformat(r[0]) for r in
                c.execute("SELECT date FROM ga4_totals WHERE property = ? ORDER BY date", (prop,))]
    return [(a + datetime.timedelta(days=1), b - datetime.timedelta(days=1), (b - a).days - 1)
            for a, b in zip(days, days[1:]) if (b - a).days > 1]


def totals(prop, start, end):
    with closing(core.connect()) as c:
        r = c.execute("SELECT COUNT(*), SUM(sessions), SUM(active_users), SUM(engaged_sessions), SUM(purchases), "
                      "SUM(revenue) FROM ga4_totals WHERE property = ? AND date BETWEEN ? AND ?",
                      (prop, start, end)).fetchone()
    if not r[0]:
        return None
    return {"days": r[0], "sessions": r[1] or 0, "active_users_sum": r[2] or 0, "engaged_sessions": r[3] or 0,
            "purchases": r[4] or 0, "revenue": r[5] or 0.0}
