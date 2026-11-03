"""One-off for Houston step 5, Phase A: changes made by hand, and the prediction line in Review and push.

Run once from the Houston folder:   .\\.venv\\Scripts\\python.exe apply_changes_patch.py
It changes core.py, views.py and mcp_server.py, keeps a .bak copy of each first, writes nothing at all unless it
finds every place it needs, and is safe to run twice.
- core.py: a change added to Review and push again keeps the prediction already written for it.
- views.py: Review and push gets a Prediction column and one line for every ticked change without one; Change log
  gets two tabs, Pushed from Houston (with each push's prediction) and Made by hand.
- mcp_server.py: tells Claude what the two new tables hold.
hand_changes.py and views_changes.py (same zip) must be in the Houston folder first.
"""
import datetime
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M")
NEEDED = ["hand_changes.py", "views_changes.py"]

CORE = [(
    "    keys = {keyf(c) for c in changes}\n"
    "    pending = [p for p in pending if keyf(p) not in keys] + changes\n",
    "    keys = {keyf(c) for c in changes}\n"
    "    kept = {keyf(p): p[\"prediction\"] for p in pending if p.get(\"prediction\")}\n"
    "    for c in changes:                      # a change queued again keeps the prediction written for it\n"
    "        if not c.get(\"prediction\") and keyf(c) in kept:\n"
    "            c[\"prediction\"] = kept[keyf(c)]\n"
    "    pending = [p for p in pending if keyf(p) not in keys] + changes\n",
)]

VIEWS = [
    # Review and push: the Prediction column, kept as soon as it is typed, and one line for all ticked changes
    ("        r[\"Discard\"] = False\n"
     "        rows.append(r)\n"
     "    df = pd.DataFrame(rows)[[\"Push\", \"Discard\", \"Check\", \"Note\", \"What\", \"Item\", \"Field\", \"From\", \"To\"]]\n"
     "    st.caption(\"Passed changes are ticked. Warnings need your tick. Blocked changes can't be pushed.\")\n"
     "    edited = st.data_editor(df, hide_index=True, key=\"r_table\",\n"
     "                            disabled=[\"Check\", \"Note\", \"What\", \"Item\", \"Field\", \"From\", \"To\"])\n",
     "        r[\"Discard\"] = False\n"
     "        r[\"Prediction\"] = ch.get(\"prediction\", \"\")\n"
     "        rows.append(r)\n"
     "    df = pd.DataFrame(rows)[[\"Push\", \"Discard\", \"Check\", \"Note\", \"What\", \"Item\", \"Field\", \"From\", \"To\",\n"
     "                             \"Prediction\"]]\n"
     "    st.caption(\"Passed changes are ticked. Warnings need your tick. Blocked changes can't be pushed. \"\n"
     "               \"Prediction (optional): why, and what we expect by when. It goes into the change log with the \"\n"
     "               \"change.\")\n"
     "    # a fresh table whenever the waiting changes differ, so a tick or a prediction can't land on another row\n"
     "    table_key = \"r_table_\" + str(abs(hash(tuple(p[\"id\"] for p in pending))))\n"
     "    edited = st.data_editor(df, hide_index=True, key=table_key,\n"
     "                            disabled=[\"Check\", \"Note\", \"What\", \"Item\", \"Field\", \"From\", \"To\"],\n"
     "                            column_config={\"Prediction\": st.column_config.TextColumn(\"Prediction\", width=\"large\")})\n"
     "    typed = False\n"
     "    for i, r in edited.iterrows():                 # a prediction typed in the table is kept straight away\n"
     "        text = \"\" if pd.isna(r[\"Prediction\"]) else str(r[\"Prediction\"]).strip()\n"
     "        if text != (pending[i].get(\"prediction\") or \"\"):\n"
     "            pending[i][\"prediction\"] = text\n"
     "            typed = True\n"
     "    if typed:\n"
     "        core.save_pending(pending)\n"
     "    same = st.text_input(\"Prediction for every ticked change that has none (optional)\",\n"
     "                         key=f\"r_pred_{st.session_state.get('r_pred_n', 0)}\",\n"
     "                         placeholder=\"Why, and what we expect by when\")\n"),
    ("        m = need_connection() if any(ch[\"kind\"] != \"winmentor\" for ch in chosen) else mg()\n",
     "        if same.strip():\n"
     "            for ch in chosen:\n"
     "                if not (ch.get(\"prediction\") or \"\").strip():\n"
     "                    ch[\"prediction\"] = same.strip()\n"
     "            core.save_pending(pending)\n"
     "        m = need_connection() if any(ch[\"kind\"] != \"winmentor\" for ch in chosen) else mg()\n"),
    ("        st.session_state.last_push = [_row(ch, attrs, s, n) for ch, s, n in results]\n",
     "        st.session_state.last_push = [_row(ch, attrs, s, n) for ch, s, n in results]\n"
     "        st.session_state[\"r_pred_n\"] = st.session_state.get(\"r_pred_n\", 0) + 1   # an empty line next time\n"),
    # Change log: two tabs
    ("def page_log():\n"
     "    st.header(\"Change log\")\n"
     "    log = core.read_log()[::-1]\n",
     "def page_log():\n"
     "    st.header(\"Change log\")\n"
     "    import views_changes\n"
     "    pushed, by_hand = st.tabs([\"Pushed from Houston\", \"Made by hand\"])\n"
     "    with pushed:\n"
     "        _pushed_log()\n"
     "    with by_hand:\n"
     "        views_changes.hand_tab()\n"
     "\n"
     "\n"
     "def _pushed_log():\n"
     "    log = core.read_log()[::-1]\n"),
    ("\"Result\": e.get(\"status\", \"\"), \"Note\": e.get(\"note\", \"\")})",
     "\"Result\": e.get(\"status\", \"\"), \"Note\": e.get(\"note\", \"\"),\n"
     "                     \"Prediction\": e.get(\"prediction\", \"\")})"),
    ("    st.caption(\"Changes made before Houston (2 and 5 October) are in the project record, not here.\")\n",
     "    st.caption(\"Changes made outside Houston, for example by hand in the Magento admin, go in the tab \"\n"
     "               \"Made by hand.\")\n"),
]

