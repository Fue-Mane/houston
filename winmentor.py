"""WinMENTOR data: the courier on the office PC -> Google Cloud Storage bucket -> data/houston.db.

The office PC uploads one zip per run (daily/yyyy/MM/<stamp>.zip) and, once, the history
(history/<database>/<yyyy_MM>.zip and history/<database>/_lists.zip). Houston reads the bucket with the
shared Google sign-in, read-only, and loads every zip once. Nothing is ever written to the bucket.

Rules
- Raw first: every line is stored exactly as WinMENTOR sent it, in columns c0..c44 of wm_<set>, with its
  database (company), period, source file and run time. Meanings live in the helpers below and can change
  without reloading anything.
- Monthly lists (sales, receptions, goods in, transfers, money in and out): the newest copy of each month
  wins. When a newer copy differs from the stored one, the difference is noted in wm_revisions.
- Stock lists: one copy per day, the day's last run (period = the run's date).
- Other lists (catalogue, customers, prices, balances, warehouses, price categories): only the newest copy.
- A list the courier couldn't read in a run (e.g. sales while WinMENTOR was open) never replaces a good copy.
- wm_codes: WinMENTOR internal code (SKU) <-> EAN for each database, from WinMENTOR's own lists.
  Supplier work uses EANs only: suppliers change their own codes too often (Stefan, Oct 2026).
"""
import datetime
import hashlib
import io
import json
import re
import zipfile
from collections import Counter, defaultdict
from contextlib import closing

import core
import google_auth
from google_auth import GoogleError  # noqa: F401  (used by the pages)

SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"
BUCKET = "bwe"
NCOLS = 45
MONTHLY = ("sales_ext", "sales_month", "receptions", "goods_in", "transfers", "money_in", "money_out")
DAILY_KEPT = ("stock", "stock_ean", "stock_cost", "stock_lots")
LATEST_ONLY = ("catalogue", "catalogue_ean", "customers", "special_prices", "customer_balances",
               "supplier_balances", "warehouses", "price_categories")
SETS = MONTHLY + DAILY_KEPT + LATEST_ONLY
# Stefan's rule (Oct 2026): a product's stock is its Stock in these two warehouses; all others are left out
STOCK_WAREHOUSES = ("DEPOZIT CENTRAL", "VANZARI EN-GROS")

# Column positions the helpers rely on (0-based), measured on BIO2025 in October 2026.
# *_ean files were read with EAN codes, everything else with internal codes (SKUs).
COL = {
    "stock_ean": {"ean": 0, "name": 1, "qty": 4, "warehouse": 12, "sku": 18},
    "stock_cost": {"warehouse": 0, "name": 2, "code": 3, "qty": 6, "cost": 8},
    "stock_lots": {"code": 0, "name": 1, "qty": 4, "supplier": 10, "warehouse": 12, "date": 17},
    "sales_ext": {"day": 1, "doc": 2, "code": 3, "qty": 4, "price": 6},
    "catalogue": {"code": 0, "name": 1},
}

TABLE_NOTES = {
    "wm_files": "WinMENTOR: every zip loaded from the bucket (name, upload time, run time, database, status, "
                "the courier's manifest). Status 'failed' files are retried on the next update.",
    "wm_versions": "WinMENTOR: which copy of each list is stored, per list, database and period (run time, lines).",
    "wm_revisions": "WinMENTOR: months that changed when a newer copy arrived (lines before/after/changed).",
    "wm_codes": "WinMENTOR internal code (sku) <-> EAN per database, built from WinMENTOR's own lists.",
    "wm_order_layouts": "Saved layouts of suppliers' order forms (which column holds EAN, order, pieces/carton).",
    "wm_ean_links": "Supplier EANs that WinMENTOR holds under another EAN, linked by hand on the Orders page. "
                    "factor = WinMENTOR units per supplier piece (1 when they are the same unit).",
    "wm_sales_by_ean_month": "View. WinMENTOR sales per EAN, database and month (qty = sum of quantity, "
                             "from GetVanzariExt column 4). Months in two databases appear once per database.",
}
for _s in SETS:
    TABLE_NOTES[f"wm_{_s}"] = (f"WinMENTOR raw lines, list '{_s}', exactly as sent: c0..c44 are WinMENTOR's columns. "
                               "period = month (yyyy_MM) for monthly lists, the day for stock lists, '' otherwise.")

