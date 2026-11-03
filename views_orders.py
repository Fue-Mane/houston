"""Stock and orders pages: Orders (suppliers' order forms) and WinMENTOR (loading the courier's files)."""
import datetime
import json

import pandas as pd
import streamlit as st

import google_auth
import health
import orders
import winmentor as wm


def _signin_or_stop():
    state = wm.status_signin()
    if state == "no_client":
        st.warning("Save the file downloaded from Google Cloud as `secrets/client_secret.json` in the Houston folder.")
        st.stop()
    if state in ("not_signed_in", "needs_permission"):
        st.info("Houston needs read access to the WinMENTOR bucket. Sign in as nicoleta@biomania.ro: a browser tab "
                "opens; allow access (Search Console, Analytics and Cloud Storage, all read-only), then come back here.")
        if st.button("Sign in with Google", type="primary", key="wm_signin"):
            try:
                google_auth.sign_in()
                st.rerun()
            except Exception as e:
                st.error(f"Sign-in didn't complete: {e}")
        st.stop()


def _update(key):
    if st.button("Update from the bucket", type="primary", key=key):
        bar = st.progress(0.0, "Looking in the bucket")
        try:
            res = wm.update(progress=lambda f, t: bar.progress(min(float(f), 1.0), t))
        except wm.GoogleError as e:
            st.error(str(e))
            return
        msg = f"{res['loaded']} new files loaded ({res['in_bucket']} in the bucket)."
        h = health.rebuild_quietly("WinMENTOR update")
        if h:
            msg += f" Data health: {h['issues']} problems on {h['products']} website products."
        if res["revised"]:
            msg += f" {res['revised']} months changed since their last copy."
        st.success(msg)
        if res["not_read"]:
            st.warning(f"{res['not_read']} lists in those files weren't read by the courier (for example sales while "
                       "WinMENTOR was open). Houston kept the previous copy of each.")
        for err in res["errors"]:
            st.error(err)


def _age_warning(run_at):
    if not run_at:
        return
    when = datetime.datetime.fromisoformat(run_at[:19])
    days = (datetime.date.today() - when.date()).days
    working = sum(1 for i in range(1, days + 1)
                  if (when.date() + datetime.timedelta(days=i)).weekday() < 5)
    if working > 1:
        st.warning(f"The newest delivery is from {when:%d %b %Y %H:%M}, {working} working days ago. "
                   "Check the courier on the office PC (C:\\ProgramData\\Houston\\export.log).")


# --- WinMENTOR page -----------------------------------------------------------------------
def page_winmentor():
    st.header("WinMENTOR")
    st.caption("Stock, sales and lists from WinMENTOR, delivered by the courier on the office PC to the bucket "
               f"`{wm.bucket()}`. Houston only reads the bucket; it never writes to it or to WinMENTOR.")
    _signin_or_stop()
    s = wm.status()
    last = s["last_delivery"]
    if last:
        st.markdown(f"**Newest delivery:** {last['run_at'][:16].replace('T', ' ')} · database {last['company']}")
        _age_warning(last["run_at"])
        if last["errors"]:
            st.warning("In that delivery the courier couldn't read: " + ", ".join(last["errors"]) +
                       ". The previous copy of each is kept.")
    else:
        st.info("Nothing loaded yet. The first update loads the whole history: about 210 files, a few minutes.")
    _update("wm_update")
    if s["failed"]:
        with st.expander(f"Files that failed to load ({len(s['failed'])}): they are tried again on the next update"):
            st.dataframe(pd.DataFrame(s["failed"], columns=["file", "when", "error"]), hide_index=True)
    cov = wm.coverage()
    st.subheader("What's loaded")
    st.dataframe(pd.DataFrame(cov["lists"]), hide_index=True, column_config={
        "list": "List", "rows": st.column_config.NumberColumn("Rows"), "databases": "Databases",
        "first": "First period", "last": "Last period"})
    st.caption("Monthly lists: one copy per month, the newest. Stock lists: one copy per day. "
               "Other lists: only the newest copy.")
    if cov["codes"]:
        un = dict(cov["unmatched_sales_lines"])
        st.markdown("**Codes**: " + " · ".join(f"{comp}: {n:,} products with an EAN" + (f", {un[comp]:,} sale lines "
                                                                                       "without one" if un.get(comp) else "")
                                               for comp, n in cov["codes"]))
    with st.expander(f"Months that changed after a later delivery ({len(cov['revisions'])})"):
        if cov["revisions"]:
            st.dataframe(pd.DataFrame(cov["revisions"], columns=[
                "list", "database", "month", "old copy", "new copy", "old lines", "new lines", "lines changed"]),
                hide_index=True)
        else:
            st.caption("None yet. This shows whether WinMENTOR months get corrected after they end.")
        st.caption("The current month isn't listed: it changes with every delivery.")
    if last and last["at_sign"]:
        st.caption("Customer records containing an '@', per column of the customer list: " +
                   ", ".join(f"{k}: {v}" for k, v in last["at_sign"].items()))
    with st.expander("Settings"):
        b = st.text_input("Bucket", wm.bucket(), key="wm_bucket_input")
        if st.button("Save", key="wm_bucket_save") and b.strip():
            wm.core.set_setting("wm_bucket", b.strip())
            st.success("Saved.")


