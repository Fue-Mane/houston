"""Sales section: Total (every sale of the company) and one page per channel, all with the same layout.

Each page: months to look at against the same months a year earlier; totals in lei without VAT and in product
units and clients; sales added up day by day with chart notes on their dates; a monthly chart; what changed
against a year earlier (a tree: kept products split into units, mix and prices; range changes into new and no longer
sold; fees and discounts); the biggest gains and losses; products bought together (Website and eMAG Marketplace);
tables by brand and by product (a click on a product opens its window). Total has an empty forecast section.
Total starts with the channel table and has the chart-notes editor. eMAG shows Marketplace and wholesale separately.
"""
import datetime

import altair as alt
import pandas as pd
import streamlit as st

import channels
import core
import notes
import product_card
import sales

EMAG_PARTS = [channels.MARKETPLACE, channels.WHOLESALE]
LEI = st.column_config.NumberColumn(format="%,.0f")          # 1,234,567 (comma flag checked in Streamlit 1.66)
UNITS = st.column_config.NumberColumn(format="%,.0f")
PCT = st.column_config.NumberColumn(format="%+,.0f%%")


@st.cache_data(show_spinner="Adding up the sales", max_entries=16)
def _load(first, last, chans, version):          # version: rebuilt channels give a fresh result
    return sales.load(first, last, list(chans) if chans else None)


def _pct(now, before):
    ch = sales.change(now, before)
    return None if ch is None else f"{ch:+.1f}% vs a year earlier"


def _period(info, key):
    months = sales.month_range(info["first"], info["last"])
    start = f"{info['last'][:4]}_01" if f"{info['last'][:4]}_01" in months else months[0]
    a, b, _ = st.columns([1, 1, 3])
    first = a.selectbox("From", months, index=months.index(start), format_func=sales.label, key=key + "_from")
    last = b.selectbox("To", months, index=len(months) - 1, format_func=sales.label, key=key + "_to")
    if first > last:
        first, last = last, first
    return first, last


def _caption(data, info):
    text = (f"Lei without VAT, compared with {sales.label(data['before'][0])} to {sales.label(data['before'][-1])}. "
            f"WinMENTOR sales up to {info['last_day']:%d %b %Y}.")
    if data["cut"]:
        text += f" {sales.label(data['cut'][0])} is counted up to day {data['cut'][1]}, to match."
    st.caption(text + " Samples and donations aren't sales. Delivery fees and discounts are in the totals, not in "
                      "the brand and product tables.")


def _table_of(df):
    st.dataframe(df, hide_index=True,
                 column_config={"Lei": LEI, "Lei a year earlier": LEI, "Change %": PCT, "Clients": UNITS})


def _metrics(data):
    t = sales.totals(data)
    a, b, c = st.columns(3)
    a.metric("Sales, lei", f"{t['now']['value']:,.0f}", _pct(t["now"]["value"], t["before"]["value"]))
    b.metric("Product units", f"{t['now']['units']:,.0f}", _pct(t["now"]["units"], t["before"]["units"]))
    c.metric("Clients", f"{t['now']['clients']:,}", _pct(t["now"]["clients"], t["before"]["clients"]),
             help="Each shop of a company counts once, and each delivery address of a person counts once. "
                  "Returns and rebates don't count.")


def note_marks(page_notes):
    """Chart notes on a time axis: a dashed orange line on each note's date, with a marker to hover."""
    if page_notes.empty:
        return None
    df = page_notes.assign(date=pd.to_datetime(page_notes["day"]))
    df = df.groupby("date", as_index=False).agg(notes=("note", " | ".join), kinds=("kind", ", ".join))
    tip = [alt.Tooltip("date:T", title="Date", format="%d %b %Y"), alt.Tooltip("kinds:N", title="Kind"),
           alt.Tooltip("notes:N", title="Note")]
    rules = alt.Chart(df).mark_rule(color="#E37400", strokeDash=[4, 3], opacity=0.7).encode(x="date:T", tooltip=tip)
    pins = alt.Chart(df).mark_point(shape="triangle-down", filled=True, size=110, color="#E37400").encode(
        x="date:T", y=alt.value(6), tooltip=tip)
    return rules + pins


def notes_list(page_notes):
    if page_notes.empty:
        return
    with st.expander(f"Chart notes in these months ({len(page_notes)})"):
        st.markdown("\n".join(f"- {datetime.date.fromisoformat(d):%d %b %Y} · {k} · {n}"
                               for d, k, n in zip(page_notes["day"], page_notes["kind"], page_notes["note"])))


