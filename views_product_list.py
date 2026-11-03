"""Data > Product list: one row per product active on the website, joined with WinMENTOR (from the last pre-flight run).

Click any cell of a product to open its window. Switch on Edit in the table to change cells instead; every edit goes
to Review and push. The website EAN, the producer and the online stock flag are written to the store when pushed;
EAN in WinMENTOR and WinMENTOR name go to the operator's list (Houston never writes to WinMENTOR).
"""
import pandas as pd
import streamlit as st

import core
import health
import operator_requests as ops
import product_card
import views
import views_health
import winmentor as wm

SOLD = f"Sold, {health.SALES_MONTHS} months"
# (column, label, how an edit is queued: None = read-only, "website", "stock" or "winmentor")
COLS = [("name", "Website name", None), ("sku", "SKU", None), ("match", "Matched by", None),
        ("wm_code", "SKU in WinMENTOR", None), ("ean", "EAN", None), ("stock", "Stock", None), ("sold_recent", SOLD, None),
        ("online_in_stock", "In stock online", "stock"), ("online_price", "Online price", None),
        ("supplier", "Last delivered by", None), ("last_delivery", "Last delivery", None),
        ("producer", "Producer (website)", "website"), ("magento_ean", "EAN on the website", "website"),
        ("wm_ean", ops.EAN, "winmentor"), ("wm_name", ops.NAME, "winmentor"), ("problems", "Problems", None)]
WEBSITE_CODE = {"producer": "manufacturer", "magento_ean": "c2c_cod_ean"}
FILTER_CHECKS = [k for k in health.CHECKS if k["id"] not in ("unmatched_wm", "sale_no_channel")]   # the others are about website products
NUMBERS = {"stock": "%.0f", "sold_recent": "%.0f", "online_price": "%.2f", "problems": "%d"}


def page_product_list():
    st.header("Product list")
    st.caption("One row per product active on the website, joined with WinMENTOR's live database by SKU. Stock = "
               "Depozit central + VANZARI EN-GROS, newest stock photo. Sold = units sold at a price in this month and "
               f"the {health.SALES_MONTHS - 1} before it; samples at price 0 are left out. Click a product to open it.")
    run = views_health._fresh_run()
    rows = health.products()
    if not rows:
        st.info("The list is built by the pre-flight checks. Run them on Data health.")
        return

    found = {}
    for cid, sku, _, _ in health.issues():
        found.setdefault((sku or "").strip(), set()).add(cid)
    for r in rows:
        r["problems"] = len(found.get(r["sku"], ()))
        r["online_in_stock"] = bool(r["online_in_stock"])

    f1, f2, f3, f4 = st.columns([3, 3, 2, 2])
    q = f1.text_input("Search name, SKU or EAN", key="pl_q").strip().lower()
    titles = {k["title"]: k["id"] for k in FILTER_CHECKS}
    picked = f2.multiselect("Problems", list(titles), key="pl_pr", placeholder="Any")
    suppliers = sorted({r["supplier"] for r in rows if r["supplier"]})
    sup = f3.selectbox("Last delivered by", ["Any"] + suppliers + ["None found"], key="pl_sup")
    stock = f4.selectbox("Warehouse stock", ["Any", "In stock", "None"], key="pl_stk")

    def keep(r):
        if q and q not in " ".join(str(r.get(c) or "") for c in ("name", "sku", "wm_code", "ean", "magento_ean",
                                                                   "wm_ean")).lower():
            return False
        if picked and not {titles[p] for p in picked} <= found.get(r["sku"], set()):
            return False
        if sup == "None found" and r["supplier"] or sup not in ("Any", "None found") and r["supplier"] != sup:
            return False
        if stock == "In stock" and not (r["stock"] or 0) > 0 or stock == "None" and (r["stock"] or 0) > 0:
            return False
        return True
    shown = [r for r in rows if keep(r)]

    a, b = st.columns([3, 1])
    a.caption(f"{len(shown)} of {len(rows)} products · built {views_health._when(run)}" if run else
              f"{len(shown)} of {len(rows)} products")
    edit = b.toggle("Edit in the table", key="pl_edit",
                    help="On: change cells, then Add table edits to review. Off: click a product to open it.")
    df = pd.DataFrame(shown, columns=[c for c, _, _ in COLS])
    config = {c: (st.column_config.NumberColumn(label, format=NUMBERS[c]) if c in NUMBERS else
                  st.column_config.CheckboxColumn(label) if c == "online_in_stock" else
                  st.column_config.TextColumn(label)) for c, label, _ in COLS}

    if not edit:
        product_card.clickable_table(df, list(df["sku"]), "pl_view", hide_index=True, column_config=config)
        return

    cache = views.cache()
    attrs, prods = (cache["attributes"], cache["products"]) if cache else ({}, {})
    makers = [o["label"] for o in (attrs.get("manufacturer") or {}).get("options") or []]
    if makers:
        config["producer"] = st.column_config.SelectboxColumn("Producer (website)", options=[""] + makers)
    editable = {c for c, _, how in COLS if how == "winmentor" or how in ("website", "stock") and cache}
    if not cache:
        st.caption("Load the catalogue (Catalogue > Load) to edit the website columns. The WinMENTOR columns can be "
                   "edited now.")
    edited = st.data_editor(df, hide_index=True, column_config=config, key="pl_edit_" + str(abs(hash(tuple(df["sku"])))),
                            disabled=[c for c, _, _ in COLS if c not in editable])
    if not st.button("Add table edits to review", type="primary"):
        return
    by_sku = {r["sku"]: r for r in shown}
    changes, problems = [], []
    for (_, before), (_, after) in zip(df.iterrows(), edited.iterrows()):
        r = by_sku[before["sku"]]
        raw, name = r["raw_sku"] or r["sku"], r["name"]
        for col, label, how in COLS:
            if how is None or core.norm(after[col]) == core.norm(before[col]):
                continue
            new = core.norm(after[col]).strip()
            if how == "stock":
                changes.append(core.new_change("stock", raw, "is_in_stock", "1" if before[col] else "0",
                                               "1" if after[col] else "0", name))
            elif how == "website":
                code, rec = WEBSITE_CODE[col], prods.get(raw)
                if not rec:
                    problems.append(f"{raw}: not in the catalogue copy, so it was skipped")
                    continue
                if code == "manufacturer":
                    back = {o["label"]: o["value"] for o in attrs["manufacturer"].get("options") or []}
                    if new and new not in back:
                        problems.append(f"{raw}: {new!r} isn't a producer option, so it was skipped")
                        continue
                    new = back.get(new, "")
                elif new and not wm.ean(new):
                    problems.append(f"{raw}: {new!r} isn't a valid EAN (8, 12 or 13 digits and a correct last digit)")
                    continue
                changes += core.product_diff(raw, rec, {code: new}, attrs)
            elif not r["wm_code"]:
                problems.append(f"{raw}: not in WinMENTOR, so there's nothing there to change")
            elif col == "wm_ean" and not wm.ean(new):
                problems.append(f"{raw}: {new!r} isn't a valid EAN (8, 12 or 13 digits and a correct last digit)")
            else:
                changes.append(core.new_change("winmentor", r["wm_code"], label, core.norm(before[col]), new, name))
    for p in problems:
        st.error(p)
    st.success(f"{core.queue(changes)} changes added to Review and push.") if changes else st.info("No edits found.")
