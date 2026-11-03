"""Changes made outside Houston, for example by hand in the Magento admin.

They are entered once with the "Add change by hand" form (Catalogue > Change log, tab Made by hand). Together with
Houston's own pushes (change_log), they are the record Product Analysis uses to judge whether changes worked: each
change is dated, says which products it touched and what kind of change it was, and can carry a prediction (why,
and what we expect by when).

Tables:
- hand_changes: one row per entry. day 'YYYY-MM-DD'; scope 'product', 'list', 'category' or 'site';
  scope_label: the category's name for a category entry; types: one or more of TYPES, joined with ', ';
  what: what changed; prediction; draft: the draft it was saved from ('' if none); entered_at.
- hand_change_products: the products an entry touched (change_id, sku, product_id). A category entry keeps the
  products that were in the category when it was saved. A whole-site entry has none: it applies to every product.

No Streamlit here: the page is views_changes.py.
"""
import datetime
import json
import re
from contextlib import closing

import core

TYPES = ["Name / meta", "Description", "Images", "Page design", "Price / promo", "Other"]
SCOPES = {"product": "One product", "list": "A list of products", "category": "A category",
          "site": "The whole site"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS hand_changes (id INTEGER PRIMARY KEY, day TEXT NOT NULL, scope TEXT NOT NULL,
    scope_label TEXT DEFAULT '', types TEXT NOT NULL, what TEXT NOT NULL, prediction TEXT DEFAULT '',
    draft TEXT DEFAULT '', entered_at TEXT);
CREATE TABLE IF NOT EXISTS hand_change_products (change_id INTEGER, sku TEXT, product_id TEXT,
    PRIMARY KEY (change_id, sku));
CREATE INDEX IF NOT EXISTS ix_hand_change_products_sku ON hand_change_products (sku);
"""

# For the MCP server's describe_database (mcp_server.py adds these to its table notes).
TABLE_NOTES = {
    "hand_changes": "Changes made outside Houston (e.g. by hand in the Magento admin), entered on Catalogue > "
                    "Change log, tab Made by hand. day = date of the change; scope = product, list, category or "
                    "site (site = every product); types = one or more of: " + ", ".join(TYPES) + " (joined with "
                    "', '); what = what changed; prediction = why, and what we expect by when; draft = the draft "
                    "from the session logs it was saved from.",
    "hand_change_products": "Products each hand_changes entry touched (change_id = hand_changes.id). A category "
                            "entry keeps the products that were in the category when it was saved; a site entry "
                            "has none, because it applies to every product.",
    "change_log": "Every change pushed from Houston, with result (Done/Check/Failed) and backup file. The JSON in "
                  "entry may carry 'prediction': why, and what we expect by when.",
    "pending": "Changes waiting in Houston's Review and push. The JSON in change may carry 'prediction'.",
}

# Houston's pushes, by the kind of change they are (for_product)
FIELD_TYPES = {"name": "Name / meta", "meta_title": "Name / meta", "meta_description": "Name / meta",
               "meta_keyword": "Name / meta", "description": "Description", "short_description": "Description",
               "price": "Price / promo", "special_price": "Price / promo", "special_from_date": "Price / promo",
               "special_to_date": "Price / promo"}
PRODUCT_KINDS = ("product", "stock", "image_update", "image_add", "image_remove", "cat_assign", "cat_remove",
                 "link_add", "link_remove")

# --- drafts: changes made before Houston's change log began (7 Oct 2026) ----------------------------------------
# From the session logs of 27 and 30 Sep 2026 and the work log (2 and 5 Oct). Stefan checks each one and saves it.
_PREDICTION_30_SEP = ("No measurable change in clicks. Possible position gains on the phrasings now in the names, "
                      "within 8–12 weeks. The gluten-free opener may lift CTR only where a gluten-free page is "
                      "already on page 1. (Written before the 29–30 Sep changes, session log of 30 Sep.)")
PLANT_DRINKS = ["3019998", "3019038", "3018991", "3019009", "3024291", "3028872/3", "3028873/3", "3018983",
                "3021522", "3028871/3", "3019092", "3033719", "3019039", "3019025", "3033214", "3019073", "3028014",
                "0845294", "3019023", "0310055", "0040380", "3022254", "3025986", "3036334", "3019081", "3019047",
                "3043368", "3041010", "3042256", "3045762", "3019053", "3019067", "0018470"]
DRAFTS = [
    {"id": "2026-09-27-plant-drinks", "label": "27 Sep: 33 plant drinks", "day": "2026-09-27", "scope": "list",
     "skus": PLANT_DRINKS, "types": ["Name / meta", "Description"],
     "what": "33 plant drinks: new names; 27 meta titles cleared (the 25 with „lapte”, plus 3042256 and 3045762); "
             "new meta descriptions; FAQ block added at the end of the description. 12 names were corrected again "
             "on 27–28 Sep (exact date not recorded).",
     "prediction": "No measurable change in clicks. CTR changes little, and only where a page is already on page 1. "
                   "Possible position gains on „băutură vegetală” searches within 8–12 weeks. Any movement on "
                   "„lapte” searches would come from the FAQ and can't be sized. Wrong if „băutură vegetală din "
                   "migdale” and „lapte de migdale” haven't moved by early December. (Written 27 Sep, session log "
                   "of 27 Sep.)"},
    {"id": "2026-09-29-flakes", "label": "29 Sep: 23 flakes, musli and porridge", "day": "2026-09-29",
     "scope": "list", "skus": [], "types": ["Name / meta"],
     "what": "Flakes, musli and porridge, 23 products: new names and meta descriptions.",
     "prediction": _PREDICTION_30_SEP, "todo": "Paste the 23 SKUs from your Change log (the rows of 29 Sep)."},
    {"id": "2026-09-30-flakes-100", "label": "30 Sep: „100%” on 3 flakes", "day": "2026-09-30", "scope": "list",
     "skus": ["0310128", "0848326", "0901034"], "types": ["Name / meta"],
     "what": "„100%” added to 3 flake names: corn flakes (0310128) and coconut flakes (0848326, 0901034).",
     "prediction": _PREDICTION_30_SEP},
    {"id": "2026-09-30-pasta", "label": "30 Sep: pastas", "day": "2026-09-30", "scope": "list", "skus": [],
     "types": ["Name / meta"],
     "what": "Pasta: new names („Paste bio [Formă] din …”) and meta descriptions. No meta titles to clear.",
     "prediction": _PREDICTION_30_SEP, "todo": "Paste the pasta SKUs from your Change log (the pasta rows of 30 Sep)."},
    {"id": "2026-09-30-gnocchi", "label": "30 Sep: 2 gnocchi", "day": "2026-09-30", "scope": "list",
     "skus": ["0700126", "0700127"], "types": ["Name / meta", "Description"],
     "what": "Gnocchi, 2 products: names, meta descriptions, and a FAQ with one question: „Cât timp se fierb "
             "gnocchi?”",
     "prediction": _PREDICTION_30_SEP},
    {"id": "2026-09-30-vinegars", "label": "30 Sep: 6 vinegars", "day": "2026-09-30", "scope": "list",
     "skus": ["0036185", "0702175", "0036186", "0702474", "0037556", "0039593"], "types": ["Name / meta"],
     "what": "Vinegars, 6 products: names and meta descriptions.", "prediction": _PREDICTION_30_SEP},
    {"id": "2026-09-30-creams", "label": "30 Sep: 5 cooking creams", "day": "2026-09-30", "scope": "list",
     "skus": ["3018996", "0039829", "0018588", "3019001", "3019005"], "types": ["Name / meta", "Description"],
     "what": "Cooking creams, 5 products: names, meta descriptions, and a FAQ with the legal question („smântână” "
             "/ „frișcă”) plus one product question. The FAQ was on all 5 pages in the catalogue copy of 22 Oct; "
             "the day it was added isn't recorded.",
     "prediction": _PREDICTION_30_SEP},
    {"id": "2026-10-02-happymais", "label": "2 Oct: 3 Happymais meta descriptions", "day": "2026-10-02",
     "scope": "list", "skus": ["HM-MAR", "HM-SOGMEI", "HM-VOLMAS"], "types": ["Name / meta"],
     "what": "Meta descriptions of 3 Happymais toys, imported by file (test of the Dataflow import).",
     "prediction": ""},
    {"id": "2026-10-05-happymais", "label": "5 Oct: 1 Happymais meta description", "day": "2026-10-05",
     "scope": "list", "skus": ["HM-VOLFEM"], "types": ["Name / meta"],
     "what": "Meta description of one Happymais toy, written through the store API (test of API writes).",
     "prediction": ""},
]


def connect():
    c = core.connect()
    if not core.READ_ONLY:
        c.executescript(SCHEMA)
    return c


# --- the catalogue copy ----------------------------------------------------------------------------------------
def catalogue_products():
    """{SKU as stored: name} from the catalogue copy."""
    with closing(core.connect()) as c:
        return {sku: name or "" for sku, name in c.execute("SELECT sku, name FROM products")}


def categories():
    """[(category_id, label, products)] for the categories holding products in the catalogue copy, in tree order."""
    with closing(core.connect()) as c:
        counts = dict(c.execute("SELECT category_id, COUNT(*) FROM product_categories GROUP BY category_id"))
        rows = c.execute("SELECT category_id, path FROM categories ORDER BY rowid").fetchall()
    return [(cid, path, counts[cid]) for cid, path in rows if counts.get(cid)]


def category_label(category_id):
    with closing(core.connect()) as c:
        row = c.execute("SELECT path FROM categories WHERE category_id = ?", (str(category_id),)).fetchone()
    return row[0] if row else str(category_id)


def category_products(category_id):
    with closing(core.connect()) as c:
        return [r[0] for r in c.execute("SELECT sku FROM product_categories WHERE category_id = ? ORDER BY sku",
                                        (str(category_id),))]


# --- pasted SKUs -----------------------------------------------------------------------------------------------
_SPLIT = re.compile(r"[\s,;]+")


def _loose(sku):
    """What two forms of one SKU have in common: digits without leading zeros, otherwise lower case."""
    s = (sku or "").strip()
    return (s.lstrip("0") or "0") if s.isdigit() else s.lower()


def match_skus(text, catalogue):
    """Pasted SKUs against the catalogue copy. catalogue: {SKU as stored: name}.

    Returns (found, not_found, unclear): found = [(typed, SKU as stored, how)], with how = 'exact', 'spaces
    ignored', 'leading zeros ignored' or 'case ignored'; not_found and unclear (more than one product fits) list
    what was typed. Separators: new lines, tabs, spaces, commas and semicolons. A header cell 'SKU' is skipped,
    and each product counts once. 'Leading zeros ignored' covers the Change log's 7-digit padding (0591715 for
    591715)."""
    trimmed, loose = {}, {}
    for s in catalogue:
        trimmed.setdefault(s.strip(), []).append(s)
        loose.setdefault(_loose(s), []).append(s)
    found, not_found, unclear, typed_seen, products_seen = [], [], [], set(), set()
    for typed in _SPLIT.split(text or ""):
        if not typed or typed.lower() == "sku" or typed in typed_seen:
            continue
        typed_seen.add(typed)
        if typed in catalogue:
            hits, how = [typed], "exact"
        elif typed in trimmed:
            hits, how = trimmed[typed], "spaces ignored"
        else:
            hits = loose.get(_loose(typed), [])
            how = "leading zeros ignored" if typed.isdigit() else "case ignored"
        if len(hits) == 1:
            if hits[0] not in products_seen:
                products_seen.add(hits[0])
                found.append((typed, hits[0], how))
        elif hits:
            unclear.append(typed)
        else:
            not_found.append(typed)
    return found, not_found, unclear


# --- entries ---------------------------------------------------------------------------------------------------
def _as_date(day):
    if isinstance(day, datetime.datetime):
        return day.date()
    if isinstance(day, datetime.date):
        return day
    return datetime.date.fromisoformat(str(day)[:10])


def add(day, scope, types, what, prediction="", skus=(), scope_label="", draft=""):
    """Save one entry and return its id. Raises ValueError with a plain message when something is missing."""
    day = _as_date(day)
    if day > datetime.date.today():
        raise ValueError("The date is in the future. Use the day the change was made.")
    if scope not in SCOPES:
        raise ValueError(f"Unknown choice of products: {scope!r}.")
    types = [t for t in TYPES if t in (types or [])]
    if not types:
        raise ValueError("Choose at least one type of change.")
    what = (what or "").strip()
    if not what:
        raise ValueError("Write what changed.")
    skus = [] if scope == "site" else list(dict.fromkeys(s for s in (skus or []) if s))
    if scope != "site" and not skus:
        raise ValueError("Choose the products it touched.")
    with closing(connect()) as c, c:
        ids = dict(c.execute("SELECT sku, product_id FROM products"))
        missing = [s for s in skus if s not in ids]
        if missing:
            raise ValueError("Not in the catalogue copy: " + ", ".join(missing[:10]) + ".")
        cur = c.execute("INSERT INTO hand_changes (day, scope, scope_label, types, what, prediction, draft, entered_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (day.isoformat(), scope, scope_label or "", ", ".join(types), what,
                         (prediction or "").strip(), draft or "", core.now()))
        cid = cur.lastrowid
        c.executemany("INSERT INTO hand_change_products VALUES (?, ?, ?)", [(cid, s, ids[s]) for s in skus])
    return cid


def delete(change_id):
    with closing(connect()) as c, c:
        c.execute("DELETE FROM hand_change_products WHERE change_id = ?", (int(change_id),))
        c.execute("DELETE FROM hand_changes WHERE id = ?", (int(change_id),))


def entries():
    """Every entry, newest day first: DataFrame id, day, scope, scope_label, types, what, prediction, draft,
    entered_at, products (how many)."""
    import pandas as pd
    with closing(connect()) as c:
        return pd.read_sql_query(
            "SELECT h.id, h.day, h.scope, h.scope_label, h.types, h.what, h.prediction, h.draft, h.entered_at, "
            "COUNT(p.sku) AS products FROM hand_changes h LEFT JOIN hand_change_products p ON p.change_id = h.id "
            "GROUP BY h.id ORDER BY h.day DESC, h.id DESC", c)


def products_of(change_id):
    """The products of one entry: DataFrame SKU, Product."""
    import pandas as pd
    with closing(connect()) as c:
        return pd.read_sql_query(
            "SELECT p.sku AS SKU, COALESCE(pr.name, '') AS Product FROM hand_change_products p "
            "LEFT JOIN products pr ON pr.sku = p.sku WHERE p.change_id = ? ORDER BY p.sku", c,
            params=[int(change_id)])


def drafts():
    """The drafts not saved yet, oldest first."""
    with closing(connect()) as c:
        saved = {r[0] for r in c.execute("SELECT draft FROM hand_changes WHERE draft <> ''")}
    return [d for d in DRAFTS if d["id"] not in saved]


def _push_type(kind, field):
    if kind == "product":
        return FIELD_TYPES.get(field, "Other")
    return "Images" if kind.startswith("image_") else "Other"


def for_product(sku):
    """Every recorded change that touched one product, newest first: Houston's pushes (written ones only) and
    changes made by hand, whole-site entries included. DataFrame day, source, types, what, prediction."""
    import pandas as pd
    rows = []
    with closing(connect()) as c:
        marks = ",".join("?" * len(PRODUCT_KINDS))
        for pushed_at, kind, field, entry in c.execute(
                f"SELECT pushed_at, kind, field, entry FROM change_log WHERE target = ? AND kind IN ({marks}) "
                f"AND status IN ('Done', 'Check')", (sku, *PRODUCT_KINDS)):
            try:
                prediction = (json.loads(entry or "{}") or {}).get("prediction", "")
            except (json.JSONDecodeError, AttributeError):
                prediction = ""
            rows.append(((pushed_at or "")[:10], "Houston push", _push_type(kind, field or ""), field or kind,
                         prediction or ""))
        for day, types, what, prediction in c.execute(
                "SELECT day, types, what, prediction FROM hand_changes WHERE scope = 'site' OR id IN "
                "(SELECT change_id FROM hand_change_products WHERE sku = ?)", (sku,)):
            rows.append((day, "By hand", types, what, prediction or ""))
    df = pd.DataFrame(rows, columns=["day", "source", "types", "what", "prediction"])
    return df.sort_values("day", ascending=False, kind="stable").reset_index(drop=True)
