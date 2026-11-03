"""One-off: runs the data health checks after every WinMENTOR update and every catalogue load.

Run once from the Houston folder:   .\\.venv\\Scripts\\python.exe apply_health_patch.py
It changes views.py and views_orders.py (an import and one call in each), keeps a .bak copy of each file first,
and changes nothing in a file where it can't find the exact place. Safe to run twice.
"""
import datetime
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M")


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


def _replace_once(text, find, new, name):
    if text.count(find) != 1:
        raise LookupError(f"{name}: couldn't find the place to change ({find.strip().splitlines()[0][:70]!r})")
    return text.replace(find, new)


def patch_views_orders():
    name = "views_orders.py"
    text, nl = _read(name)
    if "import health" in text:
        print(f"{name}: already done, nothing changed")
        return
    text = _replace_once(text, "import orders\nimport winmentor as wm\n",
                         "import health\nimport orders\nimport winmentor as wm\n", name)
    line = "        msg = f\"{res['loaded']} new files loaded ({res['in_bucket']} in the bucket).\"\n"
    text = _replace_once(text, line, line +
                         "        h = health.rebuild_quietly(\"WinMENTOR update\")\n"
                         "        if h:\n"
                         "            msg += f\" Data health: {h['issues']} problems on {h['products']} website products.\"\n",
                         name)
    _write(name, text, nl)


def patch_views():
    name = "views.py"
    text, nl = _read(name)
    if "import health" in text:
        print(f"{name}: already done, nothing changed")
        return
    text = _replace_once(text, "import core\nfrom magento import ApiError, Magento\n",
                         "import core\nimport health\nfrom magento import ApiError, Magento\n", name)
    lines = text.split("\n")
    start = [i for i, x in enumerate(lines) if "st.session_state.cache = core.load_catalogue(" in x]
    if len(start) != 1:
        raise LookupError(f"{name}: couldn't find the catalogue load")
    rerun = next((i for i in range(start[0] + 1, min(start[0] + 6, len(lines))) if lines[i].strip() == "st.rerun()"),
                 None)
    if rerun is None:
        raise LookupError(f"{name}: couldn't find the line after the catalogue load")
    indent = lines[rerun][:len(lines[rerun]) - len(lines[rerun].lstrip())]
    lines.insert(rerun, f"{indent}health.rebuild_quietly(\"Catalogue load\")")
    _write(name, "\n".join(lines), nl)


if __name__ == "__main__":
    for step in (patch_views_orders, patch_views):
        try:
            step()
        except LookupError as e:
            print(f"{e}. Nothing changed in that file: paste this message into the chat.")
