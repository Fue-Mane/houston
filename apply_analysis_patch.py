"""One-off for Houston step 5, Phase B: the Product Analysis section.

Run once from the Houston folder:   .\\.venv\\Scripts\\python.exe apply_analysis_patch.py
It changes app.py, views.py and product_card.py, keeps a .bak copy of each first, writes nothing at all unless it
finds every place it needs, and is safe to run twice.
- app.py: the menu gets a Product Analysis section (Overview, Product page) between Marketing and Catalogue.
- product_card.py: the product window gets an "Open product page" button; its Google and Data checks parts can be
  shown without their title, for the product page.
- views.py: Catalogue > Products opens the product sent by "Edit in Catalogue".
analysis.py and views_analysis.py (same zip) must be in the Houston folder first.
"""
import datetime
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M")
NEEDED = ["analysis.py", "views_analysis.py"]

APP = [
    ("import views_marketing  # noqa: E402\n",
     "import views_marketing  # noqa: E402\n"
     "import views_analysis  # noqa: E402\n"),
    ("        st.Page(views.page_google_ads, title=\"Google Ads\"),\n"
     "    ],\n"
     "    \"Catalogue\": [\n",
     "        st.Page(views.page_google_ads, title=\"Google Ads\"),\n"
     "    ],\n"
     "    \"Product Analysis\": views_analysis.pages(),\n"
     "    \"Catalogue\": [\n"),
]

CARD = [
    ("def _google(r, rec, sku):\n"
     "    _title(r, rec, sku)\n",
     "def _google(r, rec, sku, title=True):\n"
     "    if title:\n"
     "        _title(r, rec, sku)\n"),
    ("def _checks(r, rec, sku, found):\n"
     "    _title(r, rec, sku)\n",
     "def _checks(r, rec, sku, found, title=True):\n"
     "    if title:\n"
     "        _title(r, rec, sku)\n"),
    ("    part = _pick_part(f\"pc_part_{sku}\", labels)\n",
     "    left, right = st.columns([3, 1])\n"
     "    with left:\n"
     "        part = _pick_part(f\"pc_part_{sku}\", labels)\n"
     "    if right.button(\"Open product page\", key=f\"pc_page_{sku}\"):\n"
     "        import views_analysis\n"
     "        views_analysis.open_product_page(sku)\n"),
]

VIEWS = [(
    "    st.header(\"Products\")\n"
    "    import search_data as sd\n",
    "    st.header(\"Products\")\n"
    "    jump = st.session_state.pop(\"open_in_catalogue\", None)      # \"Edit in Catalogue\" on the product page\n"
    "    if jump in prods:\n"
    "        st.session_state[\"p_q\"] = jump.strip()\n"
    "        st.session_state[\"p_open\"] = jump\n"
    "    import search_data as sd\n",
)]

FILES = [("app.py", "views_analysis", APP),
         ("product_card.py", "Open product page", CARD),
         ("views.py", "open_in_catalogue", VIEWS)]


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
    print("Done. Restart Houston: the menu has a Product Analysis section.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
