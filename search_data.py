"""Search Console figures per product, read from data/houston.db.

A product's page is https://www.biomania.ro/<url_key>.html (checked 9 Oct 2026: 303 of 516 live products
matched a page with impressions). Position is averaged weighted by impressions, which is how a
multi-day average has to be computed from daily rows.
"""
import datetime
from contextlib import closing

import pandas as pd

import core

SITE = "https://www.biomania.ro/"
WINDOWS = {"Last 28 days": 28, "Last 90 days": 90, "Everything stored": None}


def page_urls(url_key):
    k = (url_key or "").strip()
    return [f"{SITE}{k}.html", f"{SITE}{k}"] if k else []


def default_property():
    with closing(core.connect()) as c:
        row = c.execute("SELECT property FROM gsc_totals GROUP BY property ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
    return row[0] if row else None


def window(prop, label):
    """(start, end) as ISO dates, ending on the latest stored day."""
    with closing(core.connect()) as c:
        first, last = c.execute("SELECT MIN(date), MAX(date) FROM gsc_totals WHERE property = ?", (prop,)).fetchone()
    if not last:
        return None
    days = WINDOWS.get(label)
    end = datetime.date.fromisoformat(last)
    start = datetime.date.fromisoformat(first) if days is None else end - datetime.timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def gaps_inside(prop, start, end):
    import google_sc
    return [(a, b, n) for a, b, n in google_sc.missing_ranges(prop) if a.isoformat() <= end and b.isoformat() >= start]


def per_product(prop, start, end, products):
    """{sku: {impressions, clicks, ctr, position, top_query}} for the window."""
    url_to_sku = {}
    for sku, r in products.items():
        for u in page_urls(r.get("url_key")):
            url_to_sku[u] = sku
    out = {}
    with closing(core.connect()) as c:
        for page, clicks, impr, pos in c.execute(
                "SELECT page, SUM(clicks), SUM(impressions), SUM(position * impressions) FROM gsc_pages "
                "WHERE property = ? AND date BETWEEN ? AND ? GROUP BY page", (prop, start, end)):
            sku = url_to_sku.get(page)
            if not sku:
                continue
            o = out.setdefault(sku, {"impressions": 0, "clicks": 0, "_pos": 0.0, "top_query": ""})
            o["impressions"] += impr or 0
            o["clicks"] += clicks or 0
            o["_pos"] += pos or 0.0
        best = {}
        for page, query, impr in c.execute(
                "SELECT page, query, SUM(impressions) AS i FROM gsc_queries WHERE property = ? AND date BETWEEN ? AND ? "
                "GROUP BY page, query", (prop, start, end)):
            sku = url_to_sku.get(page)
            if sku and impr > best.get(sku, ("", -1))[1]:
                best[sku] = (query, impr)
    for sku, o in out.items():
        o["ctr"] = o["clicks"] / o["impressions"] if o["impressions"] else 0.0
        o["position"] = o.pop("_pos") / o["impressions"] if o["impressions"] else None
        o["top_query"] = best.get(sku, ("", 0))[0]
    return out


def daily(prop, url_key, start, end):
    urls = page_urls(url_key)
    marks = ",".join("?" * len(urls))
    with closing(core.connect()) as c:
        return pd.read_sql_query(
            f"SELECT date, SUM(clicks) AS clicks, SUM(impressions) AS impressions, SUM(position * impressions) AS pos_x_impr "
            f"FROM gsc_pages WHERE property = ? "
            f"AND page IN ({marks}) AND date BETWEEN ? AND ? GROUP BY date ORDER BY date",
            c, params=[prop, *urls, start, end])


def queries(prop, url_key, start, end, limit=200):
    urls = page_urls(url_key)
    marks = ",".join("?" * len(urls))
    with closing(core.connect()) as c:
        df = pd.read_sql_query(
            f"SELECT query, SUM(clicks) AS clicks, SUM(impressions) AS impressions, "
            f"SUM(position * impressions) * 1.0 / SUM(impressions) AS position FROM gsc_queries WHERE property = ? "
            f"AND page IN ({marks}) AND date BETWEEN ? AND ? GROUP BY query ORDER BY impressions DESC LIMIT ?",
            c, params=[prop, *urls, start, end, limit])
    df["ctr"] = (df["clicks"] / df["impressions"] * 100).round(1).fillna(0)
    return df[["query", "impressions", "clicks", "ctr", "position"]]
