"""Data health: the joined product list (website + WinMENTOR) and the checks that run on it.

rebuild() runs after every WinMENTOR update, after every catalogue load, and from the Data health page.
- health_products: one row per product enabled on the website, matched to WinMENTOR's live database by SKU.
- health_issues: one row per problem: which check, which product, what is wrong.
- health_runs: when each rebuild ran, after what, and how many problems each check found.
Houston fixes nothing itself: a problem disappears once it is fixed at the source.

Rules (Stefan, Oct 2026)
- Active = enabled on the website. There are no B2B-only products, so any product sold in WinMENTOR without a
  website product is a problem, never an exception.
- A website pack written as SKU/N (e.g. 3028873/3) is N of WinMENTOR's single 3028873.
- Samples go out on avize at price 0: they are not sales, so "sold" counts priced lines only.
- Stock = Depozit central + VANZARI EN-GROS, from the newest stock photo (winmentor.STOCK_WAREHOUSES).
"""
import datetime
import json
import re
from collections import Counter, defaultdict
from contextlib import closing

import channel_units
import channels
import core
import operator_requests
import winmentor as wm

SALES_MONTHS = 3          # "sold recently" = sold at a price in the current month or the two before it

# One dict per check, in the order shown on the page. level: "warning" (red: costs sales or misleads customers
# today) or "caution" (amber: wrong data to fix). how: what Houston does, in plain words, for the page.
CHECKS = [
    {"id": "online_out_but_stocked", "group": "Stock", "level": "warning",
     "title": "Website says out of stock, warehouse has it",
     "how": "Houston compares the stock flag on the website with WinMENTOR's stock in Depozit central and VANZARI "
            "EN-GROS. It lights up when the website says out of stock but the warehouse has at least one.",
     "why": "Lost sales.", "fixer": "You, in Magento"},
    {"id": "online_in_but_none", "group": "Stock", "level": "warning",
     "title": "Website says in stock, warehouse has none",
     "how": "The same comparison the other way round: the website says in stock, but Depozit central and VANZARI "
            "EN-GROS have none.",
     "why": "Customers can order what you don't have.", "fixer": "You, in Magento; reorder it if it sells"},
    {"id": "unmatched_web", "group": "Matching", "level": "caution",
     "title": "Website SKU not in WinMENTOR",
     "how": "Every website product should have the same SKU in WinMENTOR. Houston looks up each website SKU in "
            "WinMENTOR's product list. A pack written as SKU/3 counts as 3 of the single.",
     "why": "Houston can't show its stock or sales.", "fixer": "You, in Magento, or the operator"},
    {"id": "unmatched_wm", "group": "Matching", "level": "caution",
     "title": "Sold in WinMENTOR, not on the website",
     "how": f"Every product WinMENTOR sold at a price in the last {SALES_MONTHS} months should be on the website. "
            "Houston looks up each one by SKU. Services, like the delivery fee, are skipped.",
     "why": "Its sales don't reach any website product.", "fixer": "You or the operator"},
    {"id": "sku_space", "group": "Matching", "level": "caution",
     "title": "SKU with a stray space",
     "how": "Houston looks for a space at the start or end of a website SKU, like '0009336 '. It still matches the "
            "product, but the SKU should be cleaned.",
     "why": "Other tools match SKUs exactly and miss these.", "fixer": "You, in Magento"},
    {"id": "ean_differs", "group": "EAN", "level": "caution",
     "title": "EAN differs between the website and WinMENTOR",
     "how": "For products with the same SKU on both sides, Houston compares the two EANs. It lights up when both "
            "are real EANs but different.",
     "why": "Supplier forms and marketplaces match on EAN, so one side is wrong.",
     "fixer": "Check the pack, fix the wrong side"},
    {"id": "ean_missing_web", "group": "EAN", "level": "caution",
     "title": "EAN missing on the website",
     "how": "The website's EAN is empty or not a real EAN, while WinMENTOR has one. A real EAN has 8, 12 or 13 "
            "digits and a correct last digit.",
     "why": "Supplier forms and marketplaces match on EAN.", "fixer": "You, in Magento"},
    {"id": "ean_missing_wm", "group": "EAN", "level": "caution",
     "title": "EAN missing in WinMENTOR",
     "how": "WinMENTOR's EAN is empty or not a real EAN, while the website has one.",
     "why": "Supplier forms and marketplaces match on EAN.", "fixer": "The operator"},
    {"id": "ean_none", "group": "EAN", "level": "caution",
     "title": "No EAN on either side",
     "how": "Neither the website nor WinMENTOR has a real EAN for the product.",
     "why": "Supplier forms and marketplaces match on EAN.", "fixer": "Both sides, from the pack"},
    {"id": "ean_duplicate", "group": "EAN", "level": "caution",
     "title": "Same EAN on two website products",
     "how": "Houston looks for one EAN used by two different website products.",
     "why": "At least one of them is wrong.", "fixer": "Check the packs, fix in Magento"},
    {"id": "dormant", "group": "Suppliers and activity", "level": "caution",
     "title": f"No stock and no sales for {SALES_MONTHS} months",
     "how": "The product is active on the website, the warehouse has none, and nothing was sold at a price in this "
            "month or the two before.",
     "why": "Either discontinued (take it offline) or forgotten (reorder).", "fixer": "You decide"},
    {"id": "no_supplier", "group": "Suppliers and activity", "level": "caution",
     "title": "No delivery found",
     "how": "Houston looks for the product's last delivery (reception) in WinMENTOR's live database. It lights up "
            "when there is none.",
     "why": "Ordering by supplier can't place it.", "fixer": "You or your mother name the supplier"},
    {"id": "producer_vs_supplier", "group": "Suppliers and activity", "level": "caution",
     "title": "Website producer isn't who delivers",
     "how": "Houston compares the producer on the website with the company that delivered the product last in "
            "WinMENTOR, by the first word of each name. It lights up when they differ.",
     "why": "Orders go to whoever delivers.", "fixer": "Your mother confirms the supplier"},
]
CHECKS.append(channels.CHECK)
GROUPS = ["Stock", "Matching", "EAN", "Suppliers and activity", channels.CHECK["group"]]