VIEWS = {
    "wm_sales_by_ean_month": """CREATE VIEW wm_sales_by_ean_month AS
        SELECT k.ean, s.company, s.period AS month, SUM(CAST(REPLACE(s.c4, ',', '.') AS REAL)) AS qty,
               COUNT(*) AS lines
        FROM wm_sales_ext s JOIN wm_codes k ON k.company = s.company AND k.sku = TRIM(s.c3)
        GROUP BY k.ean, s.company, s.period""",
}


def _schema():
    cols = ", ".join(f"c{i} TEXT" for i in range(NCOLS))
    parts = []
    for s in SETS:
        parts.append(f"CREATE TABLE IF NOT EXISTS wm_{s} (company TEXT, period TEXT, src TEXT, run_at TEXT, "
                     f"line INTEGER, {cols});")
        parts.append(f"CREATE INDEX IF NOT EXISTS ix_wm_{s} ON wm_{s} (company, period);")
    parts += [
        "CREATE INDEX IF NOT EXISTS ix_wm_sales_ext_code ON wm_sales_ext (company, c3);",
        """CREATE TABLE IF NOT EXISTS wm_files (name TEXT PRIMARY KEY, size INTEGER, uploaded TEXT, mode TEXT,
            company TEXT, run_at TEXT, loaded_at TEXT, status TEXT, note TEXT, manifest TEXT);""",
        """CREATE TABLE IF NOT EXISTS wm_versions (set_name TEXT, company TEXT, period TEXT, run_at TEXT, src TEXT,
            lines INTEGER, digest TEXT, PRIMARY KEY (set_name, company, period));""",
        """CREATE TABLE IF NOT EXISTS wm_revisions (set_name TEXT, company TEXT, period TEXT, old_run_at TEXT,
            new_run_at TEXT, old_lines INTEGER, new_lines INTEGER, lines_changed INTEGER, noted_at TEXT);""",
        """CREATE TABLE IF NOT EXISTS wm_codes (company TEXT, sku TEXT, ean TEXT, name TEXT, source TEXT,
            PRIMARY KEY (company, sku));""",
        "CREATE INDEX IF NOT EXISTS ix_wm_codes_ean ON wm_codes (ean);",
        """CREATE TABLE IF NOT EXISTS wm_order_layouts (supplier TEXT PRIMARY KEY, signature TEXT, layout TEXT,
            saved_at TEXT);""",
        """CREATE TABLE IF NOT EXISTS wm_ean_links (supplier_ean TEXT PRIMARY KEY, winmentor_ean TEXT, factor REAL,
            note TEXT, saved_at TEXT);""",
    ]
    return "\n".join(parts)


_ready = set()


def connect():
    """core.connect() plus the WinMENTOR tables (created on Houston's first use; the MCP server only reads)."""
    c = core.connect()
    key = str(core.db_path())
    if not core.READ_ONLY and key not in _ready:
        c.executescript(_schema())
        for name, sql in VIEWS.items():
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone():
                c.execute(sql)
        c.commit()
        _ready.add(key)
    return c


