"""One-off for the merged Products page: lets WinMENTOR changes go through Review and push to the operator's list.

Run once from the Houston folder:   .\\.venv\\Scripts\\python.exe apply_products_patch.py
- core.py: push() hands changes of kind 'winmentor' to operator_requests.send() instead of the store.
- views.py: the review table names them "WinMENTOR (for the operator)", counts them as done when sent, and pushing
  only WinMENTOR changes doesn't need the store connection.
Keeps a .bak copy of each file first, changes nothing in a file where it can't find the exact place. Safe to run twice.
"""
import datetime
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M")


def _read(name):
    raw = (HERE / name).read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), ("\r\n" if "\r\n" in raw else "\n")


def _patch(name, marker, edits):
    text, nl = _read(name)
    if marker in text:
        print(f"{name}: already done, nothing changed")
        return
    for find, new in edits:
        if text.count(find) != 1:
            raise LookupError(f"{name}: couldn't find the place to change ({find.strip().splitlines()[0][:70]!r})")
        text = text.replace(find, new)
    p = HERE / name
    shutil.copy2(p, p.with_name(f"{name}.bak-{STAMP}"))
    with open(p, "w", encoding="utf-8", newline=nl) as f:
        f.write(text)
    print(f"{name}: done. The old file is kept as {name}.bak-{STAMP}")


CORE = [(
    'def push(mg, cache, changes, progress=lambda f, t: None):\n'
    '    """Writes changes, verifies each one, logs it. Returns list of (change, status, note)."""\n',
    'def push(mg, cache, changes, progress=lambda f, t: None):\n'
    '    """Writes changes, verifies each one, logs it. Returns list of (change, status, note)."""\n'
    '    if any(c.get("kind") == "winmentor" for c in changes):   # never written: they go to the operator\'s list\n'
    '        import operator_requests\n'
    '        rest = [c for c in changes if c.get("kind") != "winmentor"]\n'
    '        done = push(mg, cache, rest, progress) if rest else []\n'
    '        return done + operator_requests.send([c for c in changes if c.get("kind") == "winmentor"])\n',
)]

VIEWS = [
    ('"image_remove": "Remove image", "option_add": "New option"}',
     '"image_remove": "Remove image", "option_add": "New option",\n'
     '              "winmentor": "WinMENTOR (for the operator)"}'),
    ('        ok = sum(1 for r in res if r["Check"] == "Done")\n',
     '        ok = sum(1 for r in res if r["Check"] in ("Done", "Sent to the operator"))\n'),
    ('        st.dataframe(pd.DataFrame(res), hide_index=True)\n    pending = core.load_pending()\n',
     '        st.dataframe(pd.DataFrame(res), hide_index=True)\n'
     '        sent = sum(1 for r in res if r["Check"] == "Sent to the operator")\n'
     '        if sent:\n'
     '            st.info(f"{sent} WinMENTOR changes went to the operator\'s list: Data health, tab For the operator.")\n'
     '    pending = core.load_pending()\n'),
    ('        m = need_connection()\n        bar = st.progress(0.0, "Writing")\n',
     '        m = need_connection() if any(ch["kind"] != "winmentor" for ch in chosen) else mg()\n'
     '        bar = st.progress(0.0, "Writing")\n'),
]

if __name__ == "__main__":
    for name, marker, edits in (("core.py", "operator_requests.send", CORE),
                                ("views.py", '"winmentor": "WinMENTOR (for the operator)"', VIEWS)):
        try:
            _patch(name, marker, edits)
        except LookupError as e:
            print(f"{e}. Nothing changed in that file: paste this message into the chat.")
