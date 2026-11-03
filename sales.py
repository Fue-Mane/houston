"""What the Sales pages show. Read-only: from sales_docs (channels.py) and WinMENTOR's sale lines.

Rules (Stefan, 23 Oct 2026)
- A sale is a GetVanzariLuna line (wm_sales_month) whose document has a channel. Samples and donations have
  none (channels rule 1), so they are left out.
- Lei are without VAT: quantity x price.
- Delivery fees, discounts, rebates and other services count in the lei totals. Product, brand and supplier
  figures count products only: lines with a code that WinMENTOR's catalogue doesn't mark as a service.
- Year against year compares the same months a year earlier. When the period ends in the month of the newest
  sale, the month a year earlier is counted up to the same day.
- Brand comes from the website (attribute c2c_length); supplier is whoever delivered the product last.
- Clients: each shop or branch of a company counts once, and each delivery address of a person counts once.
  Returns and rebates (negative documents) don't count. A person's address is turned into a one-way code in
  memory to count them; it is never stored.
"""
import datetime
import hashlib
from collections import defaultdict
from contextlib import closing

import pandas as pd

import channels
import winmentor as wm

NOT_ON_WEBSITE = "(not on the website)"
NO_SUPPLIER = "(no delivery found)"


def month_range(first, last):
    out, m = [], first
    while m <= last:
        out.append(m)
        m = wm.month_add(m, 1)
    return out


def label(month):
    return datetime.date(int(month[:4]), int(month[5:]), 1).strftime("%b %Y")


def status():
    """None before the first channel run, else {'first', 'last' (months), 'last_day', 'version'}."""
    with closing(wm.connect()) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'sales_docs'").fetchone():
            return None
        first, last, version = c.execute("SELECT MIN(month), MAX(month), MAX(built_at) FROM sales_docs "
                                         "WHERE channel <> ?", (channels.NOT_SALE,)).fetchone()
        if not last:
            return None
        day = c.execute("SELECT MAX(doc_date) FROM sales_docs WHERE month = ?", (last,)).fetchone()[0]
    return {"first": first, "last": last, "last_day": datetime.date.fromisoformat(day), "version": version}


def needs_rebuild():
    """True when sales_docs was built before it had the shop column, so clients would be counted wrongly."""
    with closing(wm.connect()) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(sales_docs)")]
    return bool(cols) and "place" not in cols


def _lists(c):
    """Services, website facts per WinMENTOR code, and the last supplier per code."""
    services = {code for (code,) in c.execute("SELECT DISTINCT TRIM(c0) FROM wm_catalogue WHERE TRIM(c14) = 'Da'")}
    web = {}
    try:
        for code, sku, name, brand in c.execute(
                "SELECT h.wm_code, h.sku, h.name, v.value FROM health_products h LEFT JOIN product_values v "
                "ON v.sku = h.raw_sku AND v.code = 'c2c_length' WHERE h.wm_code <> ''"):
            web.setdefault(code, (sku, name or "", (brand or "").strip() or NOT_ON_WEBSITE))
    except Exception:                               # noqa: BLE001  (no product list yet)
        pass
    last = {}
    for code, supplier, d in c.execute("SELECT TRIM(c8), TRIM(c4), c3 FROM wm_receptions"):
        parts = (d or "").strip().split(".")
        if not code or len(parts) != 3:
            continue
        try:
            iso = datetime.date(int(parts[2]), int(parts[1]), int(parts[0])).isoformat()
        except ValueError:
            continue
        if code not in last or iso >= last[code][1]:
            last[code] = (supplier or "", iso)
    return services, web, {k: v[0] for k, v in last.items()}


