"""Website orders from the store (Magento), for the channel rules.

Houston keeps four things per order: order number, when it was placed, status and total with VAT.
Magento's answer also carries names, emails, addresses and phone numbers; they are left out of what is
saved and never written to disk.

Tables
- mg_orders: one row per order. created_at is UTC, as Magento sends it; placed_on is the day in Romanian time.
- mg_pulls: every update, with the dates read, the number of orders and any error.

The first update reads every order since April 2011 (WinMENTOR's first month). Later updates re-read the
last 60 days, so an order that changes status (on hold -> complete, complete -> closed) is picked up.
"""
import datetime
from contextlib import closing

import core
from magento import ApiError

FIRST_DAY = datetime.date(2011, 4, 1)
REREAD_DAYS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS mg_orders (increment_id TEXT PRIMARY KEY, created_at TEXT, placed_on TEXT, status TEXT,
    grand_total REAL, pulled_at TEXT);
CREATE INDEX IF NOT EXISTS ix_mg_orders_day ON mg_orders (placed_on);
CREATE TABLE IF NOT EXISTS mg_pulls (pulled_at TEXT, start_date TEXT, end_date TEXT, orders INTEGER, error TEXT);
"""


def connect():
    c = core.connect()
    if not core.READ_ONLY:
        c.executescript(SCHEMA)
    return c


# --- Romanian time ------------------------------------------------------------------------------
def _last_sunday(year, month):
    nxt = datetime.date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - datetime.timedelta(days=1)
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)


def _summer(utc):
    """Romania is on summer time (UTC+3) from the last Sunday of March to the last Sunday of October, 01:00 UTC."""
    start = datetime.datetime.combine(_last_sunday(utc.year, 3), datetime.time(1))
    end = datetime.datetime.combine(_last_sunday(utc.year, 10), datetime.time(1))
    return start <= utc < end


def to_local(utc):
    return utc + datetime.timedelta(hours=3 if _summer(utc) else 2)


def from_local(local):
    for hours in (3, 2):
        utc = local - datetime.timedelta(hours=hours)
        if (3 if _summer(utc) else 2) == hours:
            return utc
    return local - datetime.timedelta(hours=2)


# --- reading the store --------------------------------------------------------------------------
def _row(order, pulled_at):
    """The four kept fields of one order, plus the Romanian day it was placed."""
    number = str(order.get("increment_id") or "").strip()
    created = str(order.get("created_at") or "").strip()
    try:
        placed = to_local(datetime.datetime.strptime(created[:19], "%Y-%m-%d %H:%M:%S")).date().isoformat()
    except ValueError:
        placed = ""
    try:
        total = round(float(order.get("grand_total")), 2)
    except (TypeError, ValueError):
        total = None
    return (number, created, placed, str(order.get("status") or "").strip(), total, pulled_at)


def _months(start, end):
    d = start.replace(day=1)
    while d <= end:
        yield d
        d = (d + datetime.timedelta(days=32)).replace(day=1)


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _log(pulled_at, start, end, orders, error):
    with closing(connect()) as c, c:
        c.execute("INSERT INTO mg_pulls VALUES (?, ?, ?, ?, ?)",
                  (pulled_at, start.isoformat(), end.isoformat(), orders, error))


def update(mg, progress=lambda f, t: None, start=None, today=None):
    """Read orders from the store, one month per request. Returns {'orders', 'start', 'end'}."""
    today = today or datetime.date.today()
    if start is None:
        with closing(connect()) as c:
            newest = c.execute("SELECT MAX(placed_on) FROM mg_orders WHERE placed_on <> ''").fetchone()[0]
        start = FIRST_DAY if not newest else max(
            FIRST_DAY, datetime.date.fromisoformat(newest) - datetime.timedelta(days=REREAD_DAYS))
    pulled_at = core.now()
    months = list(_months(start, today))
    saved = 0
    try:
        for i, month in enumerate(months):
            progress(i / len(months), f"Website orders, {month:%b %Y}")
            first = max(month, start)
            after = (month + datetime.timedelta(days=32)).replace(day=1)
            lo = from_local(datetime.datetime.combine(first, datetime.time()))
            hi = from_local(datetime.datetime.combine(after, datetime.time())) - datetime.timedelta(seconds=1)
            answer = mg.call("sales_order.list", [{"created_at": {"from": _stamp(lo), "to": _stamp(hi)}}]) or []
            rows = [r for r in (_row(o, pulled_at) for o in answer) if r[0]]
            with closing(connect()) as c, c:
                c.executemany("INSERT OR REPLACE INTO mg_orders VALUES (?, ?, ?, ?, ?, ?)", rows)
            saved += len(rows)
    except ApiError as e:
        _log(pulled_at, start, today, saved, str(e))
        raise
    _log(pulled_at, start, today, saved, "")
    progress(1.0, "Done")
    return {"orders": saved, "start": start.isoformat(), "end": today.isoformat()}


# --- for the channel rules and the sidebar --------------------------------------------------------
def for_matching(c):
    """[(order number, placed_on, status, total)] on connection c; [] before the first update."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mg_orders'").fetchone():
        return []
    return c.execute("SELECT increment_id, placed_on, status, grand_total FROM mg_orders "
                     "WHERE placed_on <> '' AND grand_total IS NOT NULL").fetchall()


def info():
    with closing(connect()) as c:
        n, newest = c.execute("SELECT COUNT(*), MAX(placed_on) FROM mg_orders").fetchone()
        last = c.execute("SELECT pulled_at, orders, error FROM mg_pulls ORDER BY pulled_at DESC LIMIT 1").fetchone()
    return {"orders": n or 0, "newest": newest or "", "last_pull": last[0] if last else "",
            "last_error": (last[2] or "") if last else ""}
