"""Data health: the pre-flight checklist on the data, with who fixes each problem, and the operator's list."""
import datetime
import html
from collections import defaultdict

import pandas as pd
import streamlit as st

import core
import health
import operator_requests as ops

SOURCE_WORDS = {"Run pre-flight checks": "run by hand", "Run the checks now": "run by hand",
                "WinMENTOR update": "after a WinMENTOR update", "Catalogue load": "after a catalogue load",
                "new data arrived": "after new data arrived"}


def _when(run):
    """'22 Oct 2026, 20:25 (run by hand)' for a run."""
    try:
        at = datetime.datetime.strptime(run["run_at"][:19], "%Y-%m-%d %H:%M:%S").strftime("%d %b %Y, %H:%M")
    except ValueError:
        at = run["run_at"][:16]
    return f"{at} ({SOURCE_WORDS.get(run['source'], run['source'])})"


# --- running the checks ---------------------------------------------------------------------
def _run_button(key):
    if st.button("Run pre-flight checks", key=key, type="primary"):
        with st.spinner("Running pre-flight checks"):
            r = health.rebuild_quietly("Run pre-flight checks")
        if r:
            st.session_state["health_ran"] = (f"Pre-flight checks done: {r['issues']} problems on "
                                              f"{r['products']} website products.")
        st.rerun()


def _fresh_run():
    """The latest run, after running the checks again if new data arrived since (catalogue or WinMENTOR)."""
    run = health.last_run()
    newest = health.newest_data()
    if newest and (not run or newest > run["run_at"]):
        with st.spinner("New data since the last checks: running pre-flight checks"):
            health.rebuild_quietly("new data arrived")
        run = health.last_run()
    if st.session_state.get("health_ran"):
        st.success(st.session_state.pop("health_ran"))
    err = health.last_error()
    if err:
        st.error(f"The last pre-flight run failed ({err}). Press Run pre-flight checks; if it fails again, "
                 "copy this message into the build chat.")
    return run


# --- the pre-flight checklist ------------------------------------------------------------------
PALETTE = {
    "dark": {"card": "#151821", "border": "#31333f", "text": "#fafafa", "muted": "#9aa0a6", "leader": "#4a4d57",
             "green": "#2fb36b", "red": "#e5484d"},
    "light": {"card": "#f7f8fa", "border": "#e1e4e8", "text": "#1f2328", "muted": "#6b7280", "leader": "#c3c8cf",
              "green": "#1f9d57", "red": "#d93036"},
}


def current_theme():
    return "light" if (core.setting("theme") or "dark") == "light" else "dark"


def _checklist_html(run, counts, by):
    """The checklist. Each check with problems is a row that opens to show its products."""
    c = PALETTE[current_theme()]
    checks = health.CHECKS
    cleared = sum(1 for k in checks if not counts.get(k["id"]))
    bar = "".join(f'<i style="background:{c["green"] if not counts.get(k["id"]) else c["red"]}"></i>' for k in checks)
    rows = []
    for g in health.GROUPS:
        rows.append(f'<div class="pf-sec">{html.escape(g)}</div>')
        for k in (x for x in checks if x["group"] == g):
            n = counts.get(k["id"], 0)
            color = c["green"] if not n else c["red"]
            line = (f'<span class="pf-led" style="background:{color};box-shadow:0 0 5px {color}"></span>'
                    f'<span class="pf-name">{html.escape(k["title"])}</span><span class="pf-lead"></span>'
                    f'<span class="pf-val" style="color:{color}">{"OK" if not n else f"{n:,}"}</span>')
            tip = html.escape(k["how"], quote=True)
            if not n:
                rows.append(f'<div class="pf-row" title="{tip}">{line}<span class="pf-chev"></span></div>')
                continue
            items = "".join(f"<tr><td>{html.escape(str(s))}</td><td>{html.escape(str(pn))}</td>"
                            f"<td>{html.escape(str(d))}</td></tr>" for s, pn, d in by.get(k["id"], []))
            rows.append(f'<details class="pf-d"><summary class="pf-row" title="{tip}">{line}'
                        f'<span class="pf-chev">&#8250;</span></summary>'
                        f'<div class="pf-body"><div class="pf-note">{html.escape(k["how"])} Why it matters: '
                        f'{html.escape(k["why"])} Who fixes: {html.escape(k["fixer"])}.</div>'
                        f'<div class="pf-scroll"><table><thead><tr><th>{k.get("head", ("SKU", "Product"))[0]}</th><th>{k.get("head", ("SKU", "Product"))[1]}</th><th>What\'s wrong</th>'
                        f'</tr></thead><tbody>{items}</tbody></table></div></div></details>')
    when = _when(run) if run else ""
    return f"""<style>
.pf-card {{ background:{c["card"]}; border:1px solid {c["border"]}; border-radius:10px; padding:16px 20px 12px;
           color:{c["text"]}; }}
.pf-head {{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap; }}
.pf-head b {{ font-size:1.15rem; }}
.pf-muted {{ color:{c["muted"]}; font-size:.9rem; }}
.pf-bar {{ display:flex; gap:3px; margin:8px 0 6px; }}
.pf-bar i {{ flex:1; height:5px; border-radius:2px; }}
.pf-sec {{ color:{c["muted"]}; font-size:.8rem; margin:12px 0 2px; }}
.pf-row {{ display:flex; align-items:center; gap:10px; padding:3px 4px; border-radius:6px; }}
.pf-d > summary {{ list-style:none; cursor:pointer; }}
.pf-d > summary::-webkit-details-marker {{ display:none; }}
.pf-d > summary:hover {{ background:{c["border"]}55; }}
.pf-d[open] > summary {{ background:{c["border"]}77; }}
.pf-led {{ width:10px; height:10px; border-radius:50%; flex:0 0 10px; }}
.pf-name {{ white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.pf-lead {{ flex:1; border-bottom:1px dotted {c["leader"]}; transform:translateY(-4px); min-width:20px; }}
.pf-val {{ min-width:48px; text-align:right; font-weight:600; font-variant-numeric:tabular-nums; }}
.pf-chev {{ width:12px; color:{c["muted"]}; display:inline-block; transition:transform .15s; }}
.pf-d[open] .pf-chev {{ transform:rotate(90deg); }}
.pf-body {{ margin:4px 0 10px 24px; }}
.pf-note {{ color:{c["muted"]}; font-size:.85rem; margin:4px 0 6px; }}
.pf-scroll {{ max-height:320px; overflow:auto; border:1px solid {c["border"]}; border-radius:6px; }}
.pf-scroll table {{ width:100%; border-collapse:collapse; font-size:.88rem; margin:0; }}
.pf-scroll th {{ position:sticky; top:0; background:{c["card"]}; color:{c["muted"]}; text-align:left; font-weight:600;
                padding:6px 10px; border-bottom:1px solid {c["border"]}; }}
.pf-scroll td {{ padding:5px 10px; border-bottom:1px solid {c["border"]}; vertical-align:top; }}
@media (prefers-reduced-motion:reduce) {{ .pf-chev {{ transition:none; }} }}
</style>
<div class="pf-card">
  <div class="pf-head"><b>Pre-flight checklist</b>
    <span class="pf-muted">{cleared} of {len(checks)} clear, last check {html.escape(when)}</span></div>
  <div class="pf-bar">{bar}</div>
  {''.join(rows)}
</div>"""