def _cumulative(data, info, page_notes):
    df = sales.cumulative(data, info["last_day"])
    start = datetime.date(int(data["months"][0][:4]), int(data["months"][0][5:]), 1)
    names = ["Selected months", "A year earlier"]
    line = alt.Chart(df).mark_line(strokeWidth=2.5).encode(
        x=alt.X("date:T", title=None, axis=alt.Axis(format="%d %b")),
        y=alt.Y("lei:Q", title="Lei, added up"),
        color=alt.Color("series:N", sort=names, title=None,
                        scale=alt.Scale(domain=names, range=["#2fb36b", "#8b8f98"])),
        strokeDash=alt.StrokeDash("series:N", sort=names, legend=None,
                                  scale=alt.Scale(domain=names, range=[[1, 0], [5, 3]])))
    wide = df.pivot(index="date", columns="series", values="lei").reset_index().rename(
        columns={"Selected months": "now", "A year earlier": "before"})
    wide["diff"] = wide["now"] - wide["before"]
    hover = alt.selection_point(nearest=True, on="mouseover", fields=["date"], empty=False)
    rule = alt.Chart(wide).mark_rule(color="#8b8f98").encode(
        x="date:T", opacity=alt.condition(hover, alt.value(0.6), alt.value(0)),
        tooltip=[alt.Tooltip("date:T", title="Date", format="%d %b %Y"),
                 alt.Tooltip("now:Q", title="Selected months, so far", format=",.0f"),
                 alt.Tooltip("before:Q", title="A year earlier, so far", format=",.0f"),
                 alt.Tooltip("diff:Q", title="Ahead (+) or behind (-)", format="+,.0f")]).add_params(hover)
    marks = note_marks(page_notes)
    chart = line + rule + marks if marks is not None else line + rule
    st.markdown(f"**Sales added up from {start:%d %b %Y}**")
    st.altair_chart(chart.properties(height=320))
    st.caption(f"Both lines start at zero on {start:%d %b}: the grey dashed one is the same dates a year earlier. "
               "Hover anywhere to read both totals and the gap. The gap depends on where the period starts: a strong "
               "first month carries its lead into the next. Orange lines are chart notes; hover the marker at the "
               "top to read them.")
    notes_list(page_notes)


def _heatmap(first, last):
    df = sales.order_times(first, last)
    n = int(df["orders"].sum())
    st.markdown("**When people order on the website**")
    if not n:
        st.caption("No website orders with a time in these months. Update the website orders in the sidebar.")
        return
    y = alt.Y("day:N", sort=sales.WEEKDAYS, title=None)
    x = alt.X("hour:O", title="Hour, Romanian time")
    cells = alt.Chart(df).mark_rect().encode(
        x=x, y=y, color=alt.Color("orders:Q", title="Orders", scale=alt.Scale(scheme="greens")),
        tooltip=[alt.Tooltip("day:N", title="Day"), alt.Tooltip("hours:N", title="Time"),
                 alt.Tooltip("orders:Q", title="Orders", format=",.0f")])
    text = alt.Chart(df[df["orders"] > 0]).mark_text(fontSize=10).encode(
        x=x, y=y, text=alt.Text("orders:Q", format=",.0f"),
        color=alt.condition(alt.datum.orders > df["orders"].max() / 2, alt.value("white"), alt.value("black")))
    st.altair_chart((cells + text).properties(height=230))
    st.caption(f"{n:,} completed website orders placed in these months, by the time Magento recorded them. Only "
               "website orders carry a time; WinMENTOR invoices have a date only.")


def _monthly(data):
    df = sales.monthly(data)
    order = list(dict.fromkeys(df["month"]))
    st.markdown("**By month**")
    st.altair_chart(alt.Chart(df).mark_bar().encode(
        x=alt.X("month:N", sort=order, title=None),
        xOffset=alt.XOffset("series:N", sort=["A year earlier", "Selected months"]),
        y=alt.Y("lei:Q", title="Lei"),
        color=alt.Color("series:N", sort=["A year earlier", "Selected months"], title=None,
                        scale=alt.Scale(range=["#8b8f98", "#2fb36b"])),
        tooltip=[alt.Tooltip("of:N", title="Month"), alt.Tooltip("lei:Q", title="Lei", format=",.0f")],
    ).properties(height=260))