MCP = [(
    "from winmentor import TABLE_NOTES as _WM_NOTES; TABLE_NOTES.update(_WM_NOTES)\n",
    "from winmentor import TABLE_NOTES as _WM_NOTES; TABLE_NOTES.update(_WM_NOTES)\n"
    "try:  # changes made by hand (Catalogue > Change log, tab Made by hand): what the new tables hold\n"
    "    from hand_changes import TABLE_NOTES as _HC_NOTES; TABLE_NOTES.update(_HC_NOTES)\n"
    "except Exception:  # noqa: BLE001  (a description must never stop the server)\n"
    "    pass\n",
)]

FILES = [("core.py", "keeps the prediction written for it", CORE),
         ("views.py", "def _pushed_log():", VIEWS),
         ("mcp_server.py", "from hand_changes import TABLE_NOTES", MCP)]


def _read(name):
    raw = (HERE / name).read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), newline


def _write(name, text, newline):
    p = HERE / name
    shutil.copy2(p, p.with_name(f"{name}.bak-{STAMP}"))
    with open(p, "w", encoding="utf-8", newline=newline) as f:
        f.write(text)
    print(f"{name}: done. The old file is kept as {name}.bak-{STAMP}")


def main():
    missing = [n for n in NEEDED if not (HERE / n).exists()]
    if missing:
        print("Copy these files from the zip into the Houston folder first: " + ", ".join(missing) +
              ". Nothing changed.")
        return 1
    ready, problems = [], []
    for name, marker, edits in FILES:
        if not (HERE / name).exists():
            problems.append(f"{name}: file not found")
            continue
        text, newline = _read(name)
        if marker in text:
            print(f"{name}: already done, nothing changed")
            continue
        for find, new in edits:
            if text.count(find) != 1:
                problems.append(f"{name}: couldn't find the place to change ({find.strip().splitlines()[0][:70]!r})")
                break
            text = text.replace(find, new)
        else:
            ready.append((name, text, newline))
    if problems:
        print("Nothing changed in any file:")
        for p in problems:
            print("  " + p)
        print("Paste these lines into the chat.")
        return 1
    if not ready:
        print("Nothing to do: this patch is already installed.")
        return 0
    for name, text, newline in ready:
        _write(name, text, newline)
    print("Done. Restart Houston, and restart Claude Desktop so the Houston connection describes the new tables.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
