"""Catalogue > Change log, tab Made by hand: the "Add change by hand" form and the list of entries.

Changes made outside Houston (for example by hand in the Magento admin) are entered here once, so they count when
Houston judges whether changes worked. Storage and checks: hand_changes.py.
"""
import datetime

import pandas as pd
import streamlit as st

import hand_changes as hc

K = "hc_"                                                   # prefix of this tab's widget keys
SCOPE_OF = {label: key for key, label in hc.SCOPES.items()}


def _defaults():
    s = st.session_state
    s.setdefault(K + "day", datetime.date.today())
    s.setdefault(K + "types", [])
    s.setdefault(K + "scope", hc.SCOPES["product"])
    s.setdefault(K + "draft", "")
    return s


def _picked():
    """A draft was chosen: fill the form with it."""
    s = st.session_state
    d = next((d for d in hc.drafts() if d["id"] == s.get(K + "pick")), None)
    if not d:
        return
    s[K + "day"] = datetime.date.fromisoformat(d["day"])
    s[K + "types"] = list(d["types"])
    s[K + "scope"] = hc.SCOPES[d["scope"]]
    s[K + "skus"] = "\n".join(d.get("skus") or [])
    s[K + "what"] = d["what"]
    s[K + "pred"] = d.get("prediction", "")
    s[K + "draft"] = d["id"]


def _clear():
    s = st.session_state
    s[K + "day"] = datetime.date.today()
    s[K + "types"] = []
    for k in ("skus", "what", "pred"):
        s[K + k] = ""
    for k in ("one", "cat", "pick"):
        s[K + k] = None
    s[K + "draft"] = ""


def _chosen_products():
    """(skus, label, problem) for what the form says now."""
    s = st.session_state
    scope = SCOPE_OF.get(s.get(K + "scope"), "product")
    if scope == "product":
        return ([s[K + "one"]] if s.get(K + "one") else []), "", ""
    if scope == "list":
        found, not_found, unclear = hc.match_skus(s.get(K + "skus") or "", hc.catalogue_products())
        if not_found or unclear:
            return [], "", ("These SKUs aren't in the catalogue copy: " + ", ".join(not_found + unclear) +
                            ". Correct or remove them, then save again.")
        return [sku for _, sku, _ in found], "", ""
    if scope == "category":
        cid = s.get(K + "cat")
        if not cid:
            return [], "", ""
        skus = hc.category_products(cid)
        return skus, hc.category_label(cid), "" if skus else "That category has no products in the catalogue copy."
    return [], "", ""


def _save():
    s = st.session_state
    scope = SCOPE_OF.get(s.get(K + "scope"), "product")
    skus, label, problem = _chosen_products()
    if problem:
        s[K + "msg"] = ("error", problem)
        return
    try:
        hc.add(s.get(K + "day"), scope, s.get(K + "types") or [], s.get(K + "what") or "", s.get(K + "pred") or "",
               skus, label, s.get(K + "draft") or "")
    except ValueError as e:
        s[K + "msg"] = ("error", str(e))
        return
    who = "the whole site" if scope == "site" else f"{len(skus)} product{'' if len(skus) == 1 else 's'}"
    s[K + "msg"] = ("ok", f"Saved: {hc._as_date(s.get(K + 'day')):%d %b %Y}, {who}.")
    _clear()


def _delete(change_id):
    hc.delete(change_id)
    st.session_state[K + "open"] = None
    st.session_state[K + "msg_list"] = "Entry deleted."


def _show_match(text):
    if not (text or "").strip():
        return
    cat = hc.catalogue_products()
    found, not_found, unclear = hc.match_skus(text, cat)
    near = sum(1 for _, _, how in found if how != "exact")
    line = f"{len(found)} product{'' if len(found) == 1 else 's'} found"
    if near:
        line += f", {near} of them after ignoring leading zeros, spaces or upper and lower case"
    if not_found:
        line += ". Not in the catalogue copy: " + ", ".join(not_found)
    if unclear:
        line += ". More than one product fits: " + ", ".join(unclear)
    (st.warning if not_found or unclear else st.caption)(line + ".")
    if found:
        with st.expander("Show the products"):
            st.dataframe(pd.DataFrame([{"Typed": t, "SKU": sku, "Product": cat.get(sku, ""), "Match": how}
                                       for t, sku, how in found]), hide_index=True)