EXPLAIN = {
    "volume": "Products sold in both periods: the change in units, counted at last year's average price per unit",
    "mix": "Products sold in both periods: selling more of the dearer ones (up) or of the cheaper ones (down)",
    "price": "Products sold in both periods: the change in what we got per unit of the same product",
    "new": "Products sold now that weren't sold at all a year earlier",
    "gone": "Products sold a year earlier that aren't sold now, including old versions replaced by a new code",
    "services": "Delivery fees, discounts and rebates",
}


TONES = {"up": "#1D9E75", "down": "#D85A30", "flat": "#888780"}
TREE_W, TREE_H = 680, 446


def _tone(lei):
    return "flat" if round(lei) == 0 else "up" if lei > 0 else "down"


def tree_chart(steps, dark=True):
    """The change against a year earlier as a tree: each box is the sum of the boxes to its right.
    Drawn with Altair (Streamlit's st.html strips SVG)."""
    s = {x["key"]: x for x in steps}
    kept = s["volume"]["lei"] + s["mix"]["lei"] + s["price"]["lei"]
    rng = s["new"]["lei"] + s["gone"]["lei"]
    change = s["now"]["lei"] - s["before"]["lei"]
    count = lambda k: f" ({s[k]['n']:,})" if s[k].get("n") is not None else ""  # noqa: E731
    boxes = [  # x, y, width, title, lei, what it means, tone
        (10, 241, 130, "Change", change, "Sales in the selected months minus the same months a year earlier", "flat"),
        (180, 78, 200, f"Kept products{count('volume')}", kept,
         "Products sold in both periods. The three boxes to the right add up to this.", None),
        (180, 266, 200, "Range changes", rng, "New products minus products no longer sold", None),
        (180, 380, 200, "Fees and discounts", s["services"]["lei"], EXPLAIN["services"], None),
        (430, 10, 230, "More units", s["volume"]["lei"], EXPLAIN["volume"], None),
        (430, 78, 230, "Product mix", s["mix"]["lei"], EXPLAIN["mix"], None),
        (430, 146, 230, "Prices", s["price"]["lei"], EXPLAIN["price"], None),
        (430, 232, 230, f"New products{count('new')}", s["new"]["lei"], EXPLAIN["new"], None),
        (430, 300, 230, f"No longer sold{count('gone')}", s["gone"]["lei"], EXPLAIN["gone"], None),
    ]
    df = pd.DataFrame([{"x": x, "x2": x + w, "y": y, "y2": y + 56, "cx": x + w / 2, "ty": y + 20, "vy": y + 39,
                        "title": t, "value": f"{lei:+,.0f} lei", "why": why, "color": TONES[tone or _tone(lei)]}
                       for x, y, w, t, lei, why, tone in boxes])
    segs = [(140, 269, 160, 269), (160, 106, 160, 408), (160, 106, 180, 106), (160, 294, 180, 294),
            (160, 408, 180, 408), (380, 106, 405, 106), (405, 38, 405, 174), (405, 38, 430, 38), (405, 106, 430, 106),
            (405, 174, 430, 174), (380, 294, 405, 294), (405, 260, 405, 328), (405, 260, 430, 260),
            (405, 328, 430, 328)]
    lines = pd.DataFrame(segs, columns=["x", "y", "x2", "y2"])
    X = alt.Scale(domain=[0, TREE_W])
    Y = alt.Scale(domain=[0, TREE_H], reverse=True)
    ink = "#FAFAFA" if dark else "#31333F"
    tip = [alt.Tooltip("title:N", title="Box"), alt.Tooltip("value:N", title="Lei"),
           alt.Tooltip("why:N", title="What it means")]
    rules = alt.Chart(lines).mark_rule(color="#888780", strokeWidth=0.8).encode(
        x=alt.X("x:Q", scale=X, axis=None), y=alt.Y("y:Q", scale=Y, axis=None), x2="x2:Q", y2="y2:Q")
    base = alt.Chart(df)
    rects = base.mark_rect(cornerRadius=8, strokeWidth=1, fillOpacity=0.16).encode(
        x=alt.X("x:Q", scale=X, axis=None), x2="x2:Q", y=alt.Y("y:Q", scale=Y, axis=None), y2="y2:Q",
        fill=alt.Color("color:N", scale=None), stroke=alt.Color("color:N", scale=None), tooltip=tip)
    titles = base.mark_text(fontSize=14, fontWeight=600, color=ink, baseline="middle").encode(
        x=alt.X("cx:Q", scale=X, axis=None), y=alt.Y("ty:Q", scale=Y, axis=None), text="title:N", tooltip=tip)
    values = base.mark_text(fontSize=13, color=ink, opacity=0.8, baseline="middle").encode(
        x=alt.X("cx:Q", scale=X, axis=None), y=alt.Y("vy:Q", scale=Y, axis=None), text="value:N", tooltip=tip)
    return (rules + rects + titles + values).properties(width=TREE_W, height=TREE_H).configure_view(strokeWidth=0)


