"""What the Marketing pages show. Read-only, from Google Analytics (ga4_landing) and the store's orders (mg_orders).

- Visits are Google Analytics' engaged sessions. Bots rarely count as engaged, so this leaves out most of the bot
  traffic that has inflated plain sessions since January 2026. Direct still carries some in the worst months.
- Orders and revenue are as Google Analytics credits them (revenue with VAT). It doesn't see every order, so
  coverage() compares its count with the store's own.
- A year earlier is only compared when Houston has Google Analytics data for every one of those months.
"""
import datetime
from contextlib import closing

import pandas as pd

import core
import sales
import winmentor as wm

TOP_SOURCES = 6
OTHER = "Other sources"


def status():
    """None before the first Google Analytics pull, else {'first', 'last' (months), 'last_day'}."""
    with closing(core.connect()) as c:
        try:
            first, last = c.execute("SELECT MIN(date), MAX(date) FROM ga4_landing").fetchone()
        except Exception as e:                          # noqa: BLE001  (no Google Analytics tables yet)
            if not core.missing_table(e):
                raise
            return None
    if not last:
        return None
    return {"first": first[:7].replace("-", "_"), "last": last[:7].replace("-", "_"),
            "last_day": datetime.date.fromisoformat(last)}


def _pages(c):
    """Landing path -> (SKU, product name) for product pages."""
    try:
        return {f"/{(k or '').strip()}.html": ((sku or "").strip(), name or "")
                for sku, name, k in c.execute("SELECT sku, name, url_key FROM products WHERE url_key <> ''")}
    except Exception as e:                              # noqa: BLE001  (catalogue not loaded yet)
        if not core.missing_table(e):
            raise
        return {}


def _year_earlier(d):
    try:
        return d.replace(year=d.year - 1)
    except ValueError:                                  # 29 February
        return d.replace(year=d.year - 1, day=28)


def load(first, last):
    """Google Analytics rows for months first..last ('yyyy_MM') and a year earlier, plus the store's order counts.
    When the period ends in the newest month Houston holds, both years stop at the same day (cut), as on the
    Sales pages, so a month still in progress isn't compared with a whole month."""
    st = status()
    months = sales.month_range(first, last)
    before = [wm.month_add(m, -12) for m in months]
    cut = st["last_day"] if st and last == st["last"] else None

    def days(ms, end=None):
        return f"{ms[0][:4]}-{ms[0][5:]}-01", (end.isoformat() if end else f"{ms[-1][:4]}-{ms[-1][5:]}-31")
    spans = {"now": days(months, cut), "before": days(before, _year_earlier(cut) if cut else None)}
    with closing(core.connect()) as c:
        frames = []
        for period in ("now", "before"):
            a, b = spans[period]
            frames.append(pd.read_sql_query(
                "SELECT date, channel, landing_page AS page, SUM(engaged_sessions) AS visits, "
                "SUM(sessions) AS sessions, SUM(purchases) AS orders, SUM(revenue) AS revenue FROM ga4_landing "
                "WHERE date BETWEEN ? AND ? GROUP BY 1, 2, 3", c, params=[a, b]).assign(period=period))
            frames[-1]["store_orders"] = 0
        store = {}
        try:
            for period in ("now", "before"):
                a, b = spans[period]
                store[period] = c.execute("SELECT COUNT(*) FROM mg_orders WHERE status IN ('complete', 'closed') "
                                          "AND placed_on BETWEEN ? AND ?", (a, b)).fetchone()[0]
        except Exception as e:                          # noqa: BLE001  (no store orders pulled yet)
            if not core.missing_table(e):
                raise
            store = {"now": 0, "before": 0}
        pages = _pages(c)
    rows = pd.concat(frames, ignore_index=True)
    rows["month"] = rows["date"].str[:7].str.replace("-", "_")
    rows["channel"] = rows["channel"].fillna("(not set)")
    comparable = bool(st) and before[0] >= st["first"]
    return {"months": months, "before": before, "rows": rows, "store": store, "pages": pages,
            "comparable": comparable, "cut": cut}


def totals(data):
    r = data["rows"]
    out = {}
    for p in ("now", "before"):
        part = r[r["period"] == p]
        visits, orders = float(part["visits"].sum()), float(part["orders"].sum())
        out[p] = {"visits": visits, "orders": orders, "revenue": float(part["revenue"].sum()),
                  "rate": orders / visits * 100 if visits else None, "store": data["store"].get(p, 0)}
    return out


def by_source(data):
    r = data["rows"]
    now = r[r["period"] == "now"].groupby("channel")[["visits", "orders", "revenue"]].sum()
    before = r[r["period"] == "before"].groupby("channel")[["orders"]].sum().rename(columns={"orders": "orders_b"})
    df = now.join(before, how="outer").fillna(0.0)
    df = df[(df["visits"] > 0) | (df["orders"] > 0) | (df["orders_b"] > 0)].sort_values(["orders", "visits"], ascending=False)
    total = df.sum()
    df.loc["Total"] = total
    df = df.reset_index().rename(columns={"channel": "Source", "index": "Source"})
    out = pd.DataFrame({"Source": df["Source"], "Visits": df["visits"], "Orders": df["orders"], "Revenue": df["revenue"],
                        "Orders per 100 visits": [o / v * 100 if v else None for o, v in zip(df["orders"], df["visits"])]})
    if data["comparable"]:
        out["Orders a year earlier"] = df["orders_b"]
        out["Change %"] = [sales.change(a, b) for a, b in zip(df["orders"], df["orders_b"])]
    return out


def weekly_orders(data):
    """Orders per week (weeks start on Monday) of the selected months, by source; small sources as Other sources."""
    r = data["rows"][data["rows"]["period"] == "now"]
    top = r.groupby("channel")["orders"].sum().nlargest(TOP_SOURCES).index
    r = r.assign(source=r["channel"].where(r["channel"].isin(top), OTHER),
                 week=pd.to_datetime(r["date"]).dt.to_period("W-SUN").dt.start_time)
    df = r.groupby(["week", "source"], as_index=False)["orders"].sum()
    if df.empty:
        return df
    full = pd.MultiIndex.from_product([sorted(df["week"].unique()), sorted(df["source"].unique())],
                                      names=["week", "source"])
    return df.set_index(["week", "source"]).reindex(full, fill_value=0.0).reset_index()


def by_page(data, limit=60):
    """Landing pages of the selected months by orders. A product page carries its SKU (for its window)."""
    r = data["rows"][data["rows"]["period"] == "now"]
    df = r.groupby("page", as_index=False)[["visits", "orders", "revenue"]].sum()
    df = df[(df["visits"] > 0) | (df["orders"] > 0)].sort_values(["orders", "visits"], ascending=False).head(limit)
    names, skus = [], []
    for p in df["page"]:
        sku, name = data["pages"].get(p, ("", ""))
        skus.append(sku)
        names.append("Homepage" if p == "/" else name or p)
    return pd.DataFrame({"Page": names, "Address": df["page"].tolist(), "Visits": df["visits"].tolist(),
                         "Orders": df["orders"].tolist(), "Revenue": df["revenue"].tolist(),
                         "Orders per 100 visits": [o / v * 100 if v else None for o, v in zip(df["orders"], df["visits"])]}), skus