# --- values ------------------------------------------------------------------------
def num(v):
    """WinMENTOR numbers use a decimal comma: '10,6' -> 10.6, '240' -> 240.0, '' -> 0.0."""
    s = str(v if v is not None else "").strip().replace(" ", "")
    if not s:
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def ean(v):
    """Digits only, as EAN-13 (or EAN-8), or '' when it isn't a valid EAN/GTIN (correct check digit).
    Works for numbers, text, and numbers Excel stored as floats."""
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, float):
        if v != v or v != int(v):
            return ""
        v = f"{v:.0f}"
    d = re.sub(r"\D", "", str(v))
    if len(d) not in (8, 12, 13, 14):
        return ""
    body, check = d[:-1], int(d[-1])
    total = sum(int(x) * (3 if i % 2 == 0 else 1) for i, x in enumerate(reversed(body)))
    if (10 - total % 10) % 10 != check:
        return ""
    if len(d) == 12:
        d = "0" + d                      # UPC-A is EAN-13 with a leading zero
    elif len(d) == 14 and d[0] == "0":
        d = d[1:]
    return d


def month_add(month, k):
    y, m = (int(x) for x in month.split("_"))
    i = y * 12 + (m - 1) + k
    return f"{i // 12:04d}_{i % 12 + 1:02d}"


def month_range(first, last):
    """Every 'yyyy_MM' month from first to last, both included."""
    out, m = [], first
    while m <= last:
        out.append(m)
        m = month_add(m, 1)
    return out


def month_label(month):
    """'2026_10' -> 'Oct 2026'."""
    return datetime.date(int(month[:4]), int(month[5:]), 1).strftime("%b %Y")


def full_months_before(today, n):
    """The n full months before today's month, oldest first: 22 Oct 2026, 3 -> 2026_07, 2026_08, 2026_09."""
    cur = f"{today.year:04d}_{today.month:02d}"
    return [month_add(cur, -i) for i in range(n, 0, -1)]


# --- bucket --------------------------------------------------------------------------
def status_signin():
    return google_auth.status(SCOPE)


def bucket():
    return core.setting("wm_bucket") or BUCKET


def service():
    from googleapiclient.discovery import build
    return build("storage", "v1", credentials=google_auth.credentials(), cache_discovery=False)


def list_files(svc, name):
    out = []
    for prefix in ("history/", "daily/"):
        token = None
        while True:
            req = svc.objects().list(bucket=name, prefix=prefix, pageToken=token,
                                     fields="items(name,size,updated),nextPageToken")
            resp = google_auth.execute(req, "Cloud Storage")
            out += resp.get("items", [])
            token = resp.get("nextPageToken")
            if not token:
                break
    return [i for i in out if i["name"].endswith(".zip")]


def download(svc, name, obj):
    return google_auth.execute(svc.objects().get_media(bucket=name, object=obj), "Cloud Storage")


# --- loading ---------------------------------------------------------------------------
def _lines(data):
    rows = []
    for raw in data.decode("utf-8-sig").split("\n"):
        raw = raw.rstrip("\r")
        if not raw:
            continue
        parts = raw.split(";")
        if len(parts) > NCOLS:
            parts = parts[:NCOLS - 1] + [";".join(parts[NCOLS - 1:])]
        rows.append(parts)
    return rows


def _key(parts):
    """A line as a comparable tuple, trailing empty columns ignored."""
    p = list(parts)
    while p and (p[-1] is None or p[-1] == ""):
        p.pop()
    return tuple("" if x is None else x for x in p)