# --- product window -------------------------------------------------------------------------
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@st.dialog("Product", width="large")
def product_dialog(e, months):
    import altair as alt
    d = wm.product_details(e)
    magento = wm.magento_names([e]).get(e, "")
    wm_name = next((n for comp, _, n, _ in d["codes"] if n and comp == d["company"]), "") or \
        (d["stock"]["name"] if d["stock"] else "")
    st.subheader(magento or wm_name or e)
    wm_sku = next((sku for comp, sku, _, _ in d["codes"] if comp == "BIO2025"), "")
    st.markdown(" · ".join(x for x in (
        f"**EAN:** `{e}`",
        f"**WinMENTOR name:** {wm_name}" if wm_name else "",
        f"**WinMENTOR SKU (BIO2025):** `{wm_sku}`" if wm_sku else
        "**WinMENTOR SKU (BIO2025):** not found",
    ) if x))

    series = wm.monthly_series(e)
    per = dict(series)
    def total(ms):
        return float(sum(per.get(m, 0.0) for m in ms))
    today = wm.today()
    y_now, y_prev = wm.ytd(e, today)
    last_year = [wm.month_add(m, -12) for m in months]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Stock now", f"{d['stock']['total']:,.0f}" if d["stock"] else "—",
              help="Depozit central + VANZARI EN-GROS, newest stock photo" + (f" ({d['stock_day']})" if d["stock_day"] else ""))
    m2.metric(f"Sold per month, last {len(months)}", f"{total(months) / len(months):,.1f}")
    m3.metric("Same months last year", f"{total(last_year) / len(months):,.1f}")
    m4.metric(f"Sold this year to {today:%d %b}", f"{y_now:,.0f}",
              delta=f"{(y_now - y_prev) / y_prev:+.0%} vs {today.year - 1} to {today:%d %b}" if y_prev else None,
              help=f"Same period last year: {y_prev:,.0f}")

    if not series:
        st.info("No sales found for this EAN in WinMENTOR.")
    else:
        import channel_units
        st.markdown("**Units sold per month**")
        channel_units.chart(e, series)                   # All sales, plus one line per sales channel

        years = sorted({m[:4] for m, _ in series}, reverse=True)
        st.markdown("**Year against year, month by month**")
        a, b = st.columns(2)
        ya = a.selectbox("Year", years, index=0, key=f"ya_{e}")
        yb = b.selectbox("Compared with", years, index=1 if len(years) > 1 else 0, key=f"yb_{e}")
        comp = pd.DataFrame([{"month": MONTH_NAMES[i], "year": y, "qty": per.get(f"{y}_{i + 1:02d}", 0.0)}
                             for y in (ya, yb) for i in range(12)])
        bars = alt.Chart(comp).mark_line(point=True).encode(
            x=alt.X("month:N", sort=MONTH_NAMES, title=None),
            y=alt.Y("qty:Q", title="Units sold"),
            color=alt.Color("year:N", title="Year", sort=[ya, yb]),
            tooltip=[alt.Tooltip("year:N", title="Year"), alt.Tooltip("month:N", title="Month"),
                     alt.Tooltip("qty:Q", title="Units sold", format=",.0f")],
        ).properties(height=280)
        st.altair_chart(bars)

    if d["lots"]:
        st.markdown("**Stock lines with dates**")
        st.dataframe(pd.DataFrame([(w, wm.num(q), dt, sup) for w, q, dt, sup in d["lots"]],
                                  columns=["warehouse", "qty", "date (probably expiry, unconfirmed)", "supplier"]),
                     hide_index=True)
    st.caption("Google Ads figures and sales forecasts will be added here.")