def _form():
    s = _defaults()
    drafts = hc.drafts()
    if drafts:
        labels = {d["id"]: d["label"] for d in drafts}
        st.selectbox("Start from a draft", list(labels), index=None, key=K + "pick", on_change=_picked,
                     format_func=lambda i: labels.get(i, i),
                     placeholder=f"{len(drafts)} drafts from the session logs, for changes made before Houston's "
                                 "change log began")
        todo = next((d.get("todo") for d in drafts if d["id"] == s.get(K + "draft")), "")
        if todo and not (s.get(K + "skus") or "").strip():
            st.info(todo)
    a, b = st.columns([1, 3])
    a.date_input("Date of the change", key=K + "day", max_value=datetime.date.today(), format="DD.MM.YYYY")
    b.multiselect("Type of change, one or more", hc.TYPES, key=K + "types")
    scope = SCOPE_OF[st.radio("Products", list(hc.SCOPES.values()), key=K + "scope", horizontal=True)]
    if scope == "product":
        cat = hc.catalogue_products()
        st.selectbox("Product", sorted(cat), index=None, key=K + "one", placeholder="Type a SKU or part of the name",
                     format_func=lambda sku: f"{sku} · {cat.get(sku, '')}")
    elif scope == "list":
        text = st.text_area("SKUs", key=K + "skus", height=110,
                            placeholder="Paste them from your Change log: one per line, or separated by commas or "
                                        "spaces")
        _show_match(text)
    elif scope == "category":
        cats = {cid: f"{label} ({n})" for cid, label, n in hc.categories()}
        st.selectbox("Category", list(cats), index=None, key=K + "cat", placeholder="Choose a category",
                     format_func=lambda cid: cats.get(cid, cid))
        st.caption("Houston keeps the products that are in the category today.")
    else:
        st.caption("A whole-site change shows on every product's charts.")
    st.text_area("What changed", key=K + "what", height=90)
    st.text_area("Prediction (optional): why, and what we expect by when", key=K + "pred", height=90)
    save, clear, _ = st.columns([1, 1, 4])
    save.button("Save", type="primary", key=K + "save", on_click=_save)
    clear.button("Clear the form", key=K + "clear", on_click=_clear)
    msg = s.pop(K + "msg", None)
    if msg:
        (st.success if msg[0] == "ok" else st.error)(msg[1])


def _who(r):
    if r.scope == "site":
        return "Whole site"
    if r.scope == "category":
        return f"{r.products} in {r.scope_label}"
    return str(r.products)


def _who_long(r):
    if r.scope == "site":
        return "whole site"
    n = f"{r.products} product{'' if r.products == 1 else 's'}"
    return f"{n} in {r.scope_label}" if r.scope == "category" else n


def _entries():
    st.subheader("Entries")
    done = st.session_state.pop(K + "msg_list", None)
    if done:
        st.success(done)
    df = hc.entries()
    if df.empty:
        st.caption("Nothing entered yet.")
        return
    rows = list(df.itertuples())
    st.dataframe(pd.DataFrame({"Date": [f"{hc._as_date(r.day):%d %b %Y}" for r in rows],
                               "Products": [_who(r) for r in rows], "Type": df["types"],
                               "What changed": df["what"], "Prediction": df["prediction"]}), hide_index=True)
    label = {int(r.id): f"{hc._as_date(r.day):%d %b %Y}, {_who_long(r)}: {r.what[:70]}" for r in rows}
    pick = st.selectbox("Open an entry", list(label), index=None, key=K + "open",
                        format_func=lambda i: label.get(i, str(i)),
                        placeholder="Choose one to see its products or delete it")
    if pick is None:
        return
    products = hc.products_of(pick)
    if not products.empty:
        st.dataframe(products, hide_index=True)
    sure = st.checkbox("Yes, delete this entry", key=f"{K}sure_{pick}")
    st.button("Delete", key=f"{K}del_{pick}", disabled=not sure, on_click=_delete, args=(pick,))


def hand_tab():
    st.caption("For changes made outside Houston, for example by hand in the Magento admin. Entered once here, "
               "they count when Houston judges whether changes worked. Houston's own pushes are in the first tab.")
    st.subheader("Add a change made by hand")
    _form()
    st.divider()
    _entries()