def _apply(c, set_name, company, period, run_at, src, rows):
    """Store one list copy under the rules. Returns 'stored', 'older' or 'revised'."""
    old = c.execute("SELECT run_at, lines, digest FROM wm_versions WHERE set_name = ? AND company = ? AND period = ?",
                    (set_name, company, period)).fetchone()
    if old and old[0] >= run_at:
        return "older"
    keys = [_key(r) for r in rows]
    digest = hashlib.sha1("\n".join(sorted(";".join(k) for k in keys)).encode("utf-8")).hexdigest()
    result = "stored"
    cols = ", ".join(f"c{i}" for i in range(NCOLS))
    if old and set_name in MONTHLY and old[2] != digest and period < run_at[:7].replace("-", "_"):
        before = Counter(_key(r) for r in c.execute(
            f"SELECT {cols} FROM wm_{set_name} WHERE company = ? AND period = ?", (company, period)))
        after = Counter(keys)
        changed = sum((after - before).values()) + sum((before - after).values())
        c.execute("INSERT INTO wm_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (set_name, company, period, old[0], run_at, old[1], len(rows), changed, core.now()))
        result = "revised"
    c.execute(f"DELETE FROM wm_{set_name} WHERE company = ? AND period = ?", (company, period))
    marks = ", ".join("?" * (5 + NCOLS))
    c.executemany(f"INSERT INTO wm_{set_name} VALUES ({marks})",
                  [(company, period, src, run_at, i, *(list(r) + [None] * (NCOLS - len(r))))
                   for i, r in enumerate(rows)])
    c.execute("INSERT OR REPLACE INTO wm_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
              (set_name, company, period, run_at, src, len(rows), digest))
    return result


def _jobs(manifest, names):
    """(file inside the zip, list, database, period, error) for every list in one courier zip."""
    mode, company = manifest.get("mode"), manifest.get("company", "")
    run_at = manifest.get("finished") or manifest.get("started") or ""
    files = manifest.get("files", {})
    out = []
    for fname in names:
        if fname == "manifest.json" or not fname.endswith(".txt"):
            continue
        err = (files.get(fname) or {}).get("error")
        stem = fname[:-4]
        if mode == "normal":
            if stem.startswith("snapshot-"):
                s = stem[len("snapshot-"):]
                out.append((fname, s, company, run_at[:10] if s in DAILY_KEPT else "", err))
            elif re.match(r"^\d{4}_\d{2}-", stem):
                m, s = stem[:7], stem[8:]
                comp = company if m == manifest.get("month") else (manifest.get("previous_month_company") or company)
                out.append((fname, s, comp, m, err))
        elif mode == "backfill":
            out.append((fname, stem, company, manifest.get("month", ""), err))
        elif mode == "backfill-lists":
            out.append((fname, stem, company, "", err))
    return out


def _load_zip(c, item, data):
    z = zipfile.ZipFile(io.BytesIO(data))
    manifest = json.loads(z.read("manifest.json").decode("utf-8-sig"))
    run_at = manifest.get("finished") or manifest.get("started") or ""
    if not run_at:
        raise ValueError("manifest has no run time")
    counts = Counter()
    notes = []
    for fname, s, company, period, err in _jobs(manifest, z.namelist()):
        if s not in SETS:
            notes.append(f"{fname}: unknown list, skipped")
            continue
        if err:
            counts["not read by the courier"] += 1
            notes.append(f"{fname}: not read by the courier ({err[:80]})")
            continue
        rows = _lines(z.read(fname))
        expected = (manifest.get("files", {}).get(fname) or {}).get("lines")
        if expected is not None and expected != len(rows):
            notes.append(f"{fname}: {len(rows)} lines, manifest says {expected}")
        counts[_apply(c, s, company, period, run_at, item["name"], rows)] += 1
    c.execute("INSERT OR REPLACE INTO wm_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (item["name"], int(item.get("size") or 0), item.get("updated", ""), manifest.get("mode", ""),
               manifest.get("company", ""), run_at, core.now(), "loaded", "; ".join(notes),
               json.dumps(manifest, ensure_ascii=False)))
    return counts, manifest.get("company", "")