def load(first, last, chans=None):
    """All a Sales page needs for months first..last ('yyyy_MM') and the same months a year earlier.
    chans: list of channel names, or None for every channel (the Total page)."""
    st = status()
    months = month_range(first, last)
    before = [wm.month_add(m, -12) for m in months]
    cut = (before[-1], st["last_day"].day) if st and last == st["last"] else None
    wanted = [ch for ch in channels.ORDER] if chans is None else list(chans)
    marks = ",".join("?" * len(wanted))
    with closing(wm.connect()) as c:
        owner = wm.month_owner(c)
        place = "place" if "place" in [r[1] for r in c.execute("PRAGMA table_info(sales_docs)")] else "''"
        docs = pd.read_sql_query(
            f"SELECT company, month, doc, doc_date, channel, value, customer_code, {place} AS place FROM sales_docs "
            f"WHERE month IN ({','.join('?' * len(months + before))}) AND channel IN ({marks})",
            c, params=[*months, *before, *wanted])
        frames, people = [], {}
        for m in months + before:
            if m not in owner:
                continue
            for doc, addr, town in c.execute(
                    "SELECT c15, LOWER(TRIM(COALESCE(MAX(c21), ''))), LOWER(TRIM(COALESCE(MAX(c22), ''))) "
                    "FROM wm_sales_month WHERE company = ? AND period = ? GROUP BY c15", (owner[m], m)):
                if addr:
                    people[(owner[m], m, doc)] = hashlib.sha1(f"{addr}|{town}".encode("utf-8")).hexdigest()[:16]
            frames.append(pd.read_sql_query(
                "SELECT l.period AS month, CAST(l.c1 AS INTEGER) AS day, TRIM(COALESCE(l.c3, '')) AS code, "
                "MAX(l.c16) AS wm_name, d.channel, SUM(CAST(REPLACE(l.c5, ',', '.') AS REAL)) AS qty, "
                "SUM(CAST(REPLACE(l.c5, ',', '.') AS REAL) * CAST(REPLACE(l.c7, ',', '.') AS REAL)) AS value "
                "FROM wm_sales_month l JOIN sales_docs d ON d.company = l.company AND d.month = l.period "
                f"AND d.doc = l.c15 WHERE l.company = ? AND l.period = ? AND d.channel IN ({marks}) "
                "GROUP BY l.period, CAST(l.c1 AS INTEGER), TRIM(COALESCE(l.c3, '')), d.channel",
                c, params=[owner[m], m, *wanted]))
        services, web, supplier = _lists(c)
    lines = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["month", "day", "code", "wm_name", "channel", "qty", "value"])
    docs["day"] = pd.to_datetime(docs["doc_date"]).dt.day if not docs.empty else pd.Series(dtype=int)
    docs["client"] = [f"c:{code}|{pl}" if code else (f"p:{people[(co, m, d)]}" if (co, m, d) in people else "")
                      for co, m, d, code, pl in zip(docs["company"], docs["month"], docs["doc"], docs["customer_code"],
                                                    docs["place"])]
    docs = docs.drop(columns=["company", "doc", "customer_code", "place"])
    if cut:
        docs = docs[~((docs["month"] == cut[0]) & (docs["day"] > cut[1]))]
        lines = lines[~((lines["month"] == cut[0]) & (lines["day"] > cut[1]))]
    for df in (docs, lines):
        df["period"] = df["month"].map(lambda m: "now" if m in months else "before")
    lines["product"] = (lines["code"] != "") & ~lines["code"].isin(services)
    facts = lines["code"].map(lambda k: web.get(k))
    lines["sku"] = [f[0] if f else k for f, k in zip(facts, lines["code"])]
    lines["name"] = [f[1] if f and f[1] else n for f, n in zip(facts, lines["wm_name"])]
    lines["brand"] = [f[2] if f else NOT_ON_WEBSITE for f in facts]
    lines["supplier"] = lines["code"].map(lambda k: supplier.get(k) or NO_SUPPLIER)
    return {"months": months, "before": before, "cut": cut, "docs": docs, "lines": lines}