def _flat(markup):
    """No indented lines: Streamlit's Markdown would read 4 leading spaces as a code block."""
    return "\n".join(line.strip() for line in markup.split("\n"))


def _check_explanations():
    """Explain each check in compact grouped sections without hiding anything in dropdowns."""
    for group in health.GROUPS:
        st.markdown(f"<h3><u>{group}</u></h3>", unsafe_allow_html=True)
        for check in (item for item in health.CHECKS if item["group"] == group):
            st.markdown(f"##### {check['title']}")
            st.markdown(check["how"])
            st.caption(f"Why it matters: {check['why']} · Who fixes it: {check['fixer']}")


# --- Data health ----------------------------------------------------------------------------
def page_health():
    st.header("Data health")
    st.caption("Pre-flight checks on Houston's data: every place where the website and WinMENTOR disagree, or the "
               "data is wrong, with who fixes it. Houston fixes nothing itself; a problem disappears once it is fixed "
               "at the source. The checks run after every WinMENTOR update and catalogue load, and whenever this "
               "page sees newer data than the last run.")
    run = _fresh_run()
    if not run:
        st.info("The checks haven't run yet. Press Run pre-flight checks.")
        _run_button("dh_run_first")
        return

    found = health.issues()
    by = defaultdict(list)
    for cid, sku, product, detail in found:
        by[cid].append((sku, product, detail))
    counts = {k["id"]: len(by.get(k["id"], [])) for k in health.CHECKS}

    waiting = ops.listing("waiting")
    tab_check, tab_how, tab_ops = st.tabs(["Pre-flight", "How each check works",
                                           f"For the operator ({len(waiting)})" if waiting else "For the operator"])
    with tab_check:
        left, right = st.columns([3, 1])
        with left:
            st.markdown(_flat(_checklist_html(run, counts, by)), unsafe_allow_html=True)
        with right:
            _run_button("dh_run")
            st.caption("Point at a check to see what it looks at. Click a red check to see its products.")
    with tab_how:
        st.caption("What each check looks at, in plain words. A green light means nothing was found; a red light "
                   "means at least one product needs fixing. A real EAN has 8, 12 or 13 digits and a correct last digit.")
        _check_explanations()
    with tab_ops:
        _operator_tab(waiting)


OPERATOR_FIELDS = {ops.EAN: "Cod de bare (EAN)", ops.NAME: "Denumire"}


def _operator_tab(waiting):
    st.caption("WinMENTOR changes you pushed from Review and push. Houston doesn't write to WinMENTOR: the operator "
               "makes each change, and it closes by itself once a later WinMENTOR load shows the new value.")
    if waiting:
        df = pd.DataFrame(waiting, columns=["SKU", "Product", "Field", "Now in WinMENTOR", "Should be", "Sent", "Done"])
        st.dataframe(df.drop(columns=["Done"]), hide_index=True)
        out = pd.DataFrame({"Cod (SKU)": df["SKU"], "Produs": df["Product"],
                            "Ce se schimbă": df["Field"].map(lambda f: OPERATOR_FIELDS.get(f, f)),
                            "Acum în WinMENTOR": df["Now in WinMENTOR"], "Corect": df["Should be"],
                            "Trimis": df["Sent"].str[:10]})
        st.download_button("Download the list for the operator (CSV)", out.to_csv(index=False).encode("utf-8-sig"),
                           file_name="schimbari-winmentor.csv", mime="text/csv")
    else:
        st.info("Nothing is waiting for the operator. Edit EAN in WinMENTOR or WinMENTOR name on Catalogue > Products "
                "(Data inspection) and push it from Review and push.")
    done = ops.listing("done")
    if done:
        with st.expander(f"Done ({len(done)})"):
            st.dataframe(pd.DataFrame(done, columns=["SKU", "Product", "Field", "Was", "Now", "Sent", "Seen in WinMENTOR"]),
                         hide_index=True)