def rebuild_codes(c, company):
    """Internal code <-> EAN for one database, from WinMENTOR's own lists:
    1. the newest stock list read with EAN codes carries both codes on the same line (EAN c0, code c18);
    2. the catalogue is sent twice, with EAN codes and with internal codes, in the same order: lines are paired
       only when both copies show the same product name."""
    pairs = {}
    day = c.execute("SELECT MAX(period) FROM wm_stock_ean WHERE company = ?", (company,)).fetchone()[0]
    if day:
        for e, name, sku in c.execute("SELECT c0, c1, c18 FROM wm_stock_ean WHERE company = ? AND period = ?",
                                      (company, day)):
            e, sku = ean(e), (sku or "").strip()     # trimmed only to join WinMENTOR lists with each other
            if e and sku:
                pairs.setdefault(sku, (e, name or "", "stock"))
    by_line = {line: (e, name) for line, e, name in c.execute(
        "SELECT line, c0, c1 FROM wm_catalogue_ean WHERE company = ? AND period = ''", (company,))}
    for line, sku, name in c.execute("SELECT line, c0, c1 FROM wm_catalogue WHERE company = ? AND period = ''",
                                     (company,)):
        other = by_line.get(line)
        sku = (sku or "").strip()
        if other and sku and (other[1] or "").strip() == (name or "").strip():
            e = ean(other[0])
            if e:
                pairs.setdefault(sku, (e, name or "", "catalogue"))
    c.execute("DELETE FROM wm_codes WHERE company = ?", (company,))
    c.executemany("INSERT INTO wm_codes VALUES (?, ?, ?, ?, ?)",
                  [(company, sku, e, name, src) for sku, (e, name, src) in pairs.items()])
    return len(pairs)


def _order(item):
    n = item["name"]
    return (0 if n.startswith("history/") else 1, n)


def update(progress=lambda f, t: None, svc=None):
    """Load every zip in the bucket that isn't loaded yet. Safe to run again at any time."""
    svc = svc or service()
    name = bucket()
    items = list_files(svc, name)
    with closing(connect()) as c:
        done = {r[0] for r in c.execute("SELECT name FROM wm_files WHERE status = 'loaded'")}
    new = sorted((i for i in items if i["name"] not in done), key=_order)
    res = {"in_bucket": len(items), "new": len(new), "loaded": 0, "failed": 0, "stored": 0, "revised": 0,
           "older": 0, "not_read": 0, "errors": [], "codes": {}}
    touched = set()
    for n, item in enumerate(new, 1):
        progress(n / max(len(new), 1), f"{n} of {len(new)}: {item['name']}")
        try:
            data = download(svc, name, item["name"])
            with closing(connect()) as c, c:
                counts, company = _load_zip(c, item, data)
            res["loaded"] += 1
            res["stored"] += counts["stored"]
            res["revised"] += counts["revised"]
            res["older"] += counts["older"]
            res["not_read"] += counts["not read by the courier"]
            touched.add(company)
        except Exception as e:
            res["failed"] += 1
            res["errors"].append(f"{item['name']}: {e}")
            with closing(connect()) as c, c:
                c.execute("INSERT OR REPLACE INTO wm_files (name, size, uploaded, loaded_at, status, note) "
                          "VALUES (?, ?, ?, ?, 'failed', ?)",
                          (item["name"], int(item.get("size") or 0), item.get("updated", ""), core.now(), str(e)[:500]))
    with closing(connect()) as c, c:
        for company in sorted(x for x in touched if x):
            res["codes"][company] = rebuild_codes(c, company)
    progress(1.0, "Done")
    return res


# --- reading, for the pages -------------------------------------------------------------
def status():
    with closing(connect()) as c:
        last = c.execute("SELECT name, run_at, company, manifest, note FROM wm_files WHERE mode = 'normal' "
                         "AND status = 'loaded' ORDER BY run_at DESC LIMIT 1").fetchone()
        counts = dict(c.execute("SELECT status, COUNT(*) FROM wm_files GROUP BY status").fetchall())
        failed = c.execute("SELECT name, loaded_at, note FROM wm_files WHERE status = 'failed' "
                           "ORDER BY loaded_at DESC LIMIT 20").fetchall()
    out = {"files": counts, "failed": failed, "last_delivery": None}
    if last:
        m = json.loads(last[3] or "{}")
        out["last_delivery"] = {"name": last[0], "run_at": last[1], "company": last[2], "note": last[4],
                                "errors": {f: v.get("error") for f, v in m.get("files", {}).items() if v.get("error")},
                                "at_sign": m.get("customers_with_at_sign", {})}
    return out