# --- summaries for the page -----------------------------------------------------------------------
def change(now, before):
    """Percent change, or None when there is nothing a year earlier to compare with."""
    return None if not before else (now - before) / abs(before) * 100


def totals(data):
    d, p = data["docs"], data["lines"][data["lines"]["product"]]
    out = {}
    for period in ("now", "before"):
        part = d[d["period"] == period]
        out[period] = {"value": part["value"].sum(), "documents": len(part), "clients": clients(part),
                       "units": p.loc[p["period"] == period, "qty"].sum()}
    return out


def clients(docs):
    """Distinct clients among documents with a positive total."""
    keys = docs.loc[docs["value"] > 0, "client"]
    return int(keys[keys != ""].nunique())


def by_channel(data, names):
    d = data["docs"]
    rows = []
    for ch in names:
        part = d[d["channel"] == ch]
        rows.append({"Channel": ch, "Clients": clients(part[part["period"] == "now"]),
                     "Lei": part.loc[part["period"] == "now", "value"].sum(),
                     "Lei a year earlier": part.loc[part["period"] == "before", "value"].sum()})
    rows.append({"Channel": channels.TOTAL, "Clients": clients(d[(d["period"] == "now") & d["channel"].isin(names)]),
                 "Lei": sum(r["Lei"] for r in rows), "Lei a year earlier": sum(r["Lei a year earlier"] for r in rows)})
    df = pd.DataFrame(rows)
    df["Change %"] = [change(a, b) for a, b in zip(df["Lei"], df["Lei a year earlier"])]
    return df


def monthly(data):
    d = data["docs"]
    now = d[d["period"] == "now"].groupby("month")["value"].sum()
    before = d[d["period"] == "before"].groupby("month")["value"].sum()
    rows = []
    for i, (m, b) in enumerate(zip(data["months"], data["before"])):
        rows.append({"i": i, "month": label(m), "series": "Selected months", "lei": float(now.get(m, 0.0)), "of": label(m)})
        rows.append({"i": i, "month": label(m), "series": "A year earlier", "lei": float(before.get(b, 0.0)), "of": label(b)})
    return pd.DataFrame(rows)


def grouped(data, key):
    """Products only, grouped by 'brand', 'supplier' or 'code'."""
    p = data["lines"][data["lines"]["product"]]
    cols = [key] if key != "code" else ["code", "sku", "name", "brand", "supplier"]
    if p.empty:
        return pd.DataFrame(columns=cols + ["Lei", "Lei a year earlier", "Change %", "Units", "Units a year earlier"])
    now = p[p["period"] == "now"].groupby(key).agg(lei=("value", "sum"), units=("qty", "sum"))
    before = p[p["period"] == "before"].groupby(key).agg(lei_b=("value", "sum"), units_b=("qty", "sum"))
    out = now.join(before, how="outer").fillna(0.0).reset_index()
    if key == "code":
        first = p.sort_values("period", ascending=False).drop_duplicates("code").set_index("code")
        for k in ("sku", "name", "brand", "supplier"):
            out[k] = out["code"].map(first[k])
    out["Change %"] = [change(a, b) for a, b in zip(out["lei"], out["lei_b"])]
    out = out.rename(columns={"lei": "Lei", "lei_b": "Lei a year earlier", "units": "Units", "units_b": "Units a year earlier"})
    return out.sort_values(["Lei", "Lei a year earlier"], ascending=False)[
        cols + ["Lei", "Lei a year earlier", "Change %", "Units", "Units a year earlier"]].reset_index(drop=True)


# --- why sales changed: a price-volume-mix bridge ------------------------------------------------------
BRIDGE = [("before", "A year earlier"), ("volume", "More or fewer units"), ("mix", "Product mix"),
          ("price", "Prices"), ("new", "New products"), ("gone", "Products not sold now"),
          ("services", "Fees and discounts"), ("now", "Selected months")]