def _bridge(data):
    steps = sales.bridge(data)
    before, now = steps[0]["lei"], steps[-1]["lei"]
    st.markdown("**What changed against a year earlier**")
    st.markdown(f"{before:,.0f} lei a year earlier, {now:,.0f} lei now: **{now - before:+,.0f} lei**. "
                "Each box is the sum of the boxes to its right.")
    chart = tree_chart(steps, dark=(core.setting("theme") or "dark") != "light")
    try:
        st.altair_chart(chart, width="content")
    except TypeError:                                 # a Streamlit without the width option
        st.altair_chart(chart, use_container_width=False)
    st.caption("Every product is in one of three groups: kept (sold in both periods), new, or no longer sold. "
               "Hover a box for what it means. Prices is what we got per unit of the same product, so it also moves "
               "when more of a product goes to customers who pay less, such as B2B rather than the website.")


def _movers(data, key):
    st.markdown("**Biggest gains and losses against a year earlier**")
    by = st.radio("Show", ["Products", "Brands"], horizontal=True, key=key + "_movers_by", label_visibility="collapsed")
    gains, losses = sales.movers(data, "code" if by == "Products" else "brand")
    cfg = {"Lei": LEI, "Change": st.column_config.NumberColumn(format="%+,.0f")}
    left, right = st.columns(2)
    for col, df, title in ((left, gains, "Gains"), (right, losses, "Losses")):
        with col:
            st.caption(title)
            if df.empty:
                st.caption("None.")
            elif by == "Products":
                show = df[["name", "Lei", "Change", "Note"]].rename(columns={"name": "Product"})
                product_card.clickable_table(show, df["sku"].tolist(), key=f"{key}_{title.lower()}",
                                             hide_index=True, column_config=cfg)
            else:
                st.dataframe(df[["brand", "Lei", "Change"]].rename(columns={"brand": "Brand"}), hide_index=True,
                             column_config=cfg)


def _bundles(first, last, chans, info, where):
    df, n = _load_bundles(first, last, tuple(chans), info["version"])
    st.markdown("**Bought together**")
    if df.empty:
        st.caption(f"No two products were bought together on 3 or more {where} orders in these months.")
        return
    st.dataframe(df, hide_index=True, column_config={
        "Orders with both": UNITS, "% of the product's orders": st.column_config.NumberColumn(format="%.0f%%")})
    st.caption(f"Pairs of products on the same order, from {n:,} {where} orders in these months, shown when they "
               "were bought together at least 3 times. The first product is the one bought less often: the share "
               "says how many of its orders also had the other one. Ideas for bundles and for \"often bought with\" "
               "on the product pages.")


@st.cache_data(show_spinner="Finding products bought together", max_entries=8)
def _load_bundles(first, last, chans, version):
    return sales.bundles(first, last, list(chans))


def _forecast():
    st.markdown("**Forecast to the end of the year**")
    st.caption("Empty for now: this is where the forecast from your models will go.")


def _tables(data, key):
    t_brand, t_product = st.tabs(["By brand", "By product"])
    num = {"Lei": LEI, "Lei a year earlier": LEI, "Change %": PCT, "Units": UNITS, "Units a year earlier": UNITS}
    with t_brand:
        df = sales.grouped(data, "brand").rename(columns={"brand": "Brand"})
        st.dataframe(df, hide_index=True, column_config=num)
    with t_product:
        df = sales.grouped(data, "code")
        skus = df["sku"].tolist()
        show = df.drop(columns=["sku", "supplier"]).rename(columns={"code": "SKU", "name": "Product", "brand": "Brand"})
        st.caption("Click a product to open its window.")
        product_card.clickable_table(show, skus, key=f"{key}_products", hide_index=True,
                                     column_config=num)