# --- Orders page ------------------------------------------------------------------------------
def _links_box(form, lay, sugg, links, code_map, products, names):
    """Rows whose EAN isn't in WinMENTOR (and rows already linked): link each to the WinMENTOR product by hand."""
    idle = [r for r in sugg if not r["skip"] and not r["stock"] and not any(r["sold"])]
    todo = [r for r in sugg if not r["found"] or r["ean"] in links or r in idle]
    if not todo:
        return
    hint = orders.hint_column(form, lay, code_map)
    ws = form.wb[lay["sheet"]]
    options = [""] + sorted(products, key=lambda x: (names.get(x) or products[x][0]).lower())

    def fmt(x):
        if not x:
            return "(no link)"
        return f"{x} · {names.get(x) or products[x][0]}"

    with st.expander(f"Products the supplier lists under another EAN ({len(todo)})", expanded=False):
        st.caption("WinMENTOR either has no product with these EANs, or has them with no stock and no sales, which "
                   "usually means it sells the product under another EAN. If so, pick that product here once. "
                   "Factor: how many WinMENTOR units make one of the supplier's pieces (1 if the same). "
                   + ("Proposals come from the supplier's copy of our code (column "
                      f"{orders.letter(hint)}); check them before saving." if hint else ""))
        with st.form("o_links"):
            chosen = {}
            for r in todo:
                cur, f = links.get(r["ean"], ("", 1.0))
                proposal = code_map.get(orders._code(ws.cell(r["row"], hint).value)) if hint else None
                default = cur or (proposal if proposal in products else "")
                a, b = st.columns([4, 1])
                pick = a.selectbox(f"Row {r['row']} · {r['name']} · {r['ean']}", options,
                                   index=options.index(default) if default in options else 0, format_func=fmt,
                                   key=f"lk_{r['ean']}_{r['row']}")
                fac = b.number_input("Factor", min_value=0.01, value=float(f or 1.0), step=1.0, key=f"lf_{r['ean']}_{r['row']}")
                chosen[r["ean"]] = (pick, fac)
            if st.form_submit_button("Save links"):
                for sup, (pick, fac) in chosen.items():
                    wm.save_ean_link(sup, pick, fac)
                st.rerun()