def bridge(data):
    """From a year earlier's lei to the selected months', step by step. Every product falls in one group:
    kept (sold in both periods), new (sold only now) or gone (sold only a year earlier). A product is the same
    WinMENTOR code across all the page's channels.
    - More or fewer units: the change in units of kept products, at last year's average price per unit.
    - Product mix: among kept products, selling more of the dearer or of the cheaper ones.
    - Prices: kept products, the change in what we got per unit of the same product (price changes, discounts,
      and more of a product going to customers who pay less, such as B2B rather than the website).
    - New products / Products not sold now: the new and the gone groups.
    - Fees and discounts: delivery fees, discounts, rebates and other service lines.
    Returns [{'key', 'step', 'lei'}]; the steps add up exactly to the change."""
    lines = data["lines"]
    total = {p: float(lines.loc[lines["period"] == p, "value"].sum()) for p in ("now", "before")}
    serv = lines[~lines["product"]]
    services = float(serv.loc[serv["period"] == "now", "value"].sum() - serv.loc[serv["period"] == "before", "value"].sum())
    prod = lines[lines["product"]]
    g = prod.groupby(["code", "period"])[["qty", "value"]].sum().unstack("period", fill_value=0.0)
    col = lambda what, p: g[(what, p)] if (what, p) in g.columns else pd.Series(0.0, index=g.index)  # noqa: E731
    q0, q1, r0, r1 = col("qty", "before"), col("qty", "now"), col("value", "before"), col("value", "now")
    both = (q0 > 0) & (q1 > 0)
    big_q0, big_q1, big_r0 = q0[both].sum(), q1[both].sum(), r0[both].sum()
    avg0 = big_r0 / big_q0 if big_q0 else 0.0
    at_old_prices = float((q1[both] * (r0[both] / q0[both])).sum()) if both.any() else 0.0
    new_ = (q0 <= 0) & (q1 > 0)
    steps = {"before": total["before"], "volume": (big_q1 - big_q0) * avg0, "mix": at_old_prices - big_q1 * avg0,
             "price": float(r1[both].sum()) - at_old_prices,
             "new": float((r1 - r0)[new_].sum()), "gone": float((r1 - r0)[q1 <= 0].sum()),
             "services": services, "now": total["now"]}
    counts = {"volume": int(both.sum()), "mix": int(both.sum()), "price": int(both.sum()),
              "new": int(new_.sum()), "gone": int((q1 <= 0).sum())}
    return [{"key": k, "step": name, "lei": float(steps[k]), "n": counts.get(k)} for k, name in BRIDGE]


