"""Product Analysis (build plan step 5): what its pages show. Read-only, from Houston's database.

- Search Console figures per product page come from search_data, as everywhere else in Houston.
- Website units are WinMENTOR's (sales_units, channel Website) and stock is Depozit central + VANZARI EN-GROS
  (health_products), as in the product window. For packs (SKU/N) both are counted in WinMENTOR's singles.
- Search Console has no data for 23 Jun - 8 Sep 2026, so every window starts on 9 Sep 2026 or later.
  A year earlier means the same dates in 2025.
- Third-party reports (not Google) say Google stopped serving 100 results per page around 10-14 Sep 2025, which
  removed many bot impressions from Search Console. A year-earlier window that touches those days has inflated
  impressions, so a rise looks smaller than it is.
"""
import datetime
import re
from contextlib import closing

import pandas as pd

import core
import search_data as sd

AFTER_GAP = datetime.date(2026, 9, 9)
NUM100 = (datetime.date(2025, 9, 10), datetime.date(2025, 9, 14))
WINDOWS = ["Last 28 days", "Since 9 Sep 2026"]
MIN_RISING = 50          # a rising search needs at least this many impressions in the window
MIN_QUESTION = 10
TOP = 30
BRAND = re.compile(r"bio\s*-?\s*mania", re.I)
# Question searches: a question word first, or a word that asks for information. Romanian, with and without
# diacritics. A rule of thumb, shown on the page as it is.
QUESTION_FIRST = ["ce", "cum", "cat", "cât", "cate", "câte", "care", "unde", "cand", "când", "cine", "de ce",
                  "la ce", "pentru ce", "din ce"]
QUESTION_WORDS = ["beneficii", "contraindicatii", "contraindicații", "reteta", "rețeta", "rețetă", "retete",
                  "rețete", "diferenta", "diferența", "ce este", "cum se", "cat timp", "cât timp", "este bun",
                  "e bun", "efecte", "proprietati", "proprietăți"]
_QUESTION = re.compile(r"^(?:%s)\b|\b(?:%s)\b" % ("|".join(map(re.escape, QUESTION_FIRST)),
                                                 "|".join(map(re.escape, QUESTION_WORDS))), re.I)


def is_question(query):
    return bool(_QUESTION.search(query or ""))


def default_property():
    return sd.default_property()


def latest_day(prop):
    with closing(core.connect()) as c:
        row = c.execute("SELECT MAX(date) FROM gsc_totals WHERE property = ?", (prop,)).fetchone()
    return datetime.date.fromisoformat(row[0]) if row and row[0] else None


def window(prop, label):
    """(start, end) dates for a window label, ending on the latest day Houston holds, never before 9 Sep 2026."""
    end = latest_day(prop)
    if not end:
        return None
    if label == WINDOWS[1]:
        return AFTER_GAP, end
    return max(AFTER_GAP, end - datetime.timedelta(days=27)), end


def year_earlier(d):
    try:
        return d.replace(year=d.year - 1)
    except ValueError:                                    # 29 February
        return d.replace(year=d.year - 1, day=28)


def touches_num100(start, end):
    a, b = year_earlier(start), year_earlier(end)
    return a <= NUM100[1] and b >= NUM100[0]


def _catalogue(c):
    """{sku: (name, url_key)} for live products in the catalogue copy."""
    return {sku: (name or "", key or "") for sku, name, key in
            c.execute("SELECT sku, name, url_key FROM products WHERE status = '1' AND COALESCE(read_error, '') = ''")}


def recent_months(n=3):
    """The newest WinMENTOR month with website sales, and the n-1 before it ('yyyy_MM')."""
    with closing(core.connect()) as c:
        try:
            last = c.execute("SELECT MAX(month) FROM sales_units WHERE channel = 'Website'").fetchone()[0]
        except Exception as e:                            # noqa: BLE001  (channels not built yet)
            if not core.missing_table(e):
                raise
            return []
    if not last:
        return []
    y, m = int(last[:4]), int(last[5:7])
    out = []
    for back in range(n - 1, -1, -1):
        yy, mm = y, m - back
        while mm <= 0:
            yy, mm = yy - 1, mm + 12
        out.append(f"{yy}_{mm:02d}")
    return out


def months_label(months):
    if not months:
        return ""
    name = lambda m: datetime.date(int(m[:4]), int(m[5:7]), 1).strftime("%b")  # noqa: E731
    return f"{name(months[0])}–{name(months[-1])} {months[-1][:4]}" if len(months) > 1 else \
        f"{name(months[0])} {months[0][:4]}"


def website_units(months):
    """{WinMENTOR code: units sold on the website} for the months given."""
    if not months:
        return {}
    with closing(core.connect()) as c:
        try:
            return {(code or "").strip(): qty or 0 for code, qty in c.execute(
                f"SELECT code, SUM(qty) FROM sales_units WHERE channel = 'Website' AND month IN "
                f"({','.join('?' * len(months))}) GROUP BY code", months)}
        except Exception as e:                            # noqa: BLE001  (channels not built yet)
            if not core.missing_table(e):
                raise
            return {}