SCHEMA = """
CREATE TABLE IF NOT EXISTS health_products (sku TEXT, raw_sku TEXT PRIMARY KEY, name TEXT, online_in_stock INTEGER,
    online_price REAL, magento_ean TEXT, producer TEXT, match TEXT, pack_of INTEGER, wm_code TEXT, wm_name TEXT,
    wm_ean TEXT, ean TEXT, stock REAL, supplier TEXT, last_delivery TEXT, sold_recent REAL, built_at TEXT);
CREATE TABLE IF NOT EXISTS health_issues (check_id TEXT, sku TEXT, product TEXT, detail TEXT, found_at TEXT);
CREATE INDEX IF NOT EXISTS ix_health_issues ON health_issues (check_id);
CREATE INDEX IF NOT EXISTS ix_health_issues_sku ON health_issues (sku);
CREATE TABLE IF NOT EXISTS health_runs (run_at TEXT PRIMARY KEY, source TEXT, company TEXT, products INTEGER,
    issues INTEGER, counts TEXT);
"""
PRODUCT_COLS = ["sku", "raw_sku", "name", "online_in_stock", "online_price", "magento_ean", "producer", "match",
                "pack_of", "wm_code", "wm_name", "wm_ean", "ean", "stock", "supplier", "last_delivery",
                "sold_recent", "built_at"]
PACK = re.compile(r"^(.+)/(\d+)$")
EXACT = ("SKU", "SKU, space removed")         # matches where the website EAN must equal WinMENTOR's


def _connect():
    c = wm.connect()
    if not core.READ_ONLY:
        c.executescript(SCHEMA)
    return c


