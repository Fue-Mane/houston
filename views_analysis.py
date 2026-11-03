"""Product Analysis (build plan step 5, Phase B): the section's two pages.

- Overview: every live product on the way from a Google search to a sale on the website, for a window after the
  Search Console gap, then the searches that rose against the same dates a year earlier, and question searches.
  A click on a product opens its window, which has an "Open product page" button.
- Product page: one product. Name, meta title and meta description are edited in place, into Review and push with
  a prediction, using the same widgets and checks as Catalogue > Products. "Edit in Catalogue" opens the product
  there for everything else. The product window's parts are reused as they are.
The blocks listed under Features in the brief come in Phases C and D.
"""
import streamlit as st

import analysis
import core
import health
import product_card
import views

OVERVIEW = {"title": "Overview", "url_path": "product-analysis"}
PRODUCT = {"title": "Product page", "url_path": "product-page"}
GOOGLE_FIELDS = ["name", "meta_title", "meta_description"]
PARTS = ["Sales and stock", "Google", "Data checks"]


def pages():
    return [st.Page(page_overview, **OVERVIEW), st.Page(page_product, **PRODUCT)]


def open_product_page(sku):
    """From anywhere (the product window's button): the product page for this product."""
    st.switch_page(st.Page(page_product, **PRODUCT), query_params={"sku": sku})


# --- Overview ------------------------------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=900, max_entries=8)
def _products(prop, start, end, version):
    return analysis.products(prop, start, end)


@st.cache_data(show_spinner=False, ttl=900, max_entries=8)
def _searches(prop, start, end, version):
    return analysis.searches(prop, start, end)


def _version():
    """Changes whenever new Search Console data, a catalogue load or a pre-flight run arrives."""
    with views.closing_conn() as c:
        a = c.execute("SELECT MAX(pulled_at) FROM gsc_pulls").fetchone()[0]
        b = c.execute("SELECT value FROM meta WHERE key = 'loaded_at'").fetchone()
        try:
            h = c.execute("SELECT MAX(run_at) FROM health_runs").fetchone()[0]
        except Exception as e:                             # noqa: BLE001  (no pre-flight run yet)
            if not core.missing_table(e):
                raise
            h = ""
    return f"{a}|{(b or [''])[0]}|{h}"


def _pick(label, options, key):
    try:
        got = st.segmented_control(label, options, default=options[0], key=key)
    except AttributeError:                                 # a Streamlit without segmented_control
        got = st.radio(label, options, horizontal=True, key=key)
    return got if got in options else options[0]


def _search_table(df, key, extra_cols, config):
    cols = ["query", "impressions"] + extra_cols + ["position", "clicks", "product", "page"]
    show = df[cols].rename(columns={"query": "Search", "impressions": "Impressions", "position": "Position",
                                    "clicks": "Clicks", "product": "Product", "page": "Page"})
    product_card.clickable_table(show, list(df["sku"]), key=key, hide_index=True, column_config={
        "Impressions": st.column_config.NumberColumn(format="%,.0f"), "Clicks": st.column_config.NumberColumn(format="%,.0f"),
        "Position": st.column_config.NumberColumn(format="%.1f"), **config})


