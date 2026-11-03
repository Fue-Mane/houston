"""Catalogue cache, change queue, checks, push and change log. No Streamlit code here."""
import datetime
import json
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from magento import ApiError

DATA = Path(__file__).parent / "data"
CACHE = DATA / "catalogue.json"      # old format, migrated into houston.db
PENDING = DATA / "pending.json"
LOG = DATA / "changelog.jsonl"
BACKUPS = DATA / "backups"
UPLOADS = DATA / "uploads"

LOCKED_PRODUCT = {"url_key", "url_path", "sku", "entity_id", "type_id", "attribute_set_id",
                  "created_at", "updated_at", "has_options", "required_options"}
LOCKED_CATEGORY = {"url_key", "url_path", "path", "parent_id", "level", "position", "children",
                   "all_children", "path_in_store", "children_count", "created_at", "updated_at"}
FALLBACK_LABELS = {"name": "Name", "meta_title": "Meta title", "meta_description": "Meta description",
                   "meta_keyword": "Meta keywords", "url_key": "URL key", "description": "Description",
                   "short_description": "Short description", "price": "Price", "special_price": "Special price",
                   "status": "Status", "visibility": "Visibility", "manufacturer": "Manufacturer", "weight": "Weight",
                   "image": "Base image", "small_image": "Small image", "thumbnail": "Thumbnail",
                   "image_label": "Base image label", "small_image_label": "Small image label",
                   "thumbnail_label": "Thumbnail label", "tax_class_id": "Tax class", "news_from_date": "New from",
                   "news_to_date": "New to", "special_from_date": "Special price from", "special_to_date": "Special price to"}
TEXT_FIELDS_LAPTE = {"name", "meta_title", "meta_description", "short_description", "description", "meta_keyword"}
NUMERIC_TYPES = {"price", "weight"}
IGNORE_ON_COMPARE = {"updated_at"}
PUSH_BATCH = 10  # products per request; keeps each request short for the store


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def chunks(seq, n):
    seq = list(seq)
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def norm(v):
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return sorted(str(x) for x in v)
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def as_text(v):
    """Values that come back as lists (tier prices, galleries...) become one line of text."""
    n = norm(v)
    return ", ".join(n) if isinstance(n, list) else n


def has_value(v):
    n = norm(v)
    return bool(n) if isinstance(n, list) else n.strip() != ""