def notes_editor(key):
    """Add, change or delete chart notes; fetch new Google updates."""
    with st.expander("Chart notes: add or change"):
        st.caption("Notes show as orange markers on the charts; hover a marker to read them. A note for one channel "
                   "shows on that channel's page and on Total. Google updates come from Google's Search Status "
                   "Dashboard.")
        edited = st.data_editor(
            notes.table(), key=key + "_notes", num_rows="dynamic", hide_index=True,
            column_order=["day", "kind", "channel", "note"],
            column_config={"day": st.column_config.DateColumn("Date", required=True, format="D MMM YYYY"),
                           "kind": st.column_config.SelectboxColumn("Kind", options=notes.KINDS, required=True),
                           "channel": st.column_config.SelectboxColumn("Shows on", options=[notes.ALL] + channels.ORDER,
                                                                       required=True),
                           "note": st.column_config.TextColumn("Note", required=True, width="large")})
        a, b, _ = st.columns([1, 1, 2])
        if a.button("Save notes", key=key + "_notes_save", use_container_width=True):
            saved, kept = notes.save(edited)
            st.success(f"{saved} notes saved.")
            if kept:
                st.warning(f"{kept} note{'s' if kept != 1 else ''} with the text cleared kept as before. To delete a "
                           "note, select its row and delete the row.")
        if b.button("Get new Google updates", key=key + "_notes_google", use_container_width=True):
            try:
                n = notes.update_google()
                st.success(f"{n} new Google update{'s' if n != 1 else ''} added." if n else "No new Google updates.")
            except RuntimeError as e:
                st.error(str(e))


def _page_notes(data, chans):
    first, last = data["months"][0], data["months"][-1]
    end = datetime.date(int(last[:4]), int(last[5:]), 1) + datetime.timedelta(days=31)
    end = end.replace(day=1) - datetime.timedelta(days=1)
    return notes.for_chart(f"{first[:4]}-{first[5:]}-01", end.isoformat(), chans)


def _render(title, chans, key):
    st.header("Total sales" if chans is None else f"Sales · {title}")
    if sales.needs_rebuild():
        import health
        with st.spinner("Updating the channel table once (about 10 seconds)"):
            health.rebuild_quietly("Sales pages: shops for the client count")
    info = sales.status()
    if not info:
        st.info("No sales to show yet. Run the pre-flight checks on Data health after a WinMENTOR update.")
        return
    first, last = _period(info, key)
    data = _load(first, last, tuple(chans) if chans else None, info["version"])
    _caption(data, info)
    note_chans = None if chans is None else list(chans) + (["eMAG"] if chans == EMAG_PARTS else [])
    if chans is None:
        st.markdown("**By channel**")
        _table_of(sales.by_channel(data, channels.ORDER))
    elif chans == EMAG_PARTS:
        _table_of(sales.by_channel(data, EMAG_PARTS).replace({channels.TOTAL: "eMAG"}))
        part = st.radio("Show", ["Both", channels.MARKETPLACE, channels.WHOLESALE], horizontal=True, key=key + "_part")
        if part != "Both":
            data = _load(first, last, (part,), info["version"])
    _metrics(data)
    _cumulative(data, info, _page_notes(data, note_chans))
    _monthly(data)
    if chans is None:
        _forecast()
    if chans == [channels.WEBSITE]:
        _heatmap(first, last)
    _bridge(data)
    _movers(data, key)
    if chans == [channels.WEBSITE]:
        _bundles(first, last, [channels.WEBSITE], info, "website")
    elif chans == EMAG_PARTS:
        _bundles(first, last, [channels.MARKETPLACE], info, "eMAG Marketplace")
    _tables(data, key)
    if chans is None:
        notes_editor(key)


def page_total():
    _render("Total", None, "sales_total")


def page_website():
    _render("Website", [channels.WEBSITE], "sales_web")


def page_emag():
    _render("eMAG", EMAG_PARTS, "sales_emag")


def page_bebetei():
    _render("BEBETEI", [channels.BEBETEI], "sales_bebetei")


def page_b2b():
    _render("B2B", [channels.B2B], "sales_b2b")


def page_offline():
    _render("People offline", [channels.OFFLINE], "sales_offline")


def pages():
    return [st.Page(page_total, title="Total", url_path="sales-total"),
            st.Page(page_website, title="Website", url_path="sales-website"),
            st.Page(page_emag, title="eMAG", url_path="sales-emag"),
            st.Page(page_bebetei, title="BEBETEI", url_path="sales-bebetei"),
            st.Page(page_b2b, title="B2B", url_path="sales-b2b"),
            st.Page(page_offline, title="People offline", url_path="sales-people-offline")]