def page_overview():
    st.header("Product Analysis")
    views.need_cache()
    prop = analysis.default_property()
    if not prop:
        st.markdown(f"Connect {views.google_label('search_console')} first: Marketing → "
                    f"{views.google_label('search_console')}.", unsafe_allow_html=True)
        return
    label = _pick("Window", analysis.WINDOWS, "pa_window")
    start, end = analysis.window(prop, label)
    version = _version()
    st.caption(f"{start:%d %b} to {end:%d %b %Y}, the latest day Houston holds. Search Console has nothing for "
               "23 Jun – 8 Sep 2026, so no window starts before 9 Sep.")

    st.subheader("Products")
    df, months = _products(prop, start, end, version)
    if df.empty:
        st.info("No live products in the catalogue copy. Load the catalogue first.")
        return
    shown = df[["SKU", "Product", "Impressions", "Clicks", "CTR %", "Avg. position", "Top search", "Website units",
                "Stock", "In stock online"]]
    product_card.clickable_table(shown, list(df["sku"]), key="pa_products", hide_index=True, column_config={
        "Impressions": st.column_config.NumberColumn(format="%,.0f"), "Clicks": st.column_config.NumberColumn(format="%,.0f"),
        "CTR %": st.column_config.NumberColumn(format="%.1f"), "Avg. position": st.column_config.NumberColumn(format="%.1f"),
        "Website units": st.column_config.NumberColumn(f"Website units, {analysis.months_label(months)}", format="%,.0f"),
        "Stock": st.column_config.NumberColumn(format="%,.0f")})
    packs = int(df["Pack"].sum())
    st.caption(f"Shown and clicked: {views.google_label('search_console')}, {start:%d %b} to {end:%d %b}. "
               f"Website units: WinMENTOR, website invoices, {analysis.months_label(months)}. Stock: Depozit central "
               "+ VANZARI EN-GROS. " + (f"For the {packs} packs (SKU/N), units and stock are WinMENTOR's singles. "
                                        if packs else "") +
               "Views and add to cart, from Google Analytics for Romanian visitors only, join in Phase C. "
               "A click on a product opens its window.", unsafe_allow_html=True)

    queries = _searches(prop, start, end, version)
    a0, b0 = analysis.year_earlier(start), analysis.year_earlier(end)
    st.subheader("Searches that rose against a year earlier")
    up = analysis.rising(queries)
    if up.empty:
        st.caption("No search rose in this window.")
    else:
        _search_table(up, "pa_rising", ["impressions_before", "change", "position_before"], {
            "impressions_before": st.column_config.NumberColumn(f"Impressions {a0:%d %b}–{b0:%d %b %Y}", format="%,.0f"),
            "change": st.column_config.NumberColumn("Change", format="%+,.0f"),
            "position_before": st.column_config.NumberColumn("Position a year earlier", format="%.1f")})
    st.caption(f"Impressions are not searches: they come close only when we rank near the top. Searches with "
               f"„biomania” are left out; a search needs {analysis.MIN_RISING} impressions in this window. "
               "Product and Page: the page Google showed most for it." +
               (" The year-earlier dates include 10–14 Sep 2025, when, by third-party reports, Google stopped "
                "serving 100 results per page and Search Console lost many bot impressions, so the earlier figures "
                "are inflated and rises look smaller." if analysis.touches_num100(start, end) else ""))

    st.subheader("Question searches")
    asked = analysis.questions(queries)
    if asked.empty:
        st.caption("No question searches in this window.")
    else:
        _search_table(asked, "pa_questions", ["impressions_before"], {
            "impressions_before": st.column_config.NumberColumn(f"Impressions {a0:%d %b}–{b0:%d %b %Y}",
                                                                format="%,.0f")})
    st.caption("A question search starts with " + ", ".join(f"„{w}”" for w in analysis.QUESTION_FIRST) +
               ", or contains " + ", ".join(f"„{w}”" for w in analysis.QUESTION_WORDS) +
               f"; it needs {analysis.MIN_QUESTION} impressions. Ideas for posts and for the creator. Google's own "
               "weekly search counts join in Phase D.")


# --- Product page --------------------------------------------------------------------------------------------
def _follow_link(live):
    """A link to the page (?sku=...) chooses the product, once per new link."""
    link = st.query_params.get("sku")
    if link == st.session_state.get("pa_last_link"):
        return
    st.session_state["pa_last_link"] = link
    want = link if link in live else next((s for s in live if s.strip() == (link or "").strip()), None)
    if want:
        st.session_state["pa_sku"] = want