def live_company(c):
    row = c.execute("SELECT company FROM wm_files WHERE mode = 'normal' AND status = 'loaded' "
                    "ORDER BY run_at DESC LIMIT 1").fetchone()
    if row and row[0]:
        return row[0]
    row = c.execute("SELECT company FROM wm_stock_ean ORDER BY period DESC LIMIT 1").fetchone()
    return row[0] if row else None


def stock_by_ean(company=None):
    """(day of the stock photo, {ean: {'name', 'total', 'by_wh'}}) from the newest photo of the live database.
    total = Depozit central + VANZARI EN-GROS only."""
    k = COL["stock_ean"]
    with closing(connect()) as c:
        company = company or live_company(c)
        if not company:
            return None, {}, None
        day = c.execute("SELECT MAX(period) FROM wm_stock_ean WHERE company = ?", (company,)).fetchone()[0]
        rows = c.execute(f"SELECT c{k['ean']}, c{k['name']}, c{k['qty']}, c{k['warehouse']} FROM wm_stock_ean "
                         "WHERE company = ? AND period = ?", (company, day)).fetchall() if day else []
    out = {}
    for e, name, qty, wh in rows:
        e = ean(e)
        if not e:
            continue
        r = out.setdefault(e, {"name": name or "", "total": 0.0, "by_wh": defaultdict(float)})
        wh = (wh or "").strip().upper()
        r["by_wh"][wh] += num(qty)
        if wh in STOCK_WAREHOUSES:
            r["total"] += num(qty)
    return day, out, company


def month_owner(c):
    """{month: database} for sales months. A month held by two databases is taken from the newer one: BIOMANIA
    and BIO2019 hold identical copies of Jan 2019 - Jun 2020 and BIOMANIA stops partway through Jul 2020
    (measured 21 Oct 2026), so those months come from BIO2019 (Stefan's rule). Nothing is ever added up."""
    newest = dict(c.execute("SELECT company, MAX(period) FROM wm_versions WHERE set_name = 'sales_ext' "
                            "GROUP BY company").fetchall())
    owner = {}
    for company, month in c.execute("SELECT company, period FROM wm_versions WHERE set_name = 'sales_ext'"):
        if month not in owner or newest.get(company, "") > newest.get(owner[month], ""):
            owner[month] = company
    return owner


def window_sales(eans, months):
    """({(ean, month): qty}, [months with no sales list loaded]) for the given EANs and months."""
    eans = sorted(set(e for e in eans if e))
    out = {}
    with closing(connect()) as c:
        owner = month_owner(c)
        missing = [m for m in months if m not in owner]
        for i in range(0, len(eans), 400):
            part = eans[i:i + 400]
            q = (f"SELECT ean, company, month, qty FROM wm_sales_by_ean_month WHERE ean IN ({','.join('?' * len(part))}) "
                 f"AND month IN ({','.join('?' * len(months))})")
            for e, company, month, qty in c.execute(q, [*part, *months]):
                if owner.get(month) == company:
                    out[(e, month)] = out.get((e, month), 0.0) + (qty or 0.0)
    return out, missing


