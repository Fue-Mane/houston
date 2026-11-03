"""Changes to WinMENTOR data, for the operator to make. Houston never writes to WinMENTOR.

They are made in Houston like any other change (Catalogue > Products, the Data inspection columns) and go through
Review and push. Pushing one writes nothing anywhere: it puts it on the operator's list (operator_requests) and in
the change log as "Sent to the operator". After every WinMENTOR load the pre-flight run calls verify(), which closes
a request as soon as WinMENTOR shows the new value. The list is on Data health, tab For the operator.
"""
from contextlib import closing

import core
import winmentor as wm

# the WinMENTOR columns that can be edited, as they appear in the Products table and in the review queue
EAN, NAME = "EAN in WinMENTOR", "WinMENTOR name"
SENT = "Sent to the operator"

SCHEMA = """CREATE TABLE IF NOT EXISTS operator_requests (id TEXT PRIMARY KEY, sku TEXT, product TEXT, field TEXT,
    old TEXT, new TEXT, sent_at TEXT, status TEXT, done_at TEXT);"""


def _connect():
    c = core.connect()
    if not core.READ_ONLY:
        c.executescript(SCHEMA)
    return c


def send(changes):
    """core.push hands over the changes of kind 'winmentor'. Returns (change, status, note) like core.push does."""
    if not changes:
        return []
    at = core.now()
    with closing(_connect()) as c, c:
        c.executemany("INSERT OR REPLACE INTO operator_requests VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', '')",
                      [(ch["id"], ch["target"], ch.get("target_label", ""), ch.get("field", ""),
                        core.norm(ch.get("old")), core.norm(ch.get("new")), at) for ch in changes])
    note = ("Houston doesn't write to WinMENTOR: this is on the operator's list (Data health, For the operator) "
            "and closes by itself once WinMENTOR shows the new value.")
    results = [(ch, SENT, note) for ch in changes]
    for ch, status, n in results:
        core._append_log({**ch, "pushed_at": at, "status": status, "note": n, "backup": ""})
    ids = {ch["id"] for ch in changes}
    core.save_pending([p for p in core.load_pending() if p["id"] not in ids])
    return results


def verify(c, catalogue):
    """Close the waiting requests that WinMENTOR now shows. catalogue: {SKU: {'name', 'ean'}} of the live database.
    Runs inside the pre-flight run's connection. Returns how many were closed."""
    c.executescript(SCHEMA)
    closed = []
    for rid, sku, field, new in c.execute("SELECT id, sku, field, new FROM operator_requests WHERE status = 'waiting'"):
        k = catalogue.get((sku or "").strip())
        if not k:
            continue
        if field == EAN and wm.ean(new) and k.get("ean") == wm.ean(new):
            closed.append(rid)
        elif field == NAME and (k.get("name") or "").strip() == (new or "").strip():
            closed.append(rid)
    at = core.now()
    c.executemany("UPDATE operator_requests SET status = 'done', done_at = ? WHERE id = ?", [(at, r) for r in closed])
    return len(closed)


def listing(status):
    """[(sku, product, field, old, new, sent_at, done_at)] for 'waiting' or 'done', newest first."""
    with closing(_connect()) as c:
        return c.execute("SELECT sku, product, field, old, new, sent_at, done_at FROM operator_requests "
                         "WHERE status = ? ORDER BY sent_at DESC", (status,)).fetchall()