def _float(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def _date(s):
    """WinMENTOR's '5.10.2026' or '05.10.2026' -> '2026-10-05'; '' when it isn't a date."""
    parts = (s or "").strip().split(".")
    if len(parts) != 3:
        return ""
    try:
        return datetime.date(int(parts[2]), int(parts[1]), int(parts[0])).isoformat()
    except ValueError:
        return ""


def _company_key(name):
    """First word of a company name, letters and digits only: 'A.S.T.R.A Bio, Italia' -> 'ASTRA'."""
    words = re.sub(r"[^0-9A-Z ]", "", (name or "").upper()).split()
    return words[0] if words else ""


def _months_label(months):
    lab = wm.month_label
    return lab(months[0]) if len(months) == 1 else f"{lab(months[0])} to {lab(months[-1])}"


# --- reading the two sides ------------------------------------------------------------------
def _website(c):
    labels = dict(c.execute("SELECT value, label FROM attribute_options WHERE code = 'manufacturer'"))
    vals = defaultdict(dict)
    for sku, code, value in c.execute(
            "SELECT sku, code, value FROM product_values WHERE code IN ('c2c_cod_ean', 'manufacturer')"):
        vals[sku][code] = value
    out = []
    for raw, name, price, in_stock in c.execute(
            "SELECT sku, name, price, is_in_stock FROM products WHERE status = '1' ORDER BY name"):
        v = vals.get(raw, {})
        out.append({"raw_sku": raw or "", "sku": (raw or "").strip(), "name": (name or "").strip(),
                    "online_price": _float(price), "online_in_stock": 1 if str(in_stock).strip() == "1" else 0,
                    "magento_ean_raw": (v.get("c2c_cod_ean") or "").strip(),
                    "magento_ean": wm.ean(v.get("c2c_cod_ean")),
                    "producer": labels.get(str(v.get("manufacturer") or ""), "")})
    return out


def _winmentor(c, company):
    cat = {}
    for code, name, other, service in c.execute(
            "SELECT TRIM(c0), c1, c9, TRIM(c14) FROM wm_catalogue WHERE company = ? AND period = ''", (company,)):
        if code:
            cat[code] = {"name": (name or "").strip(), "ean_raw": (other or "").strip(), "ean": wm.ean(other),
                         "service": service == "Da"}
    stock = defaultdict(float)
    day = c.execute("SELECT MAX(period) FROM wm_stock WHERE company = ?", (company,)).fetchone()[0]
    if day:
        for code, qty, wh in c.execute("SELECT TRIM(c0), c4, c12 FROM wm_stock WHERE company = ? AND period = ?",
                                       (company, day)):
            if code and (wh or "").strip().upper() in wm.STOCK_WAREHOUSES:
                stock[code] += wm.num(qty)
    last = {}
    for code, supplier, d in c.execute("SELECT TRIM(c8), TRIM(c4), c3 FROM wm_receptions WHERE company = ?",
                                       (company,)):
        iso = _date(d)
        if code and iso and (code not in last or iso >= last[code][1]):
            last[code] = (supplier or "", iso)
    today = wm.today()
    cur = f"{today.year:04d}_{today.month:02d}"
    months = [wm.month_add(cur, -i) for i in range(SALES_MONTHS - 1, -1, -1)]
    sold, sold_names = defaultdict(float), {}
    marks = ",".join("?" * len(months))
    for code, qty, price, name in c.execute(
            f"SELECT TRIM(c3), c5, c7, c16 FROM wm_sales_month WHERE company = ? AND period IN ({marks})",
            (company, *months)):
        if code and wm.num(price) > 0:          # price 0 = samples on avize: not sales
            sold[code] += wm.num(qty)
            sold_names.setdefault(code, (name or "").strip())
    return {"company": company, "catalogue": cat, "stock": stock, "stock_day": day, "last": last,
            "sold": sold, "sold_names": sold_names, "months": months}


def _join(web, wmc):
    cat = wmc["catalogue"] if wmc else {}
    rows = []
    for w in web:
        sku, code, match, pack = w["sku"], "", "not found", 1
        if sku in cat:
            code, match = sku, ("SKU" if w["raw_sku"] == sku else "SKU, space removed")
        else:
            m = PACK.match(sku)
            if m and m.group(1) in cat:
                code, match, pack = m.group(1), f"pack of {m.group(2)}", int(m.group(2))
        k = cat.get(code, {})
        supplier, last = wmc["last"].get(code, ("", "")) if (wmc and code) else ("", "")
        rows.append({**w, "match": match, "pack_of": pack, "wm_code": code, "wm_name": k.get("name", ""),
                     "wm_ean_raw": k.get("ean_raw", ""), "wm_ean": k.get("ean", ""),
                     "ean": w["magento_ean"] or (k.get("ean", "") if match in EXACT else ""),
                     "stock": wmc["stock"].get(code, 0.0) if (wmc and code) else None,
                     "supplier": supplier, "last_delivery": last,
                     "sold_recent": wmc["sold"].get(code, 0.0) if (wmc and code) else None})
    return rows


# --- the checks ---------------------------------------------------------------------------------
def _checks(rows, wmc):
    out = []                                    # (check id, code, product, what's wrong)

    def add(cid, r, detail):
        out.append((cid, r["sku"], r["name"], detail))

    for r in rows:
        if r["raw_sku"] != r["sku"]:
            add("sku_space", r, f"The SKU on the website is '{r['raw_sku']}'")
        if wmc and not r["wm_code"]:
            add("unmatched_web", r, f"SKU {r['sku']} isn't in WinMENTOR ({wmc['company']})")
    exact = [r for r in rows if r["match"] in EXACT]
    for r in exact:
        m, w = r["magento_ean"], r["wm_ean"]
        if m and w and m != w:
            add("ean_differs", r, f"Website {m}, WinMENTOR {w}")
        elif w and not m:
            add("ean_missing_web", r, f"Website has '{r['magento_ean_raw'] or 'nothing'}', WinMENTOR has {w}")
        elif m and not w:
            add("ean_missing_wm", r, f"WinMENTOR has '{r['wm_ean_raw'] or 'nothing'}', the website has {m}")
        elif not m and not w:
            add("ean_none", r, f"Website '{r['magento_ean_raw'] or 'nothing'}', "
                               f"WinMENTOR '{r['wm_ean_raw'] or 'nothing'}'")
    by_ean = defaultdict(list)
    for r in rows:
        if r["magento_ean"]:
            by_ean[r["magento_ean"]].append(r)
    for e, group in by_ean.items():
        if len(group) > 1:
            for r in group:
                add("ean_duplicate", r, f"EAN {e} is also on: " + "; ".join(g["name"] for g in group if g is not r))
    if not wmc:
        return out

    window = _months_label(wmc["months"])
    for r in (x for x in rows if x["wm_code"]):
        pack = r["pack_of"] or 1
        stock = (r["stock"] or 0.0) / pack                       # a pack of 3 needs 3 singles
        sold = r["sold_recent"] or 0.0
        unit = "" if pack == 1 else f" (counted in packs of {pack})"
        if not r["online_in_stock"] and stock >= 1:
            add("online_out_but_stocked", r, f"{stock:,.0f} in the warehouse{unit}")
        if r["online_in_stock"] and stock < 1:
            add("online_in_but_none", r, f"None in the warehouse; {sold:,.0f} sold in {window}")
        if stock < 1 and sold <= 0:
            add("dormant", r, f"No stock, nothing sold in {window}")
        if not r["supplier"]:
            add("no_supplier", r, f"No delivery found in {wmc['company']}")
        elif r["producer"] and _company_key(r["producer"]) != _company_key(r["supplier"]):
            add("producer_vs_supplier", r, f"Producer on the website: {r['producer']}; last delivered by "
                                           f"{r['supplier']} on {r['last_delivery']}")

    on_site = {r["wm_code"] for r in rows if r["wm_code"]}
    for code, qty in sorted(wmc["sold"].items(), key=lambda x: -x[1]):
        k = wmc["catalogue"].get(code, {})
        if code in on_site or k.get("service") or qty <= 0:
            continue
        out.append(("unmatched_wm", code, wmc["sold_names"].get(code) or k.get("name", ""),
                    f"SKU {code}; {qty:,.0f} sold in {window}"))
    return out


# --- running and reading ------------------------------------------------------------------------
def rebuild(source="manual"):
    """Rebuild the product list and run every check. Returns {'run_at', 'company', 'products', 'issues', 'counts'}."""
    run_at = core.now()
    with closing(_connect()) as c, c:
        company = wm.live_company(c)
        web = _website(c)
        wmc = _winmentor(c, company) if company else None
        if wmc:
            operator_requests.verify(c, wmc["catalogue"])     # close the operator's requests WinMENTOR now shows
        rows = _join(web, wmc)
        found = _checks(rows, wmc)
        found += channels.rebuild_quietly(c, run_at)
        found += channel_units.rebuild_quietly(c)          # units per product, month and channel
        pk = [r[1] for r in c.execute("PRAGMA table_info(health_products)") if r[5]]
        if pk != ["raw_sku"]:       # keyed by the trimmed SKU before: two SKUs differing by a space were merged
            c.execute("DROP TABLE health_products")
            c.executescript(SCHEMA)
        c.execute("DELETE FROM health_products")
        c.executemany(f"INSERT OR REPLACE INTO health_products VALUES ({','.join('?' * len(PRODUCT_COLS))})",
                      [tuple(run_at if k == "built_at" else r.get(k) for k in PRODUCT_COLS) for r in rows])
        c.execute("DELETE FROM health_issues")
        c.executemany("INSERT INTO health_issues VALUES (?, ?, ?, ?, ?)", [(*f, run_at) for f in found])
        counts = Counter(f[0] for f in found)
        c.execute("INSERT OR REPLACE INTO health_runs VALUES (?, ?, ?, ?, ?, ?)",
                  (run_at, source, company or "", len(rows), len(found), json.dumps(counts)))
    core.set_setting("health_last_error", "")
    return {"run_at": run_at, "company": company, "products": len(rows), "issues": len(found), "counts": counts}


def rebuild_quietly(source):
    """rebuild(), but a failure is kept for the Data health page instead of stopping the page that called it."""
    try:
        return rebuild(source)
    except Exception as e:                       # noqa: BLE001 (shown on the Data health page)
        core.set_setting("health_last_error", f"{core.now()}, after {source}: {e}")
        return None


def newest_data():
    """When the newest data arrived: the later of the last catalogue load and the last WinMENTOR file loaded."""
    with closing(_connect()) as c:
        cat = c.execute("SELECT value FROM meta WHERE key = 'loaded_at'").fetchone()
        wmf = c.execute("SELECT MAX(loaded_at) FROM wm_files WHERE status = 'loaded'").fetchone()
    return max([x for x in ((cat or [None])[0], (wmf or [None])[0]) if x] or [""])


def last_error():
    return core.setting("health_last_error") or ""


def last_run():
    with closing(_connect()) as c:
        row = c.execute("SELECT run_at, source, company, products, issues, counts FROM health_runs "
                        "ORDER BY run_at DESC LIMIT 1").fetchone()
    if not row:
        return None
    return {"run_at": row[0], "source": row[1], "company": row[2], "products": row[3], "issues": row[4],
            "counts": json.loads(row[5] or "{}")}


def issues():
    with closing(_connect()) as c:
        return c.execute("SELECT check_id, sku, product, detail FROM health_issues ORDER BY check_id, product").fetchall()


def product(sku):
    """Return one health product by SKU without loading the full product list."""
    with closing(_connect()) as c:
        cur = c.execute("SELECT * FROM health_products WHERE sku = ? LIMIT 1", (sku,))
        row = cur.fetchone()
        if not row and sku.strip() != sku:
            cur = c.execute("SELECT * FROM health_products WHERE TRIM(sku) = ? LIMIT 1", (sku.strip(),))
            row = cur.fetchone()
        if not row:
            return None
        return dict(zip((d[0] for d in cur.description), row))


def product_issues(sku):
    """Return issues for one SKU without loading the full issue list."""
    with closing(_connect()) as c:
        rows = c.execute("SELECT check_id, detail FROM health_issues "
                         "WHERE sku = ? ORDER BY check_id", (sku,)).fetchall()
        if not rows and sku.strip() != sku:
            rows = c.execute("SELECT check_id, detail FROM health_issues "
                             "WHERE TRIM(sku) = ? ORDER BY check_id", (sku.strip(),)).fetchall()
        return rows


def products():
    with closing(_connect()) as c:
        cur = c.execute("SELECT * FROM health_products ORDER BY name")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
