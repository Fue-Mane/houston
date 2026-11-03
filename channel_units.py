"""Units sold per product, month and sales channel, for the "Units sold per month" chart of the product window.

rebuild(c) fills sales_units right after the channel rules run (health.rebuild calls it after
channels.rebuild_quietly), and the chart fills it itself if the channel rules ran since it was last counted: one row per database, month, WinMENTOR SKU and channel, adding up the units (c5) of
every GetVanzariLuna line (wm_sales_month) whose document has a channel in sales_docs. Same base as the Sales pages
(sales.py). Samples and donations ("Not a sale") are left out. sales_docs holds only the database that owns each
month, so nothing is counted twice.

Checked 24 Oct 2026 on the Baule Volante apple vinegar (EAN 8018848017960): for May to October 2026, the channels
add up exactly to the units WinMENTOR's first sales list gives (wm_sales_by_ean_month), e.g. September 565 =
eMAG wholesale 300 + BEBETEI 262 + People offline 3.
"""
from contextlib import closing

import pandas as pd
import streamlit as st

import channels
import core
import winmentor as wm

ALL = "All sales"
SHOWN = [channels.WEBSITE, channels.MARKETPLACE, channels.WHOLESALE, channels.BEBETEI, channels.B2B]
COLORS = {ALL: "#4c78a8", channels.WEBSITE: "#2fb36b", channels.MARKETPLACE: "#f58518",
          channels.WHOLESALE: "#e45756", channels.BEBETEI: "#b279a2", channels.B2B: "#72b7b2"}

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sales_units (company TEXT, month TEXT, code TEXT, channel TEXT, qty REAL,
        PRIMARY KEY (company, month, code, channel))""",
    "CREATE INDEX IF NOT EXISTS ix_sales_units_code ON sales_units (company, code)",
    "CREATE TABLE IF NOT EXISTS sales_units_built (docs_built TEXT)",   # which channel run the units come from
]
BUILD = """
INSERT INTO sales_units
SELECT l.company, l.period, TRIM(COALESCE(l.c3, '')), d.channel, SUM(CAST(REPLACE(l.c5, ',', '.') AS REAL))
FROM wm_sales_month l JOIN sales_docs d ON d.company = l.company AND d.month = l.period AND d.doc = l.c15
WHERE d.channel <> ? AND TRIM(COALESCE(l.c3, '')) <> ''
GROUP BY l.company, l.period, TRIM(COALESCE(l.c3, '')), d.channel
"""


def rebuild(c):
    for sql in SCHEMA:
        c.execute(sql)
    c.execute("DELETE FROM sales_units")
    c.execute(BUILD, (channels.NOT_SALE,))
    c.execute("DELETE FROM sales_units_built")
    c.execute("INSERT INTO sales_units_built SELECT MAX(built_at) FROM sales_docs")


def _fresh(c):
    """True when sales_units was counted from the current channel run (sales_docs)."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'sales_units_built'").fetchone():
        return False
    built = c.execute("SELECT docs_built FROM sales_units_built").fetchone()
    docs = c.execute("SELECT MAX(built_at) FROM sales_docs").fetchone()
    return bool(built) and built[0] == docs[0]


def rebuild_quietly(c):
    """rebuild() inside health's run. A failure is undone and shown on Data health instead of stopping the checks."""
    c.execute("SAVEPOINT units")
    try:
        rebuild(c)
    except Exception as e:                       # noqa: BLE001 (shown on the Data health page)
        c.execute("ROLLBACK TO units")
        c.execute("RELEASE units")
        return [(channels.CHECK["id"], "-", "Units per channel couldn't be counted", f"{type(e).__name__}: {e}")]
    c.execute("RELEASE units")
    return []


def per_month(e):
    """{channel: {month: units}} for one EAN, over every database (each month from the database that owns it).
    None before the channel rules have ever run. Counts the units first if the channel rules ran since."""
    with closing(wm.connect()) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'sales_docs'").fetchone():
            return None
        if not _fresh(c):
            if core.READ_ONLY:
                return None
            with st.spinner("Counting units per sales channel, once after each WinMENTOR load"), c:
                rebuild(c)
        rows = c.execute("SELECT u.month, u.channel, SUM(u.qty) FROM sales_units u JOIN wm_codes k "
                         "ON k.company = u.company AND k.sku = u.code WHERE k.ean = ? GROUP BY u.month, u.channel",
                         (e,)).fetchall()
    out = {}
    for month, channel, qty in rows:
        out.setdefault(channel, {})[month] = qty or 0.0
    return out


def chart(e, series):
    """The units-sold chart: the All sales line as before, plus one line per channel in SHOWN. Pointing at the
    chart shows a vertical line at the nearest month, with every line's value for that month."""
    import altair as alt
    frames = [pd.DataFrame(series, columns=["month", "qty"]).assign(series=ALL)]
    per = per_month(e)
    for ch in SHOWN:
        got = (per or {}).get(ch)
        if not got:
            continue
        months = wm.month_range(min(got), max(got))          # zero for months in between with no sale in this channel
        frames.append(pd.DataFrame({"month": months, "qty": [got.get(m, 0.0) for m in months], "series": ch}))
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["month"].str.replace("_", "-") + "-01")
    order = [ALL] + [ch for ch in SHOWN if (per or {}).get(ch)]

    # one row per month, one column per line, for the hover line and its values
    wide = df.pivot_table(index="date", columns="series", values="qty", aggfunc="sum").reset_index()
    keys = {s: f"v{i}" for i, s in enumerate(order)}
    wide = wide.rename(columns=keys)
    for k in keys.values():
        if k not in wide:
            wide[k] = None

    x = alt.X("date:T", title=None)
    near = alt.selection_point(nearest=True, on="pointerover", clear="pointerout", fields=["date"], empty=False)
    base = alt.Chart(df).encode(
        x=x, y=alt.Y("qty:Q", title="Units sold"),
        color=alt.Color("series:N", title=None, sort=order,
                        scale=alt.Scale(domain=order, range=[COLORS[s] for s in order]),
                        legend=alt.Legend(orient="bottom")))
    lines = base.mark_line().encode(
        strokeWidth=alt.condition(alt.datum.series == ALL, alt.value(2.5), alt.value(1.5)))
    dots = base.transform_filter(alt.datum.series == ALL).mark_point(filled=True, size=30)
    hovered = base.mark_point(filled=True, size=60).encode(opacity=alt.condition(near, alt.value(1), alt.value(0)))
    dark = (core.setting("theme") or "dark") != "light"
    rule = alt.Chart(wide).mark_rule(color="#ffffff" if dark else "#555555", strokeWidth=1).encode(
        x=x, opacity=alt.condition(near, alt.value(0.7), alt.value(0)),
        tooltip=[alt.Tooltip("date:T", title="Month", format="%b %Y")] +
                [alt.Tooltip(field=keys[s], type="quantitative", title=s, format=",.0f") for s in order],
    ).add_params(near)
    st.altair_chart(alt.layer(lines, dots, hovered, rule).properties(height=300).interactive(bind_y=False))
    note = ("Point at the chart to see each month's figures; scroll to zoom, drag to move. One figure per month: "
            "Jan 2019 to Jun 2020 is held twice (BIOMANIA and BIO2019, identical copies), and only BIO2019's copy "
            "is used.")
    if per is None:
        note += " The channel lines appear once the channel rules have run (Data health, Run pre-flight checks)."
    else:
        note += (" The channel lines use the Sales page rules; People offline and sales with no channel are in "
                 "All sales but have no line of their own. Returns can take a month below zero.")
    st.caption(note)