# --- storage: one SQLite file, data/houston.db ---------------------------------
# Houston writes; the MCP server opens it read-only. WAL mode lets both run at the same time.
# Future sources get their own tables (gsc_..., ga4_..., ads_..., store_search_...), each row dated.
READ_ONLY = False          # the MCP server sets this to True
_schema_ready = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS attributes (code TEXT PRIMARY KEY, attribute_id TEXT, type TEXT, scope TEXT,
    required TEXT, label TEXT, label_error TEXT, has_options INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS attribute_options (code TEXT, position INTEGER, value TEXT, label TEXT,
    PRIMARY KEY (code, value));
CREATE TABLE IF NOT EXISTS attribute_sets (set_id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS set_attributes (set_id TEXT, position INTEGER, code TEXT, PRIMARY KEY (set_id, code));
CREATE TABLE IF NOT EXISTS products (sku TEXT PRIMARY KEY, product_id TEXT, set_id TEXT, type TEXT,
    status TEXT, visibility TEXT, name TEXT, meta_title TEXT, meta_description TEXT, url_key TEXT,
    price TEXT, qty TEXT, is_in_stock TEXT, read_error TEXT);
CREATE TABLE IF NOT EXISTS product_values (sku TEXT, code TEXT, value TEXT, is_json INTEGER DEFAULT 0,
    PRIMARY KEY (sku, code));
CREATE TABLE IF NOT EXISTS product_categories (sku TEXT, category_id TEXT, PRIMARY KEY (sku, category_id));
CREATE TABLE IF NOT EXISTS categories (category_id TEXT PRIMARY KEY, parent_id TEXT, name TEXT, depth INTEGER,
    path TEXT, position INTEGER);
CREATE TABLE IF NOT EXISTS category_tree (id INTEGER PRIMARY KEY CHECK (id = 1), tree TEXT);
CREATE TABLE IF NOT EXISTS pending (seq INTEGER, id TEXT PRIMARY KEY, created TEXT, kind TEXT, target TEXT,
    field TEXT, old TEXT, new TEXT, change TEXT);
CREATE TABLE IF NOT EXISTS change_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, pushed_at TEXT, kind TEXT,
    target TEXT, target_label TEXT, field TEXT, old TEXT, new TEXT, status TEXT, note TEXT, backup TEXT, entry TEXT);
CREATE TABLE IF NOT EXISTS gsc_totals (property TEXT, date TEXT, clicks INTEGER, impressions INTEGER, ctr REAL,
    position REAL, pulled_at TEXT, PRIMARY KEY (property, date));
CREATE TABLE IF NOT EXISTS dict (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL,
    UNIQUE (kind, value));
CREATE TABLE IF NOT EXISTS gsc_pages_c (prop INTEGER, date TEXT, page INTEGER, device INTEGER, clicks INTEGER,
    impressions INTEGER, position REAL, PRIMARY KEY (prop, date, page, device)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS gsc_queries_c (prop INTEGER, date TEXT, query INTEGER, page INTEGER, device INTEGER,
    clicks INTEGER, impressions INTEGER, position REAL, PRIMARY KEY (prop, date, query, page, device)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS gsc_pulls (pulled_at TEXT, property TEXT, table_name TEXT, start_date TEXT, end_date TEXT,
    rows INTEGER);
CREATE TABLE IF NOT EXISTS ga4_totals (property TEXT, date TEXT, sessions INTEGER, active_users INTEGER,
    engaged_sessions INTEGER, key_events REAL, purchases INTEGER, revenue REAL, pulled_at TEXT, PRIMARY KEY (property, date));
CREATE TABLE IF NOT EXISTS ga4_landing_c (prop INTEGER, date TEXT, page INTEGER, channel INTEGER, sessions INTEGER,
    active_users INTEGER, engaged_sessions INTEGER, key_events REAL, purchases INTEGER, revenue REAL,
    PRIMARY KEY (prop, date, page, channel)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ga4_items (property TEXT, date TEXT, item_id TEXT, item_name TEXT, items_viewed INTEGER,
    items_added_to_cart INTEGER, items_purchased INTEGER, item_revenue REAL, pulled_at TEXT,
    PRIMARY KEY (property, date, item_id, item_name));
CREATE TABLE IF NOT EXISTS ga4_site_search (property TEXT, date TEXT, search_term TEXT, searches INTEGER, pulled_at TEXT,
    PRIMARY KEY (property, date, search_term));
CREATE TABLE IF NOT EXISTS ga4_pulls (pulled_at TEXT, property TEXT, table_name TEXT, start_date TEXT, end_date TEXT,
    rows INTEGER);
CREATE TABLE IF NOT EXISTS ga4_pull_errors (pulled_at TEXT, property TEXT, table_name TEXT, start_date TEXT,
    end_date TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS ix_ga4_items_id ON ga4_items (item_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_values_code ON product_values (code);
CREATE INDEX IF NOT EXISTS ix_log_target ON change_log (target);
"""
# The three big Google tables are stored compactly: every page address, search, device, channel and property
# is stored once in `dict`, and the data rows point to it by number (the *_c tables, WITHOUT ROWID so the key
# isn't stored twice). Views with the old names give the familiar columns back, so readers see no difference.
COMPACT_VIEWS = {
    "gsc_pages": """CREATE VIEW gsc_pages AS SELECT p.value AS property, t.date, g.value AS page, d.value AS device,
        t.clicks, t.impressions, CASE WHEN t.impressions > 0 THEN 1.0 * t.clicks / t.impressions ELSE 0 END AS ctr,
        t.position FROM gsc_pages_c t JOIN dict p ON p.id = t.prop JOIN dict g ON g.id = t.page
        JOIN dict d ON d.id = t.device""",
    "gsc_queries": """CREATE VIEW gsc_queries AS SELECT p.value AS property, t.date, q.value AS query, g.value AS page,
        d.value AS device, t.clicks, t.impressions,
        CASE WHEN t.impressions > 0 THEN 1.0 * t.clicks / t.impressions ELSE 0 END AS ctr, t.position
        FROM gsc_queries_c t JOIN dict p ON p.id = t.prop JOIN dict q ON q.id = t.query JOIN dict g ON g.id = t.page
        JOIN dict d ON d.id = t.device""",
    "ga4_landing": """CREATE VIEW ga4_landing AS SELECT p.value AS property, t.date, g.value AS landing_page,
        ch.value AS channel, t.sessions, t.active_users, t.engaged_sessions, t.key_events, t.purchases, t.revenue
        FROM ga4_landing_c t JOIN dict p ON p.id = t.prop JOIN dict g ON g.id = t.page JOIN dict ch ON ch.id = t.channel""",
}
ACTION_LINKS = "(Magento action links)"
ACTION_PREFIXES = ["/catalog/product_compare/", "/wishlist/", "/productalert/", "/sendfriend/"]

CATALOGUE_TABLES = ["meta", "attributes", "attribute_options", "attribute_sets", "set_attributes", "products",
                    "product_values", "product_categories", "categories", "category_tree"]


def db_path():
    return DATA / "houston.db"


def connect():
    path = db_path()
    if READ_ONLY:
        if not path.exists():
            raise FileNotFoundError("No Houston database yet: open Houston and press Load catalogue.")
        c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
        c.execute("PRAGMA query_only = ON")
        return c
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), timeout=30)
    if str(path) not in _schema_ready:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        _ensure_views(c)
        _migrate_json(c)
        _schema_ready.add(str(path))
    return c


def _kind_of(c, name):
    row = c.execute("SELECT type FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def _ensure_views(c):
    for name, sql in COMPACT_VIEWS.items():
        if _kind_of(c, name) is None:      # a table of that name means the reorganisation hasn't run yet
            c.execute(sql)
    c.commit()


def needs_compaction():
    with closing(connect()) as c:
        return any(_kind_of(c, n) == "table" for n in COMPACT_VIEWS)


def ids(c, kind, values):
    """{value: number} for text values, adding the ones not stored yet."""
    vals = list({"" if v is None else str(v) for v in values})
    c.executemany("INSERT OR IGNORE INTO dict (kind, value) VALUES (?, ?)", [(kind, v) for v in vals])
    out = {}
    for i in range(0, len(vals), 500):
        part = vals[i:i + 500]
        out.update({v: n for n, v in c.execute(
            f"SELECT id, value FROM dict WHERE kind = ? AND value IN ({','.join('?' * len(part))})", [kind, *part])})
    return out


def action_link(page):
    return ACTION_LINKS if any(str(page).startswith(p) for p in ACTION_PREFIXES) else page


def write_compact(c, table, prop, start, end, rows):
    """Replace one property's rows between two dates. rows: dicts with the view's column names."""
    pid = ids(c, "property", [prop])[prop]
    if table == "gsc_pages":
        pages, devs = ids(c, "gsc_page", [r["page"] for r in rows]), ids(c, "device", [r["device"] for r in rows])
        c.execute("DELETE FROM gsc_pages_c WHERE prop = ? AND date BETWEEN ? AND ?", (pid, start, end))
        c.executemany("INSERT OR REPLACE INTO gsc_pages_c VALUES (?, ?, ?, ?, ?, ?, ?)",
                      [(pid, r["date"], pages[str(r["page"])], devs[str(r["device"])], r["clicks"], r["impressions"],
                        r["position"]) for r in rows])
    elif table == "gsc_queries":
        qs, pages = ids(c, "gsc_query", [r["query"] for r in rows]), ids(c, "gsc_page", [r["page"] for r in rows])
        devs = ids(c, "device", [r["device"] for r in rows])
        c.execute("DELETE FROM gsc_queries_c WHERE prop = ? AND date BETWEEN ? AND ?", (pid, start, end))
        c.executemany("INSERT OR REPLACE INTO gsc_queries_c VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      [(pid, r["date"], qs[str(r["query"])], pages[str(r["page"])], devs[str(r["device"])],
                        r["clicks"], r["impressions"], r["position"]) for r in rows])
    elif table == "ga4_landing":
        merged = {}  # action links fold into one line per day and channel; sums stay the same
        for r in rows:
            key = (r["date"], action_link(r["landing_page"]), r["channel"])
            m = merged.setdefault(key, [0, 0, 0, 0, 0, 0])
            for i, f in enumerate(("sessions", "active_users", "engaged_sessions", "key_events", "purchases", "revenue")):
                m[i] += r[f] or 0
        pages = ids(c, "ga4_page", [k[1] for k in merged])
        chans = ids(c, "channel", [k[2] for k in merged])
        c.execute("DELETE FROM ga4_landing_c WHERE prop = ? AND date BETWEEN ? AND ?", (pid, start, end))
        c.executemany("INSERT INTO ga4_landing_c VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      [(pid, d, pages[str(p)], chans[str(ch)], *m) for (d, p, ch), m in merged.items()])
    else:
        raise ValueError(table)


def compact_now(progress=lambda f, t: None):
    """One-off: move the three big Google tables into compact storage, check that every total matches,
    then shrink the file. A full copy is kept first as data/houston-before-compaction.db."""
    report = {"before_mb": round(db_path().stat().st_size / 1048576, 1)}
    safety = DATA / "houston-before-compaction.db"
    with closing(connect()) as c:
        if not any(_kind_of(c, n) == "table" for n in COMPACT_VIEWS):
            return {"skipped": "already compact"}
        progress(0.02, "Keeping a full copy first")
        if not safety.exists():
            with closing(sqlite3.connect(str(safety))) as dst:
                c.backup(dst)
        plans = {
            "gsc_pages": ("SUM(clicks), SUM(impressions)", "page", "gsc_page", [("device", "device")]),
            "gsc_queries": ("SUM(clicks), SUM(impressions)", "page", "gsc_page", [("device", "device"), ("query", "gsc_query")]),
            "ga4_landing": ("SUM(sessions), SUM(engaged_sessions), ROUND(SUM(revenue), 2)", "landing_page", "ga4_page",
                            [("channel", "channel")]),
        }
        for i, (name, (sums, page_col, page_kind, others)) in enumerate(plans.items()):
            if _kind_of(c, name) != "table":
                continue
            progress(0.1 + 0.25 * i, f"Reorganising {name}")
            before = c.execute(f"SELECT {sums} FROM {name}").fetchone()
            c.execute("INSERT OR IGNORE INTO dict (kind, value) SELECT DISTINCT 'property', COALESCE(property, '') FROM " + name)
            if name == "ga4_landing":
                cases = " OR ".join(f"landing_page LIKE '{p}%'" for p in ACTION_PREFIXES)
                page_expr = f"CASE WHEN {cases} THEN '{ACTION_LINKS}' ELSE landing_page END"
            else:
                page_expr = page_col
            c.execute(f"INSERT OR IGNORE INTO dict (kind, value) SELECT DISTINCT '{page_kind}', COALESCE({page_expr}, '') "
                      f"FROM {name}")
            page_expr = page_expr.replace(page_col, "t." + page_col)
            for col, kind in others:
                c.execute(f"INSERT OR IGNORE INTO dict (kind, value) SELECT DISTINCT '{kind}', COALESCE({col}, '') "
                          f"FROM {name}")
            joins = (f"JOIN dict dp ON dp.kind = 'property' AND dp.value = COALESCE(t.property, '') "
                     f"JOIN dict dg ON dg.kind = '{page_kind}' AND dg.value = COALESCE({page_expr}, '')")
            if name == "gsc_pages":
                c.execute(f"INSERT OR REPLACE INTO gsc_pages_c SELECT dp.id, t.date, dg.id, dd.id, t.clicks, t.impressions, "
                          f"t.position FROM gsc_pages t {joins} "
                          f"JOIN dict dd ON dd.kind = 'device' AND dd.value = COALESCE(t.device, '')")
            elif name == "gsc_queries":
                c.execute(f"INSERT OR REPLACE INTO gsc_queries_c SELECT dp.id, t.date, dq.id, dg.id, dd.id, t.clicks, "
                          f"t.impressions, t.position FROM gsc_queries t {joins} "
                          f"JOIN dict dd ON dd.kind = 'device' AND dd.value = COALESCE(t.device, '') "
                          f"JOIN dict dq ON dq.kind = 'gsc_query' AND dq.value = COALESCE(t.query, '')")
            else:
                c.execute(f"INSERT OR REPLACE INTO ga4_landing_c SELECT dp.id, t.date, dg.id, dc.id, SUM(t.sessions), "
                          f"SUM(t.active_users), SUM(t.engaged_sessions), SUM(t.key_events), SUM(t.purchases), "
                          f"SUM(t.revenue) FROM ga4_landing t {joins} "
                          f"JOIN dict dc ON dc.kind = 'channel' AND dc.value = COALESCE(t.channel, '') "
                          f"GROUP BY dp.id, t.date, dg.id, dc.id")
            c.execute(f"ALTER TABLE {name} RENAME TO {name}_old")
            c.execute(COMPACT_VIEWS[name])
            after = c.execute(f"SELECT {sums} FROM {name}").fetchone()
            if tuple(before) != tuple(after):
                c.rollback()
                raise RuntimeError(f"{name}: totals differ after reorganising ({before} vs {after}). "
                                   f"That table was left exactly as it was.")
            c.execute(f"DROP TABLE {name}_old")
            c.commit()
            report[name] = {"totals": list(after)}
        progress(0.85, "Shrinking the file")
    report.update(shrink())
    report["after_mb"] = round(db_path().stat().st_size / 1048576, 1)
    progress(1.0, "Done")
    return report


def shrink():
    """Rewrite the file without empty space. Retried at the next start if something holds the database."""
    with closing(connect()) as c:
        try:
            c.execute("VACUUM")
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.execute("DELETE FROM settings WHERE key = 'shrink_pending'")
            c.commit()
            return {}
        except sqlite3.OperationalError as e:
            c.execute("INSERT OR REPLACE INTO settings VALUES ('shrink_pending', '1')")
            c.commit()
            return {"shrink": f"not done yet ({e}); Houston tries again at the next start"}


def setting(key, default=None):
    with closing(connect()) as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with closing(connect()) as c, c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))


def guess_backup_dir():
    """The usual Google Drive for desktop folders on Windows, first one that exists."""
    candidates = [Path(f"{d}:/My Drive") for d in "GHIJ"] + [Path(f"{d}:/Drive-ul meu") for d in "GHIJ"] + \
                 [Path.home() / "Google Drive" / "My Drive", Path.home() / "Google Drive", Path.home() / "My Drive"]
    for p in candidates:
        try:
            if p.is_dir():
                return str(p / "Houston backups")
        except OSError:
            continue
    return ""


def backup_now(dest_dir, keep=8):
    """A clean snapshot of the database (plus push backups, uploads and the work log), zipped into dest_dir.
    Keeps the newest `keep` backups there. Never includes secrets/."""
    import shutil
    import zipfile
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    snap = DATA / f"snapshot_{stamp}.db"
    with closing(connect()) as src, closing(sqlite3.connect(str(snap))) as dst:
        src.backup(dst)                      # a consistent copy, even while Houston or the MCP are working
    local_zip = DATA / f"houston-backup-{stamp}.zip"
    with zipfile.ZipFile(local_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.write(snap, "data/houston.db")
        for folder in (BACKUPS, UPLOADS):
            for f in folder.glob("*") if folder.exists() else []:
                z.write(f, f"data/{folder.name}/{f.name}")
        worklog = Path(__file__).parent / "notes" / "worklog.md"
        if worklog.exists():
            z.write(worklog, "notes/worklog.md")
    snap.unlink()
    final = dest / local_zip.name
    shutil.move(str(local_zip), str(final))   # Drive only ever sees the finished file
    old = sorted(dest.glob("houston-backup-*.zip"))[:-keep]
    for f in old:
        f.unlink()
    size = round(final.stat().st_size / 1048576, 1)
    set_setting("last_backup", f"{now()}|{final}|{size}")
    return {"file": str(final), "mb": size, "removed_old": len(old)}


def last_backup():
    v = setting("last_backup")
    if not v:
        return None
    at, path, mb = v.split("|")
    return {"at": at, "file": path, "mb": float(mb)}


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _migrate_json(c):
    """One-off: move the old JSON files into the database, then rename them *.old."""
    moved = []
    if CACHE.exists() and not c.execute("SELECT 1 FROM meta WHERE key = 'loaded_at'").fetchone():
        cache = _read_json(CACHE, None)
        if cache:
            _save_cache(c, cache)
            moved.append(CACHE)
    if PENDING.exists() and not c.execute("SELECT 1 FROM pending LIMIT 1").fetchone():
        _save_pending(c, _read_json(PENDING, []))
        moved.append(PENDING)
    if LOG.exists() and not c.execute("SELECT 1 FROM change_log LIMIT 1").fetchone():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            if line.strip():
                _insert_log(c, json.loads(line))
        moved.append(LOG)
    c.commit()
    for p in moved:
        p.replace(p.with_name(p.name + ".old"))


def clean_options(opts):
    """Magento can return option groups (value = list of sub-options) or list labels. Flatten to plain text."""
    out = []
    for o in opts or []:
        if not isinstance(o, dict):
            continue
        value, label = o.get("value"), o.get("label")
        if isinstance(value, (list, tuple)) and value and all(isinstance(x, dict) for x in value):
            group = as_text(label)
            for sub in clean_options(value):
                out.append({"value": sub["value"], "label": f"{group} › {sub['label']}" if group else sub["label"]})
            continue
        value, label = as_text(value), as_text(label)
        if value != "":
            out.append({"value": value, "label": label or value})
    return out


def _s(v):
    """Anything SQLite can store: lists and dicts become JSON text."""
    if v is None or isinstance(v, (int, float)):
        return v
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict, tuple)) else str(v)


def _txt(v):
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else ("" if v is None else str(v))


def _save_cache(c, cache):
    for t in CATALOGUE_TABLES:
        c.execute(f"DELETE FROM {t}")
    c.executemany("INSERT INTO meta VALUES (?, ?)", [("loaded_at", cache.get("loaded_at", "")),
                                                     ("live_only", "1" if cache.get("live_only") else "0")])
    attrs = cache.get("attributes", {})
    for a in attrs.values():  # older copies may still hold option groups or list labels
        if a.get("options") is not None:
            a["options"] = clean_options(a["options"])
    c.executemany("INSERT INTO attributes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  [tuple(_s(x) for x in (k, a.get("id"), a.get("type"), a.get("scope"), a.get("required"), a.get("label"),
                                         a.get("label_error"))) + (1 if a.get("options") is not None else 0,)
                   for k, a in attrs.items()])
    c.executemany("INSERT OR REPLACE INTO attribute_options VALUES (?, ?, ?, ?)",
                  [(_s(k), i, _s(o["value"]), _s(o["label"])) for k, a in attrs.items() for i, o in enumerate(a.get("options") or [])])
    c.executemany("INSERT INTO attribute_sets VALUES (?, ?)", [(_s(k), _s(v)) for k, v in cache.get("sets", {}).items()])
    c.executemany("INSERT OR REPLACE INTO set_attributes VALUES (?, ?, ?)",
                  [(_s(s), i, _s(code)) for s, codes in cache.get("set_attributes", {}).items() for i, code in enumerate(codes)])
    prods, values, cats = [], [], []
    for sku, r in cache.get("products", {}).items():
        prods.append((sku, r.get("_product_id"), r.get("_set"), r.get("_type"), r.get("status"), r.get("visibility"),
                      as_text(r.get("name")), as_text(r.get("meta_title")), as_text(r.get("meta_description")),
                      as_text(r.get("url_key")), as_text(r.get("price")), r.get("_qty"), r.get("_is_in_stock"), r.get("_error")))
        values += [(sku, k, _txt(v), 1 if isinstance(v, (list, dict)) else 0) for k, v in r.items() if not k.startswith("_")]
        cats += [(sku, str(x)) for x in r.get("_categories", [])]
    c.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  [tuple(_s(x) for x in row) for row in prods])
    c.executemany("INSERT INTO product_values VALUES (?, ?, ?, ?)", values)
    c.executemany("INSERT OR IGNORE INTO product_categories VALUES (?, ?)", [(_s(a), _s(b)) for a, b in cats])
    rows = []

    def walk(node, parent, depth, trail, pos):
        if not isinstance(node, dict):
            return
        name = norm(node.get("name"))
        rows.append((norm(node.get("category_id")), parent, name, depth, " › ".join(trail + [name]), pos))
        for i, ch in enumerate(node.get("children") or []):
            walk(ch, norm(node.get("category_id")), depth + 1, trail + [name] if depth > 0 else trail, i)

    walk(cache.get("tree") or {}, "", 0, [], 0)
    c.executemany("INSERT OR REPLACE INTO categories VALUES (?, ?, ?, ?, ?, ?)", rows)
    c.execute("INSERT INTO category_tree VALUES (1, ?)", (json.dumps(cache.get("tree"), ensure_ascii=False),))


def load_cache():
    with closing(connect()) as c:
        if not c.execute("SELECT 1 FROM meta WHERE key = 'loaded_at'").fetchone():
            return None
        meta = dict(c.execute("SELECT key, value FROM meta"))
        attrs = {}
        for code, aid, typ, scope, req, label, err, has_opts in c.execute("SELECT * FROM attributes"):
            a = {"id": aid, "code": code, "type": typ, "scope": scope, "required": req, "label": label or code}
            if err:
                a["label_error"] = err
            if has_opts:
                a["options"] = []
            attrs[code] = a
        for code, _, value, label in c.execute("SELECT * FROM attribute_options ORDER BY code, position"):
            attrs.setdefault(code, {"code": code}).setdefault("options", []).append({"value": value, "label": label})
        set_attrs = {}
        for s, _, code in c.execute("SELECT * FROM set_attributes ORDER BY set_id, position"):
            set_attrs.setdefault(s, []).append(code)
        products = {}
        for (sku, pid, set_id, typ, *_rest, qty, stock, err) in c.execute("SELECT * FROM products"):
            r = {"_error": err} if err else {"_product_id": pid or "", "_set": set_id or "", "_type": typ or "", "_categories": []}
            if qty is not None:
                r["_qty"] = qty
            if stock is not None:
                r["_is_in_stock"] = stock
            products[sku] = r
        for sku, code, value, is_json in c.execute("SELECT * FROM product_values"):
            if sku in products:
                products[sku][code] = json.loads(value) if is_json else value
        for sku, cid in c.execute("SELECT * FROM product_categories ORDER BY sku, category_id"):
            if sku in products and "_categories" in products[sku]:
                products[sku]["_categories"].append(cid)
        tree = c.execute("SELECT tree FROM category_tree WHERE id = 1").fetchone()
        return {"loaded_at": meta.get("loaded_at", ""), "live_only": meta.get("live_only") == "1",
                "attributes": attrs, "set_attributes": set_attrs, "sets": dict(c.execute("SELECT * FROM attribute_sets")),
                "products": products, "tree": json.loads(tree[0]) if tree else {}}


def save_cache(cache):
    with closing(connect()) as c, c:
        _save_cache(c, cache)


def _save_pending(c, items):
    c.execute("DELETE FROM pending")
    c.executemany("INSERT OR REPLACE INTO pending VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  [(i, p["id"], p.get("created"), p.get("kind"), p.get("target"), p.get("field"),
                    _txt(p.get("old")), _txt(p.get("new")), json.dumps(p, ensure_ascii=False)) for i, p in enumerate(items)])


def load_pending():
    with closing(connect()) as c:
        return [json.loads(row[0]) for row in c.execute("SELECT change FROM pending ORDER BY seq")]


def save_pending(items):
    with closing(connect()) as c, c:
        _save_pending(c, items)


def read_log():
    with closing(connect()) as c:
        return [json.loads(row[0]) for row in c.execute("SELECT entry FROM change_log ORDER BY seq")]


def _insert_log(c, e):
    c.execute("INSERT INTO change_log (id, pushed_at, kind, target, target_label, field, old, new, status, note, backup, entry)"
              " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (e.get("id"), e.get("pushed_at"), e.get("kind"), e.get("target"), e.get("target_label"), e.get("field"),
               _txt(e.get("old")), _txt(e.get("new")), e.get("status"), e.get("note"), e.get("backup"),
               json.dumps(e, ensure_ascii=False)))


def _append_log(entry):
    with closing(connect()) as c, c:
        _insert_log(c, entry)


# --- loading the catalogue -----------------------------------------------------
def load_catalogue(mg, live_only=True, progress=lambda f, t: None):
    state = {"f": 0.02}
    mg.on_wait = lambda secs: progress(state["f"], f"The store asked to slow down, waiting {secs} s")
    progress(0.02, "Reading attribute sets")
    sets = mg.attribute_sets()
    set_attrs, attrs = {}, {}
    for s in sets:
        sid = str(s["set_id"])
        lst = mg.set_attributes(s["set_id"])
        set_attrs[sid] = [a["code"] for a in lst]
        for a in lst:
            attrs.setdefault(a["code"], {"id": str(a.get("attribute_id")), "code": a["code"],
                                         "type": a.get("type") or "text", "scope": a.get("scope", ""),
                                         "required": norm(a.get("required")),
                                         "label": FALLBACK_LABELS.get(a["code"], a["code"])})

    progress(0.08, "Reading attribute labels")
    for chunk in chunks(attrs, 40):
        for code, info in zip(chunk, mg.attribute_info_many(chunk)):
            if not isinstance(info, dict):
                attrs[code]["label_error"] = str(info) if isinstance(info, Exception) else f"unexpected answer: {type(info).__name__}"
                continue
            labels = info.get("frontend_label") or []
            if isinstance(labels, str):
                labels = [{"store_id": "0", "label": labels}]
            label = next((l.get("label") for l in labels if isinstance(l, dict) and str(l.get("store_id")) == "0" and l.get("label")), None)
            label = label or next((l.get("label") for l in labels if isinstance(l, dict) and l.get("label")), None)
            if label:
                attrs[code]["label"] = label
            else:
                attrs[code]["label_error"] = "the store returned no label"

    progress(0.12, "Reading dropdown options")
    selects = [c for c, a in attrs.items() if a["type"] in ("select", "multiselect", "boolean")]
    for chunk in chunks(selects, 40):
        for code, opts in zip(chunk, mg.attribute_options_many([attrs[c]["id"] for c in chunk])):
            if isinstance(opts, list):
                attrs[code]["options"] = clean_options(opts)

    progress(0.15, "Listing products")
    plist = mg.product_list({"status": {"eq": "1"}} if live_only else {})
    items = [(p["sku"], set_attrs.get(str(p.get("set")), [])) for p in plist]
    products = {}
    batches = chunks(items, 20)
    for i, batch in enumerate(batches):
        for (sku, _), info in zip(batch, mg.products_info(batch)):
            products[sku] = {"_error": str(info)} if isinstance(info, Exception) else _product_record(info)
        state["f"] = 0.15 + 0.75 * (i + 1) / max(len(batches), 1)
        progress(state["f"], f"Reading products {min((i + 1) * 20, len(items))} of {len(items)}")

    progress(0.92, "Reading stock")
    for chunk in chunks(products, 100):
        for s in mg.stock(chunk):
            rec = products.get(s.get("sku"))
            if rec is not None:
                rec["_qty"], rec["_is_in_stock"] = norm(s.get("qty")), norm(s.get("is_in_stock"))

    progress(0.97, "Reading categories")
    tree = mg.category_tree()
    cache = {"loaded_at": now(), "live_only": live_only, "attributes": attrs, "set_attributes": set_attrs,
             "sets": {str(s["set_id"]): s.get("name", "") for s in sets}, "products": products, "tree": tree}
    save_cache(cache)
    mg.on_wait = None
    progress(1.0, "Done")
    return cache


def _product_record(info):
    rec = {k: (norm(v) if not isinstance(v, (list, tuple)) else norm(v)) for k, v in info.items()}
    rec["_product_id"] = norm(info.get("product_id"))
    rec["_set"] = norm(info.get("set"))
    rec["_type"] = norm(info.get("type"))
    rec["_categories"] = norm(info.get("categories") or [])
    return rec


def flat_categories(tree):
    """[(id, name, depth, path_label)] in tree order."""
    out = []

    def walk(node, depth, trail):
        if not isinstance(node, dict):
            return
        name = norm(node.get("name"))
        label = " › ".join(trail + [name]) if trail else name
        out.append((norm(node.get("category_id")), name, depth, label))
        for ch in node.get("children") or []:
            walk(ch, depth + 1, trail + [name] if depth > 0 else trail)

    walk(tree, 0, [])
    return out


# --- checks --------------------------------------------------------------------
def invisible(text):
    return sum(1 for ch in str(text) if (ord(ch) < 32 and ch not in "\n\r\t") or 127 <= ord(ch) < 160)


def check(ch, attrs=None):
    """Returns (status, note). status: Passed / Warning / Blocked."""
    attrs = attrs or {}
    kind, field, new, old = ch["kind"], ch.get("field", ""), ch.get("new", ""), ch.get("old", "")
    if invisible(new):
        return "Blocked", "contains invisible characters"
    if kind == "product" and field in LOCKED_PRODUCT:
        return "Blocked", f"{field} is locked"
    if kind == "category" and field in LOCKED_CATEGORY:
        return "Blocked", f"{field} is locked"
    if kind == "product" and attrs.get(field, {}).get("type") in NUMERIC_TYPES and new != "":
        try:
            float(str(new).replace(",", "."))
        except ValueError:
            return "Blocked", "not a number"
    if kind == "stock" and field == "qty":
        try:
            float(str(new))
        except ValueError:
            return "Blocked", "not a number"
    if field in TEXT_FIELDS_LAPTE and re.search(r"\blapte\b", str(new), re.I):
        return "Warning", "contains 'lapte' — not allowed for plant-based drinks"
    if kind in ("product", "category") and old != "" and new == "":
        return "Warning", "clears the current value"
    if kind in ("image_remove", "cat_remove", "link_remove"):
        return "Warning", "removes something"
    return "Passed", ""


def save_upload(name, content_bytes):
    UPLOADS.mkdir(parents=True, exist_ok=True)
    path = UPLOADS / f"{uuid.uuid4().hex[:8]}_{name}"
    path.write_bytes(content_bytes)
    return path.name


def new_change(kind, target, field="", old="", new="", target_label="", **extra):
    return {"id": uuid.uuid4().hex[:10], "kind": kind, "target": target, "target_label": target_label,
            "field": field, "old": old, "new": new, "extra": extra, "created": now()}


def queue(changes):
    """Add changes to the queue, replacing any pending change to the same thing."""
    pending = load_pending()
    keyf = lambda c: (c["kind"], c["target"], c.get("field"), json.dumps(c.get("extra", {}).get("key")))
    keys = {keyf(c) for c in changes}
    kept = {keyf(p): p["prediction"] for p in pending if p.get("prediction")}
    for c in changes:                      # a change queued again keeps the prediction written for it
        if not c.get("prediction") and keyf(c) in kept:
            c["prediction"] = kept[keyf(c)]
    pending = [p for p in pending if keyf(p) not in keys] + changes
    save_pending(pending)
    return len(changes)


def product_diff(sku, rec, edits, attrs):
    """edits: {code: new_value_as_stored}. Returns change dicts for real differences."""
    out = []
    for code, new in edits.items():
        old = rec.get(code, "")
        if norm(new) == norm(old):
            continue
        a = attrs.get(code, {})
        out.append(new_change("product", sku, code, norm(old), norm(new), rec.get("name", ""),
                              old_label=option_label(a, old), new_label=option_label(a, new)))
    return out


def option_label(attr, value):
    opts = attr.get("options")
    if not opts:
        return ""
    ids = str(value).split(",") if attr.get("type") == "multiselect" else [str(value)]
    m = {o["value"]: o["label"] for o in opts}
    return ", ".join(m.get(i, i) for i in ids if i != "")


# --- push ----------------------------------------------------------------------
def push(mg, cache, changes, progress=lambda f, t: None):
    """Writes changes, verifies each one, logs it. Returns list of (change, status, note)."""
    if any(c.get("kind") == "winmentor" for c in changes):   # never written: they go to the operator's list
        import operator_requests
        rest = [c for c in changes if c.get("kind") != "winmentor"]
        done = push(mg, cache, rest, progress) if rest else []
        return done + operator_requests.send([c for c in changes if c.get("kind") == "winmentor"])
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    BACKUPS.mkdir(parents=True, exist_ok=True)
    backup, results = {"pushed_at": now(), "products": {}, "categories": {}, "other": []}, []
    products = cache["products"] if cache else {}
    attrs = cache["attributes"] if cache else {}
    by_sku = {}
    for c in changes:
        if c["kind"] == "product":
            by_sku.setdefault(c["target"], []).append(c)
    others = [c for c in changes if c["kind"] != "product"]
    total, done = len(by_sku) + len(others), 0

    pending_by_id = {p["id"]: p for p in load_pending()}
    skus = list(by_sku)
    for batch in chunks(skus, PUSH_BATCH):
        codes = {s: ((cache or {}).get("set_attributes", {}).get(products.get(s, {}).get("_set", ""), [])
                     or [c["field"] for c in by_sku[s]]) for s in batch}
        befores = mg.multi([("catalog_product.info", [s, "0", codes[s], "sku"]) for s in batch])
        to_write = {}
        for s, before in zip(batch, befores):
            if isinstance(before, Exception):
                results += [(c, "Failed", str(before)) for c in by_sku[s]]
                continue
            before = {k: norm(v) for k, v in before.items()}
            backup["products"][s] = before
            ok_changes = []
            for c in by_sku[s]:
                a = attrs.get(c["field"], {})
                if not _same(before.get(c["field"]), c["old"], a):
                    now_val = before.get(c["field"], "")
                    results.append((c, "Skipped", f"changed in the store since your copy: now {now_val!r}. "
                                                  "Review it again before pushing"))
                    p = pending_by_id.get(c["id"])
                    if p:
                        p["old"] = now_val
                        p.setdefault("extra", {})["old_label"] = option_label(a, now_val)
                    if s in products:
                        products[s][c["field"]] = now_val
                else:
                    ok_changes.append(c)
            if ok_changes:
                to_write[s] = (before, ok_changes)
        _write_json(BACKUPS / f"push_{stamp}.json", backup)
        if not to_write:
            done += len(batch)
            progress(done / max(total, 1), f"Pushed {done} of {total}")
            continue
        order = list(to_write)
        writes = mg.multi([("catalog_product.update", [s, {c["field"]: _api_value(c, attrs) for c in to_write[s][1]}, "0", "sku"])
                           for s in order])
        written = [s for s, w in zip(order, writes) if not isinstance(w, Exception)]
        for s, w in zip(order, writes):
            if isinstance(w, Exception):
                results += [(c, "Failed", str(w)) for c in to_write[s][1]]
        afters = mg.multi([("catalog_product.info", [s, "0", codes[s], "sku"]) for s in written]) if written else []
        for s, after in zip(written, afters):
            before, group = to_write[s]
            if isinstance(after, Exception):
                results += [(c, "Check", f"written, but reading it back failed: {after}") for c in group]
                continue
            after = {k: norm(v) for k, v in after.items()}
            changed = {c["field"] for c in group}
            surprises = [k for k in set(before) | set(after)
                         if k not in changed and k not in IGNORE_ON_COMPARE and before.get(k) != after.get(k)
                         and not (k.endswith("_label") and before.get(k, "").strip() == "" and after.get(k, "") == "")]
            for c in group:
                ok = _same(after.get(c["field"]), c["new"], attrs.get(c["field"], {}))
                status = "Done" if ok and not surprises else ("Check" if ok else "Failed")
                note = "" if ok else f"store now shows {after.get(c['field'])!r}"
                if surprises:
                    note = (note + "; " if note else "") + "also changed: " + ", ".join(sorted(surprises))
                results.append((c, status, note))
            if s in products:
                products[s].update(after)
        done += len(batch)
        progress(done / max(total, 1), f"Pushed {done} of {total}")

    for c in others:
        try:
            status, note = _push_other(mg, cache, c, backup, stamp)
        except ApiError as e:
            status, note = "Failed", str(e)
        results.append((c, status, note))
        done += 1
        progress(done / max(total, 1), f"Pushed {done} of {total}")

    for c, status, note in results:
        if status != "Skipped":
            _append_log({**c, "pushed_at": now(), "status": status, "note": note, "backup": f"push_{stamp}.json"})
    pushed_ids = {c["id"] for c, s, _ in results if s not in ("Failed", "Skipped")}
    save_pending([p for p in pending_by_id.values() if p["id"] not in pushed_ids] +
                 [p for p in load_pending() if p["id"] not in pending_by_id])
    if cache:
        save_cache(cache)
    return results


def _api_value(c, attrs):
    a = attrs.get(c["field"], {})
    v = c["new"]
    if a.get("type") in NUMERIC_TYPES and v != "":
        return str(v).replace(",", ".")
    return v


def _same(store_value, wanted, attr):
    a, b = norm(store_value), norm(wanted)
    if attr.get("type") in NUMERIC_TYPES or attr.get("type") == "qty":
        try:
            return abs(float(a or 0) - float(str(b or 0).replace(",", "."))) < 1e-6
        except ValueError:
            return a == b
    if attr.get("type") == "multiselect":
        return sorted(x for x in a.split(",") if x) == sorted(x for x in b.split(",") if x)
    return a == b


def _snapshot_other(mg, c):
    k, t, e = c["kind"], c["target"], c.get("extra", {})
    if k == "stock":
        return {"stock": mg.stock([t])}
    if k in ("cat_assign", "cat_remove"):
        return {"category_products": mg.category_products(e["category_id"])}
    if k in ("link_add", "link_remove"):
        return {"links": mg.links(c["field"], t)}
    if k.startswith("image_"):
        return {"media": mg.media(t)}
    if k == "option_add":
        return {"options": mg.call("catalog_product_attribute.options", [t, "0"])}
    return {}


def _push_other(mg, cache, c, backup, stamp):
    k, t, e = c["kind"], c["target"], c.get("extra", {})
    if k != "category":
        backup["other"].append({"change": c["id"], "kind": k, "target": t, "before": _snapshot_other(mg, c)})
        _write_json(BACKUPS / f"push_{stamp}.json", backup)
    if k == "stock":
        data = {c["field"]: (str(c["new"]) if c["field"] == "qty" else int(c["new"] or 0))}
        mg.update_stock(t, data)
        row = next((s for s in mg.stock([t]) if s.get("sku") == t), {})
        ok = _same(row.get(c["field"]), c["new"], {"type": "qty" if c["field"] == "qty" else ""})
        if ok and cache and t in cache["products"]:
            cache["products"][t]["_" + c["field"]] = norm(row.get(c["field"]))
        return ("Done", "") if ok else ("Failed", f"store now shows {row.get(c['field'])!r}")
    if k == "category":
        before = mg.category_info(t)
        backup["categories"][t] = {kk: norm(vv) for kk, vv in before.items() if not isinstance(vv, (list, dict))}
        _write_json(BACKUPS / f"push_{stamp}.json", backup)
        mg.category_update(t, {c["field"]: c["new"]})
        after = mg.category_info(t)
        ok = norm(after.get(c["field"])) == norm(c["new"])
        return ("Done", "") if ok else ("Failed", f"store now shows {after.get(c['field'])!r}")
    if k in ("cat_assign", "cat_remove"):
        cid = e["category_id"]
        (mg.category_assign if k == "cat_assign" else mg.category_remove)(cid, t)
        present = any(p.get("sku") == t for p in mg.category_products(cid))
        ok = present if k == "cat_assign" else not present
        if ok and cache and t in cache["products"]:
            cats = set(cache["products"][t].get("_categories", []))
            (cats.add if k == "cat_assign" else cats.discard)(str(cid))
            cache["products"][t]["_categories"] = sorted(cats)
        return ("Done", "") if ok else ("Failed", "category list did not change")
    if k == "cat_create":
        data = {"name": c["new"], "is_active": 1, "include_in_menu": 1,
                "available_sort_by": ["position"], "default_sort_by": "position"}
        new_id = mg.category_create(t, data)
        ok = norm(mg.category_info(new_id).get("name")) == c["new"]
        c["extra"]["new_category_id"] = norm(new_id)
        return ("Done", f"new category ID {new_id}") if ok else ("Failed", "category not found after creating")
    if k in ("link_add", "link_remove"):
        (mg.link_assign if k == "link_add" else mg.link_remove)(c["field"], t, c["new"] or c["old"])
        linked = {norm(x.get("sku")) for x in mg.links(c["field"], t)}
        ok = (c["new"] in linked) if k == "link_add" else (c["old"] not in linked)
        return ("Done", "") if ok else ("Failed", "link list did not change")
    if k == "image_update":
        mg.media_update(t, e["file"], e["data"])
        img = next((m for m in mg.media(t) if m.get("file") == e["file"]), {})
        ok = all(norm(img.get(kk)) == norm(vv) for kk, vv in e["data"].items())
        return ("Done", "") if ok else ("Check", "image saved; some values differ on read-back")
    if k == "image_add":
        import base64
        raw = (UPLOADS / e["upload"]).read_bytes()
        data = dict(e["data"], file=dict(e["data"]["file"], content=base64.b64encode(raw).decode()))
        file = mg.media_create(t, data)
        c["extra"]["file"] = file
        ok = any(m.get("file") == file for m in mg.media(t))
        return ("Done", f"file {file}") if ok else ("Failed", "image not found after upload")
    if k == "image_remove":
        mg.media_remove(t, e["file"])
        ok = not any(m.get("file") == e["file"] for m in mg.media(t))
        return ("Done", "") if ok else ("Failed", "image still there")
    if k == "option_add":
        mg.add_option(t, c["new"])
        attr = (cache or {}).get("attributes", {}).get(t, {})
        opts = mg.attribute_options_many([attr.get("id", t)])[0]
        ok = isinstance(opts, list) and any(o["label"] == c["new"] for o in clean_options(opts))
        if ok and attr:
            attr["options"] = clean_options(opts)
        return ("Done", "") if ok else ("Failed", "option not found after adding")
    return "Failed", f"unknown change type {k}"


# --- undo ----------------------------------------------------------------------
UNDO = {"product": "product", "stock": "stock", "category": "category", "cat_assign": "cat_remove",
        "cat_remove": "cat_assign", "link_add": "link_remove", "link_remove": "link_add", "image_update": "image_update"}


def undo_change(entry):
    k = entry["kind"]
    if k not in UNDO or entry.get("status") == "Failed":
        return None
    extra = dict(entry.get("extra", {}))
    extra["old_label"], extra["new_label"] = extra.get("new_label", ""), extra.get("old_label", "")
    if k == "image_update":
        if "old_data" not in extra:
            return None
        extra["data"], extra["old_data"] = extra["old_data"], extra["data"]
    return new_change(UNDO[k], entry["target"], entry.get("field", ""), entry.get("new", ""), entry.get("old", ""),
                      entry.get("target_label", ""), **{kk: vv for kk, vv in extra.items() if kk not in ("key", "undo_of")},
                      key=extra.get("key"), undo_of=entry["id"])