def cumulative(data, last_day):
    """Lei added up day by day through the selected months, and the same for a year earlier on the same dates."""
    d = data["docs"]
    first = datetime.date(int(data["months"][0][:4]), int(data["months"][0][5:]), 1)
    end_month = data["months"][-1]
    nxt = datetime.date(int(end_month[:4]), int(end_month[5:]), 1) + datetime.timedelta(days=32)
    end = min(nxt.replace(day=1) - datetime.timedelta(days=1), last_day)
    days = pd.date_range(first, end, freq="D")
    now = d[d["period"] == "now"].groupby(pd.to_datetime(d.loc[d["period"] == "now", "doc_date"]))["value"].sum()
    b = d[d["period"] == "before"]
    shifted = pd.to_datetime(b["doc_date"]) + pd.DateOffset(years=1)
    before = b.groupby(shifted)["value"].sum()
    out = pd.DataFrame({"date": days,
                        "Selected months": now.reindex(days, fill_value=0.0).cumsum().values,
                        "A year earlier": before.groupby(level=0).sum().reindex(days, fill_value=0.0).cumsum().values})
    return out.melt("date", var_name="series", value_name="lei")


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def order_times(first, last):
    """Website orders (Magento, complete or closed) placed in months first..last, by weekday and hour in Romanian
    time. Only website orders carry a time; invoices in WinMENTOR have a date only."""
    import store_orders
    a, b = f"{first[:4]}-{first[5:]}-01", f"{last[:4]}-{last[5:]}-31"
    counts = defaultdict(int)
    with closing(wm.connect()) as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'mg_orders'").fetchone():
            for (created,) in c.execute("SELECT created_at FROM mg_orders WHERE status IN ('complete', 'closed') "
                                        "AND placed_on BETWEEN ? AND ?", (a, b)):
                try:
                    t = store_orders.to_local(datetime.datetime.strptime((created or "")[:19], "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
                counts[(t.weekday(), t.hour)] += 1
    return pd.DataFrame([{"day": WEEKDAYS[d], "hour": h, "hours": f"{h:02d}:00-{(h + 1) % 24:02d}:00",
                          "orders": counts.get((d, h), 0)} for d in range(7) for h in range(24)])


# --- biggest gains and losses, and products bought together ------------------------------------------------
def movers(data, by="code", n=10):
    """Products (by='code') or brands (by='brand') with the biggest lei gains and losses against a year earlier.
    Returns (gains, losses) DataFrames; products carry their SKU and whether they are new or no longer sold."""
    g = grouped(data, by)
    if g.empty:
        return g, g
    g = g.assign(Change=g["Lei"] - g["Lei a year earlier"])
    if by == "code":
        g["Note"] = ["new" if b <= 0 < a else "no longer sold" if a <= 0 < b else ""
                     for a, b in zip(g["Units"], g["Units a year earlier"])]
    gains = g[g["Change"] > 0].nlargest(n, "Change").reset_index(drop=True)
    losses = g[g["Change"] < 0].nsmallest(n, "Change").reset_index(drop=True)
    return gains, losses


def bundles(first, last, chans, min_orders=3, max_items=30, top=20):
    """Pairs of products on the same order, for orders with 2 to max_items products in months first..last.
    The first product of a pair is the one bought less often, so the share reads: of the orders with this
    product, how many also had the other one. Returns (DataFrame, orders looked at)."""
    from itertools import combinations
    months = month_range(first, last)
    marks = ",".join("?" * len(chans))
    baskets, names = defaultdict(set), {}
    with closing(wm.connect()) as c:
        owner = wm.month_owner(c)
        services, web, _ = _lists(c)
        for m in months:
            if m not in owner:
                continue
            for doc, code, name in c.execute(
                    "SELECT c15, TRIM(c3), MAX(c16) FROM wm_sales_month WHERE company = ? AND period = ? "
                    f"AND c15 IN (SELECT doc FROM sales_docs WHERE month = ? AND value > 0 AND channel IN ({marks})) "
                    "AND TRIM(COALESCE(c3, '')) <> '' AND CAST(REPLACE(c5, ',', '.') AS REAL) > 0 "
                    "GROUP BY c15, TRIM(c3)", (owner[m], m, m, *chans)):
                if code in services:
                    continue
                baskets[(m, doc)].add(code)
                names.setdefault(code, (web.get(code) or ("", "", ""))[1] or name or code)
    orders = [b for b in baskets.values() if len(b) <= max_items]
    count, pairs = defaultdict(int), defaultdict(int)
    for b in orders:
        for code in b:
            count[code] += 1
        if len(b) >= 2:
            for a, b2 in combinations(sorted(b), 2):
                pairs[(a, b2)] += 1
    rows = []
    for (a, b2), n in pairs.items():
        if n < min_orders:
            continue
        first_, second = (a, b2) if (count[a], a) <= (count[b2], b2) else (b2, a)
        rows.append({"Product": names[first_], "Also bought": names[second], "Orders with both": n,
                     "% of the product's orders": n / count[first_] * 100})
    df = pd.DataFrame(rows, columns=["Product", "Also bought", "Orders with both", "% of the product's orders"])
    df = df.sort_values(["Orders with both", "% of the product's orders"], ascending=False).head(top)
    return df.reset_index(drop=True), len(orders)