def product_details(e):
    """Everything Houston knows about one EAN, for the product window."""
    with closing(connect()) as c:
        company = live_company(c)
        codes = c.execute("SELECT company, sku, name, source FROM wm_codes WHERE ean = ? ORDER BY company",
                          (e,)).fetchall()
        sales = c.execute("SELECT company, month, qty, lines FROM wm_sales_by_ean_month WHERE ean = ? ORDER BY month",
                          (e,)).fetchall()
        owner = month_owner(c)
        skus = [s for comp, s, _, _ in codes if comp == company]
        lots, cost = [], []
        if company and skus:
            marks = ",".join("?" * len(skus))
            kl, kc = COL["stock_lots"], COL["stock_cost"]
            day = c.execute("SELECT MAX(period) FROM wm_stock_lots WHERE company = ?", (company,)).fetchone()[0]
            if day:
                lots = c.execute(f"SELECT c{kl['warehouse']}, c{kl['qty']}, c{kl['date']}, c{kl['supplier']} "
                                 f"FROM wm_stock_lots WHERE company = ? AND period = ? AND TRIM(c{kl['code']}) IN ({marks})",
                                 (company, day, *skus)).fetchall()
            day = c.execute("SELECT MAX(period) FROM wm_stock_cost WHERE company = ?", (company,)).fetchone()[0]
            if day:
                cost = c.execute(f"SELECT c{kc['warehouse']}, c{kc['qty']}, c{kc['cost']} FROM wm_stock_cost "
                                 f"WHERE company = ? AND period = ? AND TRIM(c{kc['code']}) IN ({marks})",
                                 (company, day, *skus)).fetchall()
    day, stock, _ = stock_by_ean(company)
    return {"ean": e, "company": company, "codes": codes, "stock_day": day, "stock": stock.get(e),
            "sales": sales, "owner": owner, "lots": lots, "cost": cost}


def coverage():
    """Rows, databases and periods per raw list, for the WinMENTOR page."""
    with closing(connect()) as c:
        out = []
        for s in SETS:
            n, comps, first, last = c.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT company), MIN(NULLIF(period, '')), MAX(NULLIF(period, '')) "
                f"FROM wm_{s}").fetchone()
            out.append({"list": s, "rows": n, "databases": comps, "first": first or "", "last": last or ""})
        codes = c.execute("SELECT company, COUNT(*) FROM wm_codes GROUP BY company").fetchall()
        unmatched = c.execute("SELECT s.company, COUNT(*) FROM wm_sales_ext s LEFT JOIN wm_codes k "
                              "ON k.company = s.company AND k.sku = TRIM(s.c3) WHERE k.sku IS NULL "
                              "GROUP BY s.company").fetchall()
        revisions = c.execute("SELECT set_name, company, period, old_run_at, new_run_at, old_lines, new_lines, "
                              "lines_changed FROM wm_revisions WHERE period < REPLACE(SUBSTR(new_run_at, 1, 7), '-', '_') "
                              "ORDER BY noted_at DESC LIMIT 50").fetchall()
    return {"lists": out, "codes": codes, "unmatched_sales_lines": unmatched, "revisions": revisions}


def saved_layouts():
    with closing(connect()) as c:
        return [(s, sig, json.loads(lay), at) for s, sig, lay, at in
                c.execute("SELECT supplier, signature, layout, saved_at FROM wm_order_layouts ORDER BY supplier")]


def save_layout(supplier, signature, layout):
    with closing(connect()) as c, c:
        c.execute("INSERT OR REPLACE INTO wm_order_layouts VALUES (?, ?, ?, ?)",
                  (supplier, signature, json.dumps(layout), core.now()))


def today():
    return datetime.date.today()


# --- names, links and period totals, for the Orders page ---------------------------------------
def winmentor_products(company=None):
    """{ean: (name, internal code)} for every product the live database knows, from its codes, catalogue and
    stock lists."""
    out = {}
    with closing(connect()) as c:
        company = company or live_company(c)
        if not company:
            return out
        for e, sku, name in c.execute("SELECT ean, sku, name FROM wm_codes WHERE company = ?", (company,)):
            out.setdefault(e, (name or "", sku or ""))
        for raw, name in c.execute("SELECT c0, c1 FROM wm_catalogue_ean WHERE company = ? AND period = ''", (company,)):
            e = ean(raw)
            if e:
                out.setdefault(e, (name or "", ""))
        day = c.execute("SELECT MAX(period) FROM wm_stock_ean WHERE company = ?", (company,)).fetchone()[0]
        if day:
            for raw, name, sku in c.execute("SELECT c0, c1, c18 FROM wm_stock_ean WHERE company = ? AND period = ?",
                                            (company, day)):
                e = ean(raw)
                if e:
                    out.setdefault(e, (name or "", (sku or "").strip()))
    return out


