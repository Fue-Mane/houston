"""Google Analytics page, in the Marketing section: Traffic sources first, then the data pulls and checks.

Traffic sources shows where the website's visits and orders come from, by Google Analytics. The data tab is the
existing page (views.page_google_analytics) without its title.
"""
import altair as alt
import streamlit as st

import marketing
import product_card
import sales
import views
import views_sales

NUM = st.column_config.NumberColumn(format="%,.0f")
RATE = st.column_config.NumberColumn(format="%,.1f")
PCT = st.column_config.NumberColumn(format="%+,.0f%%")


@st.cache_data(show_spinner="Reading Google Analytics", max_entries=16)
def _load(first, last, last_day):                 # last_day: a new pull gives a fresh result
    return marketing.load(first, last)


def _pct(now, before, comparable):
    ch = sales.change(now, before) if comparable else None
    return None if ch is None else f"{ch:+.1f}% vs a year earlier"


def _period(info):
    months = sales.month_range(info["first"], info["last"])
    start = f"{info['last'][:4]}_01" if f"{info['last'][:4]}_01" in months else months[0]
    a, b, _ = st.columns([1, 1, 3])
    first = a.selectbox("From", months, index=months.index(start), format_func=sales.label, key="mk_from")
    last = b.selectbox("To", months, index=len(months) - 1, format_func=sales.label, key="mk_to")
    return (first, last) if first <= last else (last, first)


def traffic_sources():
    info = marketing.status()
    if not info:
        st.info("No Google Analytics data yet. Pull it in the Data and pulls tab.")
        return
    first, last = _period(info)
    data = _load(first, last, info["last_day"].isoformat())
    t = marketing.totals(data)
    seen = f"{t['now']['orders'] / t['now']['store'] * 100:.0f}%" if t["now"]["store"] else "an unknown share"
    st.caption(
        f"Google Analytics up to {info['last_day']:%d %b %Y}; update it in the Data and pulls tab. Visits are "
        "engaged visits, which leaves out most bot traffic (Direct still has some in the worst months). Orders and "
        f"revenue (lei with VAT) are as Google Analytics credits them: it saw {t['now']['orders']:,.0f} orders in "
        f"these months, {seen} of the {t['now']['store']:,} the store recorded."
        + (f" {sales.label(last)} is counted up to day {data['cut'].day} in both years, to match."
           if data.get("cut") and data["comparable"] else "")
        + ("" if data["comparable"] else " No comparison with a year earlier: Houston's Google Analytics data "
                                          f"starts in {sales.label(info['first'])}."))
    c = data["comparable"]
    a, b, d, e = st.columns(4)
    a.metric("Visits", f"{t['now']['visits']:,.0f}", _pct(t["now"]["visits"], t["before"]["visits"], c))
    b.metric("Orders", f"{t['now']['orders']:,.0f}", _pct(t["now"]["orders"], t["before"]["orders"], c))
    d.metric("Revenue, lei", f"{t['now']['revenue']:,.0f}", _pct(t["now"]["revenue"], t["before"]["revenue"], c))
    e.metric("Orders per 100 visits", "-" if t["now"]["rate"] is None else f"{t['now']['rate']:.1f}",
             _pct(t["now"]["rate"] or 0, t["before"]["rate"] or 0, c and bool(t["before"]["rate"])))

    st.markdown("**Orders per week, by source**")
    df = marketing.weekly_orders(data)
    if not df.empty:
        lines = alt.Chart(df).mark_line(point=True).encode(
            x=alt.X("week:T", title=None, axis=alt.Axis(format="%d %b")), y=alt.Y("orders:Q", title="Orders"),
            color=alt.Color("source:N", title=None),
            tooltip=[alt.Tooltip("week:T", title="Week of", format="%d %b %Y"), alt.Tooltip("source:N", title="Source"),
                     alt.Tooltip("orders:Q", title="Orders", format=",.0f")])
        page_notes = views_sales._page_notes(data, ["Website"])
        marks = views_sales.note_marks(page_notes)
        st.altair_chart((lines + marks if marks is not None else lines).properties(height=300))
        views_sales.notes_list(page_notes)

    st.markdown("**By source**")
    st.dataframe(marketing.by_source(data), hide_index=True,
                 column_config={"Visits": NUM, "Orders": NUM, "Revenue": NUM, "Orders per 100 visits": RATE,
                                "Orders a year earlier": NUM, "Change %": PCT})

    st.markdown("**Landing pages**")
    st.caption("The page a visit started on. Click a product page to open its window.")
    pages, skus = marketing.by_page(data)
    product_card.clickable_table(pages, skus, key="mk_pages", hide_index=True,
                                 column_config={"Visits": NUM, "Orders": NUM, "Revenue": NUM,
                                                "Orders per 100 visits": RATE})
    views_sales.notes_editor("mk")


def page_google_analytics():
    st.markdown(f"<h1>{views.google_label('analytics')}</h1>", unsafe_allow_html=True)
    tab_traffic, tab_data = st.tabs(["Traffic sources", "Data and pulls"])
    with tab_traffic:
        traffic_sources()
    with tab_data:
        views.page_google_analytics(title=False)