def products(prop, start, end):
    """One row per live product for the window: DataFrame with sku and the Overview's columns, most impressions
    first. Returns (df, months) where months are the WinMENTOR months counted in Website units."""
    with closing(core.connect()) as c:
        cat = _catalogue(c)
        try:
            hp = {sku: (wm, stock, online, pack) for sku, wm, stock, online, pack in c.execute(
                "SELECT sku, wm_code, stock, online_in_stock, pack_of FROM health_products")}
        except Exception as e:                            # noqa: BLE001  (no pre-flight run yet)
            if not core.missing_table(e):
                raise
            hp = {}
    search = sd.per_product(prop, start.isoformat(), end.isoformat(), {s: {"url_key": k} for s, (_, k) in cat.items()})
    months = recent_months()
    units = website_units(months)
    rows = []
    for sku, (name, _) in cat.items():
        s = search.get(sku) or {}
        wm, stock, online, pack = hp.get(sku.strip(), (None, None, None, 1))
        impr = s.get("impressions") or 0
        rows.append({"sku": sku, "SKU": sku.strip(), "Product": name, "Impressions": impr, "Clicks": s.get("clicks") or 0,
                     "CTR %": round(100 * s["ctr"], 1) if impr else None,
                     "Avg. position": round(s["position"], 1) if s.get("position") else None,
                     "Top search": s.get("top_query") or "",
                     "Website units": units.get((wm or "").strip(), 0) if wm else None,
                     "Stock": stock, "In stock online": None if online is None else ("Yes" if online else "No"),
                     "Pack": (pack or 1) > 1})
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["Impressions", "Clicks", "Product"], ascending=[False, False, True]).reset_index(drop=True)
    return df, months


def _per_query(c, pid, start, end):
    return {q: (impr or 0, clicks or 0, pos or 0.0) for q, impr, clicks, pos in c.execute(
        "SELECT query, SUM(impressions), SUM(clicks), SUM(position * impressions) FROM gsc_queries_c "
        "WHERE prop = ? AND date BETWEEN ? AND ? GROUP BY query", (pid, start, end))}


def searches(prop, start, end):
    """Every search that showed the site in the window, with the same dates a year earlier and the page it showed
    most. DataFrame: query, impressions, clicks, position, impressions_before, clicks_before, position_before,
    page, sku, product."""
    a, b = start.isoformat(), end.isoformat()
    a0, b0 = year_earlier(start).isoformat(), year_earlier(end).isoformat()
    with closing(core.connect()) as c:
        pid = sd._prop_id(c, prop)
        now, before = _per_query(c, pid, a, b), _per_query(c, pid, a0, b0)
        best = {}
        for q, page, impr in c.execute("SELECT query, page, SUM(impressions) FROM gsc_queries_c WHERE prop = ? AND "
                                       "date BETWEEN ? AND ? GROUP BY query, page", (pid, a, b)):
            if impr > best.get(q, (None, -1))[1]:
                best[q] = (page, impr)
        wanted = list(now) + list({p for p, _ in best.values()})
        names = {}
        for i in range(0, len(wanted), 500):
            part = wanted[i:i + 500]
            names.update(c.execute(f"SELECT id, value FROM dict WHERE id IN ({','.join('?' * len(part))})", part))
        cat = _catalogue(c)
    by_url = {}
    for sku, (name, key) in cat.items():
        for u in sd.page_urls(key):
            by_url[u] = (sku, name)
    rows = []
    for q, (impr, clicks, pos) in now.items():
        i0, c0, p0 = before.get(q, (0, 0, 0.0))
        url = names.get(best.get(q, (None, 0))[0], "")
        sku, product = by_url.get(url, ("", ""))
        rows.append({"query": names.get(q, ""), "impressions": impr, "clicks": clicks,
                     "position": pos / impr if impr else None, "impressions_before": i0, "clicks_before": c0,
                     "position_before": p0 / i0 if i0 else None, "page": url.replace(sd.SITE, "/") if url else "",
                     "sku": sku, "product": product})
    return pd.DataFrame(rows, columns=["query", "impressions", "clicks", "position", "impressions_before",
                                       "clicks_before", "position_before", "page", "sku", "product"])


def rising(df, top=TOP):
    """The searches whose impressions rose most against a year earlier. Searches with 'biomania' are left out."""
    if df.empty:
        return df
    d = df[(df["impressions"] >= MIN_RISING) & ~df["query"].str.contains(BRAND)].copy()
    d["change"] = d["impressions"] - d["impressions_before"]
    return d[d["change"] > 0].sort_values("change", ascending=False).head(top).reset_index(drop=True)


def questions(df, top=TOP):
    """Question searches that showed the site, most impressions first."""
    if df.empty:
        return df
    d = df[(df["impressions"] >= MIN_QUESTION) & df["query"].map(is_question) & ~df["query"].str.contains(BRAND)]
    return d.sort_values("impressions", ascending=False).head(top).reset_index(drop=True)