def edit_box(sku, rec, attrs):
    """Name, meta title and meta description into Review and push: the same widgets, diff and checks as
    Catalogue > Products, plus the prediction line."""
    codes = [k for k in GOOGLE_FIELDS if k in attrs]
    values = {k: views.attr_widget(attrs[k], rec.get(k, ""), f"pa_e_{sku}_{k}") for k in codes}
    prediction = st.text_input("Prediction (optional): why, and what we expect by when", key=f"pa_e_{sku}_pred")
    if not st.button("Add to review", type="primary", key=f"pa_e_{sku}_go"):
        return
    changes = core.product_diff(sku, rec, values, attrs)
    if not changes:
        st.info("Nothing changed.")
        return
    for ch in changes:
        if prediction.strip():
            ch["prediction"] = prediction.strip()
    core.queue(changes)
    flagged = [f"{attrs.get(ch['field'], {}).get('label') or ch['field']}: {note}"
               for ch in changes for status, note in [core.check(ch, attrs)] if status != "Passed"]
    st.success(f"{len(changes)} change{'' if len(changes) == 1 else 's'} added to Review and push." +
               (" Check before pushing: " + "; ".join(flagged) + "." if flagged else ""))


def _name_and_meta(sku, rec, attrs):
    left, right = st.columns([5, 1])
    with left:
        name = rec.get("name") or ""
        title = rec.get("meta_title") or ""
        desc = rec.get("meta_description") or ""
        st.markdown("\n".join([
            f"- Name: **{name}**" if name else "- Name: empty",
            f"- Meta title: {title}" if title else "- Meta title: empty, so the page title is the name",
            f"- Meta description: {desc} ({len(desc)} characters)" if desc else
            "- Meta description: empty, so Google writes its own snippet"]))
    with right:
        with st.popover("Edit"):
            edit_box(sku, rec, attrs)
        if st.button("Edit in Catalogue", key=f"pa_cat_{sku}"):
            st.session_state["open_in_catalogue"] = sku
            st.switch_page(st.Page(views.page_products, title="Products"))
    waiting = [p for p in core.load_pending() if p.get("target") == sku]
    if waiting:
        st.caption(f"{len(waiting)} change{'' if len(waiting) == 1 else 's'} for this product "
                   f"{'waits' if len(waiting) == 1 else 'wait'} in Review and push.")


def page_product():
    c = views.need_cache()
    prods, attrs = c["products"], c["attributes"]
    live = {s: r for s, r in prods.items() if "_error" not in r}
    _follow_link(live)
    st.header("Product page")
    order = sorted(live, key=lambda s: ((live[s].get("name") or "").lower(), s))
    sku = st.selectbox("Product", order, index=None, key="pa_sku", placeholder="Type a SKU or part of the name",
                       format_func=lambda s: f"{s.strip()} · {live[s].get('name', '')}")
    if not sku:
        st.info("Choose a product, or open one from the Overview: a click on a product opens its window, and the "
                "window has an Open product page button.")
        return
    if st.query_params.get("sku") != sku:                  # the address bar follows the choice, so it can be kept
        st.query_params["sku"] = sku
        st.session_state["pa_last_link"] = sku
    rec = live[sku]
    r = health.product(sku.strip()) or {}
    found = health.product_issues(sku.strip())
    st.subheader(rec.get("name") or sku)
    ean = r.get("ean") or ""
    path = (rec.get("url_path") or (f"{rec['url_key']}.html" if rec.get("url_key") else "")).strip("/")
    st.caption(f"SKU {sku.strip()}" + (f" · EAN {ean}" if ean else " · no valid EAN") +
               (f" · [biomania.ro/{path}](https://www.biomania.ro/{path})" if path else ""))
    _name_and_meta(sku, rec, attrs)
    labels = PARTS[:2] + [f"{PARTS[2]} ({len(found)})" if found else PARTS[2]]
    part = product_card._pick_part(f"pa_part_{sku}", labels)
    if part == 0:
        product_card._sales(r, rec, sku)
    elif part == 1:
        product_card._google(r, rec, sku, title=False)
    else:
        product_card._checks(r, rec, sku, found, title=False)
    st.caption("Still to come on this page (Phases C and D): the chain week by week, eMAG Marketplace against the "
               "website, bought together, seasons, comparisons with resellers and competitors, the rules check and "
               "Google's view, trends, and whether our changes worked.")