def page_orders():
    st.header("Orders")
    s = wm.status()
    if not s["last_delivery"]:
        st.info("No WinMENTOR data yet. Open **WinMENTOR** in the menu and press Update from the bucket.")
        return
    last = s["last_delivery"]
    day, stock, company = wm.stock_by_ean()
    st.caption(f"WinMENTOR data from {last['run_at'][:16].replace('T', ' ')} ({company}). Stock = Depozit central + "
               "VANZARI EN-GROS. Suggested = average monthly sales × 3 − stock, rounded up to whole cartons.")
    _age_warning(last["run_at"])

    up = st.file_uploader("The supplier's order form (Excel)", type=["xlsx"], key="o_file",
                          help="Use the supplier's newest file. Houston reads it by EAN and fills in only the "
                               "order column.")
    if not up:
        return
    try:
        form = orders.Form(up.getvalue(), up.name)
        saved = wm.saved_layouts()
        supplier, lay = orders.recognise(form, saved)
        lay = dict(lay) if lay else orders.detect(form)
    except (ValueError, RuntimeError) as e:
        st.error(str(e))
        return
    ws = form.wb[lay["sheet"]]
    heads = orders.headers(ws, lay["header_row"])

    def label(col):
        return f"{orders.letter(col)} · {heads.get(col, '(no header)')}" if col else "(none)"

    cols = sorted({c.column for row in ws.iter_rows(max_row=min(ws.max_row, 200)) for c in row if c.value is not None}
                  | {lay[k] for k in ("ean_col", "name_col", "ppc_col", "order_col", "price_col") if lay.get(k)})
    with st.expander(f"File layout · {'recognised: ' + supplier if supplier else 'new layout, please check'}",
                     expanded=not supplier):
        st.caption(f"Sheet {lay['sheet']}, headers in row {lay['header_row']}. Houston found the EAN column itself; "
                   "check the others, then save so this supplier's file is recognised next time.")
        a, b, c3 = st.columns(3)
        lay["ean_col"] = a.selectbox("EAN", cols, index=cols.index(lay["ean_col"]), format_func=label, key="o_ean")
        lay["name_col"] = b.selectbox("Product name", [None] + cols, index=([None] + cols).index(lay.get("name_col")),
                                      format_func=label, key="o_name")
        lay["ppc_col"] = c3.selectbox("Pieces per carton", [None] + cols,
                                      index=([None] + cols).index(lay.get("ppc_col")), format_func=label, key="o_ppc")
        a, b, c3 = st.columns(3)
        lay["order_col"] = a.selectbox("Order goes in", [None] + cols,
                                       index=([None] + cols).index(lay.get("order_col")), format_func=label, key="o_ord")
        lay["order_unit"] = b.radio("Order is in", ["cartons", "pieces"],
                                    index=0 if lay.get("order_unit") == "cartons" else 1, horizontal=True, key="o_unit")
        lay["price_col"] = c3.selectbox("Price per piece (optional)", [None] + cols,
                                        index=([None] + cols).index(lay.get("price_col")), format_func=label, key="o_pr")
        name = st.text_input("Supplier", supplier or up.name.rsplit(".", 1)[0], key="o_supplier")
        if st.button("Save layout for this supplier", key="o_save"):
            wm.save_layout(name.strip(), orders.signature(form, lay), json.loads(orders.layout_json(lay)))
            st.success(f"Saved. Files from {name.strip()} with these headers are recognised from now on.")
    if not lay.get("order_col"):
        st.warning("Choose the column the order goes in (File layout above).")
        return

    a, b = st.columns([1, 2])
    n = a.number_input("Average over the last … full months", 1, 12, 3, key="o_n")
    hide = b.checkbox("Hide products not in WinMENTOR", key="o_hide")
    months = wm.full_months_before(wm.today(), int(n))
    rows = orders.rows_of(form, lay)
    links = wm.ean_links()
    products = wm.winmentor_products(company)
    code_map = wm.codes_to_eans(company)
    eff = [links.get(r["ean"], (r["ean"], 1.0))[0] for r in rows]
    sales, missing = wm.window_sales(eff, months)
    if missing:
        st.warning("No sales list loaded yet for " + ", ".join(wm.month_label(m) for m in missing) +
                   ": those months count as 0. Update on the WinMENTOR page.")
    sugg = orders.suggest(rows, stock, sales, months, known=set(products), links=links)
    names = wm.magento_names(list(products) + [r["wm_ean"] for r in sugg])
    _links_box(form, lay, sugg, links, code_map, products, names)
    if any(r["in_file"] for r in rows):
        st.caption("The file already had quantities in the order column (a previous order). Houston replaces them.")

    unit = lay["order_unit"]
    mcols = [f"Sold {wm.month_label(m)}" for m in months]
    shown = [r for r in sugg if r["found"] or not hide]
    df = pd.DataFrame([{"open": False, "row": r["row"], "ean": r["ean"], "product": r["name"],
                        "magento": names.get(r["wm_ean"], ""), "stock": r["stock"], **dict(zip(mcols, r["sold"])),
                        "avg": round(r["avg"], 1), "pieces": r["pieces"], "ppc": r["ppc"], "suggested": r["cartons"],
                        "order": (r["cartons"] if unit == "cartons" else r["pieces"]) or 0, "note": r["note"]}
                       for r in shown],
                      columns=["open", "row", "ean", "product", "magento", "stock", *mcols, "avg", "pieces", "ppc",
                               "suggested", "order", "note"])
    config = {"open": st.column_config.CheckboxColumn("Open", help="Tick to open the product's page", width="small"),
              "row": st.column_config.NumberColumn("Row", disabled=True, width="small"),
              "ean": st.column_config.TextColumn("EAN", disabled=True),
              "product": st.column_config.TextColumn("Supplier's name", disabled=True),
              "magento": st.column_config.TextColumn("Magento name", disabled=True),
              "stock": st.column_config.NumberColumn("Stock now", disabled=True, format="%.0f"),
              "avg": st.column_config.NumberColumn("Avg sold / month", disabled=True),
              "pieces": st.column_config.NumberColumn("Suggested pieces", disabled=True),
              "ppc": st.column_config.NumberColumn("Pieces / carton", disabled=True, format="%.0f"),
              "suggested": st.column_config.NumberColumn("Suggested cartons", disabled=True),
              "order": st.column_config.NumberColumn(f"Order ({unit})", min_value=0, step=1, format="%d"),
              "note": st.column_config.TextColumn("Note", disabled=True)}
    for mc in mcols:
        config[mc] = st.column_config.NumberColumn(mc, disabled=True, format="%.0f")
    stamp = str(abs(hash((tuple(sorted(links.items())), hide))))[:8]
    key = f"o_table_{form.sha[:10]}_{orders.signature(form, lay)[:10]}_{n}_{unit}_{stamp}"
    edited = st.data_editor(df, column_config=config, hide_index=True, key=key)
    if hide and len(shown) < len(sugg):
        st.caption(f"{len(sugg) - len(shown)} products not in WinMENTOR are hidden; they get no order.")

    ticked = [w for w, o in zip([r["wm_ean"] for r in shown], edited["open"]) if o]
    seen = st.session_state.get("o_seen", [])
    fresh = [w for w in ticked if w not in seen]
    st.session_state.o_seen = ticked
    if fresh:
        product_dialog(fresh[0], months)

    qty = {int(r): int(q or 0) for r, q in zip(edited["row"], edited["order"])}
    ppc = {r["row"]: r["ppc"] for r in sugg}
    pieces = sum(q * (ppc[r] or 0) if unit == "cartons" else q for r, q in qty.items())
    lines = sum(1 for q in qty.values() if q)
    t1, t2, t3 = st.columns(3)
    t1.metric("Products ordered", lines)
    t2.metric("Cartons" if unit == "cartons" else "Pieces", f"{sum(qty.values()):,}")
    t3.metric("Pieces" if unit == "cartons" else "Cartons", f"{pieces:,.0f}" if unit == "cartons" else "—")
    if lay.get("price_col"):
        price = {r["row"]: r["price"] or 0 for r in rows}
        value = sum((q * (ppc[r] or 0) if unit == "cartons" else q) * price[r] for r, q in qty.items())
        st.caption(f"Value at the file's prices: {value:,.2f} (the file calculates its own total when opened).")

    data, replaced = orders.fill(form, lay, qty)
    out_name = orders.output_name(up.name)
    st.download_button(f"Download {out_name}", data, file_name=out_name, type="primary",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    note = "Only the order column changes; the supplier's formulas recalculate when the file is opened."
    if replaced:
        note += f" {replaced} cells linked to another workbook were given their current values."
    st.caption(note)