def codes_to_eans(company=None):
    """{internal code: ean} for the live database (used to read the supplier's own code column as a hint)."""
    with closing(connect()) as c:
        company = company or live_company(c)
        return dict(c.execute("SELECT sku, ean FROM wm_codes WHERE company = ?", (company,)).fetchall()) if company else {}


def magento_names(eans):
    """{ean: Magento product name} from Houston's catalogue copy (attribute c2c_cod_ean); live products first."""
    eans = sorted(set(e for e in eans if e))
    out = {}
    if not eans:
        return out
    with closing(connect()) as c:
        try:
            rows = c.execute("SELECT v.value, p.name, p.status FROM product_values v JOIN products p ON p.sku = v.sku "
                             "WHERE v.code = 'c2c_cod_ean' AND TRIM(v.value) <> ''").fetchall()
        except Exception as e:
            if not core.missing_table(e):
                raise
            return out                                     # no catalogue copy in this database
    wanted = set(eans)
    for raw, name, st in sorted(rows, key=lambda r: r[2] != "1"):
        e = ean(raw)
        if e in wanted and e not in out:
            out[e] = name or ""
    return out


def ean_links():
    with closing(connect()) as c:
        return {s: (w, f or 1.0) for s, w, f in c.execute("SELECT supplier_ean, winmentor_ean, factor FROM wm_ean_links")}


def save_ean_link(supplier_ean, winmentor_ean, factor=1.0, note=""):
    with closing(connect()) as c, c:
        if winmentor_ean:
            c.execute("INSERT OR REPLACE INTO wm_ean_links VALUES (?, ?, ?, ?, ?)",
                      (supplier_ean, winmentor_ean, float(factor or 1.0), note, core.now()))
        else:
            c.execute("DELETE FROM wm_ean_links WHERE supplier_ean = ?", (supplier_ean,))


def monthly_series(e):
    """[(month, qty)] for one EAN, one figure per month (the month's owner database), oldest first."""
    with closing(connect()) as c:
        owner = month_owner(c)
        rows = c.execute("SELECT company, month, qty FROM wm_sales_by_ean_month WHERE ean = ?", (e,)).fetchall()
    per = {}
    for company, month, qty in rows:
        if owner.get(month) == company:
            per[month] = per.get(month, 0.0) + (qty or 0.0)
    return sorted(per.items())


def ytd(e, day):
    """(units this year to `day`, units last year to the same day) for one EAN, from the daily sale lines."""
    months_now = [f"{day.year:04d}_{m:02d}" for m in range(1, day.month + 1)]
    months_prev = [f"{day.year - 1:04d}_{m:02d}" for m in range(1, day.month + 1)]
    with closing(connect()) as c:
        owner = month_owner(c)
        skus = defaultdict(list)
        for company, sku in c.execute("SELECT company, sku FROM wm_codes WHERE ean = ?", (e,)):
            skus[company].append(sku)

        def total(months):
            s = 0.0
            for m in months:
                comp = owner.get(m)
                if not comp or not skus.get(comp):
                    continue
                marks = ",".join("?" * len(skus[comp]))
                for d, qty in c.execute(f"SELECT c1, c4 FROM wm_sales_ext WHERE company = ? AND period = ? "
                                        f"AND TRIM(c3) IN ({marks})", (comp, m, *skus[comp])):
                    if m == months[-1] and int(num(d)) > day.day:
                        continue
                    s += num(qty)
            return s
        return total(months_now), total(months_prev)
