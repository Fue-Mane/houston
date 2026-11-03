"""Houston MCP server for Claude Desktop.

Read-only access to Houston's database. The only thing it can write is the work log, notes/worklog.md,
and every write keeps a backup in notes/backups/.
"""
import datetime
import re
import shutil
import statistics
from contextlib import closing
from pathlib import Path

import core
from mcp.server.mcpserver import MCPServer

core.READ_ONLY = True  # this server never writes: the database is opened read-only
mcp = MCPServer("houston")

TABLE_NOTES = {
    "meta": "When the catalogue copy was loaded (loaded_at) and whether disabled products were included.",
    "products": "One row per product with the main fields. status 1 = live, 2 = disabled.",
    "product_values": "Every attribute of every product, one row per product and attribute code. "
                      "Dropdown values are option IDs: join attribute_options on (code, value) for names.",
    "attributes": "Attribute definitions: code, name (label), type, scope.",
    "attribute_options": "Dropdown options: attribute code, option ID (value) and its name (label).",
    "attribute_sets": "Magento attribute sets.", "set_attributes": "Which attributes each set has.",
    "categories": "Category tree, one row per category, with the full path.",
    "product_categories": "Which products are in which categories.",
    "category_tree": "The raw tree Houston uses (JSON).",
    "pending": "Changes waiting in Houston's Review and push.",
    "change_log": "Every change pushed from Houston, with result (Done/Check/Failed) and backup file.",
    "gsc_totals": "Search Console, whole site per day (web search). Includes anonymised queries, so it matches "
                  "the Search Console graph. position = average position that day.",
    "gsc_pages": "View. Search Console per day, page and device (DESKTOP/MOBILE/TABLET). Includes anonymised queries. "
                 "ctr is clicks/impressions.",
    "gsc_queries": "View. Search Console per day, query, page and device. Google leaves out anonymised queries here, "
                   "so sums are lower than gsc_pages/gsc_totals. Weight position by impressions when aggregating.",
    "gsc_pulls": "Every Search Console pull: when it ran, which table, the date range and row count.",
    "ga4_totals": "Google Analytics 4, whole site per day: sessions, active users, engaged sessions, key events, "
                  "purchases, revenue (lei). Users can't be summed across days into a period total.",
    "dict": "Internal: every page address, search, device, channel and property stored once (kind, value). "
            "The *_c tables point to it by id.",
    "gsc_pages_c": "Internal compact storage behind the gsc_pages view (ids instead of text). For heavy questions, "
                   "query this joined to dict instead of the view.",
    "gsc_queries_c": "Internal compact storage behind the gsc_queries view (ids instead of text). For heavy questions, "
                     "aggregate here first and join dict only for the rows you return.",
    "ga4_landing_c": "Internal compact storage behind the ga4_landing view (ids instead of text).",
    "settings": "Houston settings, e.g. the backup folder and the last backup.",
    "ga4_landing": "View. GA4 per day, landing page (path, e.g. /produs.html) and channel (Organic Search, Direct, ...). "
                   "Join to products on url_key: landing_page = '/' || url_key || '.html'. Visits that started on "
                   "Magento compare/wishlist/stock-alert/send-to-friend links are one row per day and channel with "
                   "landing_page = '(Magento action links)' (months pulled before 9 Oct 2026 may still hold them "
                   "one URL per row).",
    "ga4_pull_errors": "Every GA4 request Google refused: when, which table, which dates, and Google's message.",
    "ga4_items": "GA4 per day and product as the shop sends it (item_id, item_name): views, add-to-carts, "
                 "purchases, item revenue. item_id is the Magento product ID: join products on product_id.",
    "ga4_site_search": "GA4 per day: searches typed into the shop's own search box (view_search_results).",
    "ga4_pulls": "Every GA4 pull: when it ran, which table, the date range and row count.",
}

from winmentor import TABLE_NOTES as _WM_NOTES; TABLE_NOTES.update(_WM_NOTES)
try:  # changes made by hand (Catalogue > Change log, tab Made by hand): what the new tables hold
    from hand_changes import TABLE_NOTES as _HC_NOTES; TABLE_NOTES.update(_HC_NOTES)
except Exception:  # noqa: BLE001  (a description must never stop the server)
    pass

def _get_cat():
    try:
        c = core.load_cache()
    except FileNotFoundError as e:
        raise ValueError(str(e))
    if not c:
        raise ValueError("No catalogue copy yet: open Houston and press Load catalogue.")
    return c

def _cut(text, limit=300):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', str(text)).strip()
    return text[:limit] + "..." if len(text) > limit else text

def _paginate(items, limit, offset):
    limit = max(1, min(limit, 500))
    return {
        "items": items[offset:offset+limit],
        "total": len(items),
        "limit": limit,
        "offset": offset
    }

@mcp.tool()
def catalogue_info() -> dict:
    """Get general information about the local catalogue copy, such as when it was loaded and counts of products/categories."""
    try:
        cat = _get_cat()
    except ValueError as e:
        return {"error": str(e)}
    
    prods = cat.get("products", {})
    live = sum(1 for p in prods.values() if p.get("status") == "1")
    errors = sum(1 for p in prods.values() if "_error" in p)
    
    return {
        "catalogue_date": cat.get("loaded_at", "Unknown"),
        "live_only_loaded": cat.get("live_only", True),
        "total_products": len(prods),
        "live_products": live,
        "disabled_products": len(prods) - live - errors,
        "failed_reads": errors,
        "attribute_sets": len(cat.get("sets", {})),
        "attributes": len(cat.get("attributes", {})),
        "categories": len(core.flat_categories(cat.get("tree") or {})),
        "items_in_review_queue": len(core.load_pending())
    }

@mcp.tool()
def search_products(query: str = "", status: str = "live", category: str = "", attribute_set: str = "",
                    missing: list[str] = None, fields: list[str] = None, limit: int = 50, offset: int = 0) -> dict:
    """Find products in the local catalogue copy by name, SKU, status (live|disabled|all), category, attribute set, or missing fields.
    Fields to check for missing values: 'meta_title', 'meta_description', 'short_description', 'description'.
    Specify 'fields' to include extra attributes in the output.
    """
    try:
        cat = _get_cat()
    except ValueError as e:
        return {"error": str(e)}
    
    missing = missing or []
    fields = fields or []
    prods = cat["products"]
    attrs = cat["attributes"]
    
    # Resolve category
    cat_id = None
    if category:
        flat = core.flat_categories(cat.get("tree") or {})
        for cid, cname, _, clabel in flat:
            if category.lower() in clabel.lower() or category == str(cid):
                cat_id = str(cid)
                break
    
    # Resolve attribute set
    set_id = None
    if attribute_set:
        for sid, sname in cat.get("sets", {}).items():
            if attribute_set.lower() in sname.lower() or attribute_set == sid:
                set_id = sid
                break

    query = query.lower().strip()
    results = []
    
    for sku, r in prods.items():
        if "_error" in r:
            continue
        
        if query and query not in sku.lower() and query not in str(r.get("name", "")).lower():
            continue
            
        p_status = r.get("status", "0")
        if status == "live" and p_status != "1": continue
        if status == "disabled" and p_status == "1": continue
        
        if cat_id and cat_id not in r.get("_categories", []):
            continue
            
        if set_id and r.get("_set") != set_id:
            continue
            
        if missing:
            skip = False
            for m in missing:
                if str(r.get(m, "")).strip():
                    skip = True
                    break
            if skip: continue
            
        row = {
            "sku": sku,
            "name": str(r.get("name", "")),
            "status": "live" if p_status == "1" else "disabled",
            "price": str(r.get("price", "")),
            "qty": str(r.get("_qty", "")),
            "stock": "in_stock" if r.get("_is_in_stock", "0") == "1" else "out_of_stock"
        }
        
        for f in fields:
            if f in r:
                a = attrs.get(f, {})
                val = r[f]
                if a.get("options") and val:
                    val = core.option_label(a, val)
                row[f] = _cut(val)
                
        results.append(row)
        
    out = _paginate(results, limit, offset)
    out["catalogue_date"] = cat.get("loaded_at", "Unknown")
    return out

@mcp.tool()
def get_product(sku: str, include_empty: bool = False) -> dict:
    """Get every attribute, category path, stock, and attribute set of one specific product by SKU."""
    try:
        cat = _get_cat()
    except ValueError as e:
        return {"error": str(e)}
        
    prods = cat["products"]
    if sku not in prods:
        return {"error": f"SKU '{sku}' not found in catalogue copy."}
        
    r = prods[sku]
    if "_error" in r:
        return {"error": f"Failed to read from store: {r['_error']}"}
        
    attrs = cat["attributes"]
    set_name = cat.get("sets", {}).get(r.get("_set", ""), r.get("_set", "Unknown"))
    
    flat = core.flat_categories(cat.get("tree") or {})
    cat_names = {str(cid): clabel for cid, _, _, clabel in flat}
    p_cats = [cat_names.get(c, c) for c in r.get("_categories", [])]
    
    details = {
        "sku": sku,
        "attribute_set": set_name,
        "stock_qty": r.get("_qty", ""),
        "stock_status": "in_stock" if r.get("_is_in_stock", "0") == "1" else "out_of_stock",
        "categories": p_cats,
        "attributes": {}
    }
    
    for k, v in r.items():
        if k.startswith("_"): continue
        if not include_empty and not str(v).strip(): continue
        
        a = attrs.get(k, {})
        label = a.get("label", k)
        opt_name = core.option_label(a, v) if a.get("options") and v else v
        
        details["attributes"][k] = {
            "label": label,
            "raw_value": v,
            "display_value": opt_name
        }
        
    return {"catalogue_date": cat.get("loaded_at", "Unknown"), "product": details}

@mcp.tool()
def list_categories() -> dict:
    """List the category tree with ID, name, full path, and number of products assigned to each."""
    try:
        cat = _get_cat()
    except ValueError as e:
        return {"error": str(e)}
        
    flat = core.flat_categories(cat.get("tree") or {})
    prods = cat["products"]
    
    counts = {}
    for r in prods.values():
        if "_error" in r: continue
        for c in r.get("_categories", []):
            counts[c] = counts.get(c, 0) + 1
            
    res = []
    for cid, name, depth, path in flat:
        res.append({
            "id": cid,
            "name": name,
            "path": path,
            "product_count": counts.get(str(cid), 0)
        })
        
    return {"catalogue_date": cat.get("loaded_at", "Unknown"), "categories": res}

@mcp.tool()
def list_attributes() -> dict:
    """List all product attributes with their code, label, type, and number of available options."""
    try:
        cat = _get_cat()
    except ValueError as e:
        return {"error": str(e)}
        
    res = []
    for k, a in cat["attributes"].items():
        res.append({
            "code": k,
            "label": a.get("label", k),
            "type": a.get("type", "text"),
            "scope": a.get("scope", ""),
            "options_count": len(a.get("options", [])) if a.get("options") else 0
        })
        
    return {"catalogue_date": cat.get("loaded_at", "Unknown"), "attributes": res}

@mcp.tool()
def attribute_values(code: str, limit: int = 50) -> dict:
    """Get the available options for a dropdown attribute, or the most common values for a text attribute."""
    try:
        cat = _get_cat()
    except ValueError as e:
        return {"error": str(e)}
        
    attrs = cat["attributes"]
    if code not in attrs:
        return {"error": f"Attribute '{code}' not found."}
        
    a = attrs[code]
    prods = cat["products"]
    
    counts = {}
    for r in prods.values():
        if "_error" in r: continue
        val = core.norm(r.get(code, ""))
        if a.get("type") == "multiselect":
            for v in val.split(","):
                if v: counts[v] = counts.get(v, 0) + 1
        else:
            if val: counts[val] = counts.get(val, 0) + 1
            
    res = []
    if a.get("options"):
        for o in a["options"]:
            res.append({
                "value": o["value"],
                "label": o["label"],
                "product_count": counts.get(o["value"], 0)
            })
    else:
        sorted_counts = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
        for val, count in sorted_counts:
            res.append({
                "value": val,
                "product_count": count
            })
            
    return {"catalogue_date": cat.get("loaded_at", "Unknown"), "attribute": a.get("label", code), "values": res}

@mcp.tool()
def content_audit(limit: int = 50) -> dict:
    """Run an SEO and content audit on all products: missing fields, duplicates, 'lapte', invisible characters,
    price and stock. Lengths of meta titles and descriptions are listed for information, not judged.
    Returns counts and sample SKUs for each issue."""
    try:
        cat = _get_cat()
    except ValueError as e:
        return {"error": str(e)}
        
    prods = cat["products"]
    
    issues = {
        "missing_meta_title": [],
        "missing_meta_desc": [],
        "missing_desc": [],
        "duplicate_name": {},
        "duplicate_meta_title": {},
        "duplicate_meta_desc": {},
        "contains_lapte_warning": [],
        "invisible_characters": [],
        "no_price": [],
        "out_of_stock": []
    }
    
    names = {}
    m_titles = {}
    m_descs = {}
    lengths = {"meta_title": [], "meta_description": []}
    
    for sku, r in prods.items():
        if "_error" in r: continue
        
        name = str(r.get("name", "")).strip()
        if name:
            names.setdefault(name.lower(), []).append(sku)
            
        mt = str(r.get("meta_title", "")).strip()
        if not mt: issues["missing_meta_title"].append(sku)
        else:
            m_titles.setdefault(mt.lower(), []).append(sku)
            lengths["meta_title"].append(len(mt))
            
        md = str(r.get("meta_description", "")).strip()
        if not md: issues["missing_meta_desc"].append(sku)
        else:
            m_descs.setdefault(md.lower(), []).append(sku)
            lengths["meta_description"].append(len(md))
            
        if not str(r.get("description", "")).strip(): issues["missing_desc"].append(sku)
        
        for f in core.TEXT_FIELDS_LAPTE:
            if re.search(r"\blapte\b", str(r.get(f, "")), re.I):
                issues["contains_lapte_warning"].append(sku)
                break
                
        for k, v in r.items():
            if core.invisible(v):
                issues["invisible_characters"].append(sku)
                break
                
        if not str(r.get("price", "")).strip() or str(r.get("price", "")) == "0.0000":
            issues["no_price"].append(sku)
            
        if r.get("_is_in_stock", "0") != "1" or float(r.get("_qty", 0) or 0) <= 0:
            issues["out_of_stock"].append(sku)
            
    for k, v in names.items():
        if len(v) > 1: issues["duplicate_name"][k] = v
    for k, v in m_titles.items():
        if len(v) > 1: issues["duplicate_meta_title"][k] = v
    for k, v in m_descs.items():
        if len(v) > 1: issues["duplicate_meta_desc"][k] = v
        
    summary = {}
    for k, v in issues.items():
        if isinstance(v, list):
            summary[k] = {"count": len(set(v)), "samples": list(set(v))[:limit]}
        else:
            # dictionary of duplicates
            dup_count = sum(len(skus) for skus in v.values())
            samples = {dup_k: skus for dup_k, skus in list(v.items())[:limit]}
            summary[k] = {"count_of_products_affected": dup_count, "groups_count": len(v), "samples": samples}
            
    summary["lengths_in_characters"] = {
        k: ({"min": min(v), "median": statistics.median(v), "max": max(v), "count": len(v)} if v else {})
        for k, v in lengths.items()}
    summary["note"] = ("Google sets no fixed length for titles or meta descriptions; it shortens them to fit the "
                       "screen. Lengths are listed for information, not as errors.")
    return {"catalogue_date": cat.get("loaded_at", "Unknown"), "audit_summary": summary}

@mcp.tool()
def review_queue() -> dict:
    """Read the queue of changes waiting in Houston's 'Review and push' page, including their Passed/Warning/Blocked status."""
    try:
        cat = _get_cat()
    except ValueError:
        cat = {}
        
    try:
        pending = core.load_pending()
    except FileNotFoundError as e:
        return {"error": str(e)}
    attrs = cat.get("attributes", {})
    
    res = []
    for ch in pending:
        status, note = core.check(ch, attrs)
        res.append({
            "id": ch["id"],
            "kind": ch["kind"],
            "target_sku_or_id": ch["target"],
            "target_name": ch.get("target_label", ""),
            "field": ch.get("field", ""),
            "from": ch.get("extra", {}).get("old_label") or ch.get("old", ""),
            "to": ch.get("extra", {}).get("new_label") or ch.get("new", ""),
            "status": status,
            "note": note,
            "queued_at": ch.get("created", "")
        })
        
    return {"catalogue_date": cat.get("loaded_at", "Unknown"), "queue_count": len(res), "queue": res}

@mcp.tool()
def change_log(sku: str = "", limit: int = 50) -> dict:
    """Read recent pushes from the change log, optionally filtered by a specific SKU."""
    try:
        log = core.read_log()
    except FileNotFoundError as e:
        return {"error": str(e)}
    log.reverse()
    
    try:
        cat = _get_cat()
    except ValueError:
        cat = {}
    attrs = cat.get("attributes", {})
    
    res = []
    for e in log:
        if sku and e.get("target") != sku:
            continue
            
        res.append({
            "pushed_at": e.get("pushed_at", ""),
            "kind": e.get("kind", ""),
            "target_sku_or_id": e.get("target", ""),
            "target_name": e.get("target_label", ""),
            "field": e.get("field", ""),
            "from": e.get("extra", {}).get("old_label") or e.get("old", ""),
            "to": e.get("extra", {}).get("new_label") or e.get("new", ""),
            "result": e.get("status", ""),
            "note": e.get("note", "")
        })
        if len(res) >= limit:
            break
            
    return {"catalogue_date": cat.get("loaded_at", "Unknown"), "changes": res}

@mcp.tool()
def describe_database() -> dict:
    """List every table in Houston's database with its columns, row count and what it holds.
    Call this before run_sql."""
    try:
        with closing(core.connect()) as c:
            out = []
            for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
                                     "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                cols = [r[1] for r in c.execute(f'PRAGMA table_info("{name}")')]
                rows = c.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                out.append({"table": name, "rows": rows, "columns": cols, "about": TABLE_NOTES.get(name, "")})
            loaded = c.execute("SELECT value FROM meta WHERE key = 'loaded_at'").fetchone()
        return {"catalogue_date": loaded[0] if loaded else "not loaded", "tables": out}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def run_sql(sql: str, max_rows: int = 200) -> dict:
    """Run ONE read-only SQL query (SELECT or WITH) on Houston's SQLite database and return the rows.
    At most max_rows rows are returned (capped at 1000). Writing is impossible: the database is opened read-only."""
    q = sql.strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", q):
        return {"error": "Only SELECT or WITH queries are allowed."}
    try:
        with closing(core.connect()) as c:
            cur = c.execute(q)
            cols = [d[0] for d in cur.description or []]
            limit = max(1, min(int(max_rows), 1000))
            rows = cur.fetchmany(limit + 1)
        return {"columns": cols, "rows": [list(r) for r in rows[:limit]], "truncated": len(rows) > limit}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# --- work log: the only thing this server can write ---------------------------------------
WORKLOG = Path(__file__).parent / "notes" / "worklog.md"
LIVING_SECTIONS = ("Current state", "Decisions", "Open items")


def _worklog_backup():
    folder = WORKLOG.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WORKLOG, folder / f"worklog_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.md")


def _bad_text(text):
    if not text or not text.strip():
        return "The text is empty."
    if re.search(r"(?m)^## ", text):
        return "The text can't contain '## ' headings; those would break the log's structure."
    return None


@mcp.tool()
def read_worklog() -> dict:
    """Read the biomania.ro work log (notes/worklog.md): current state, decisions, open items and dated log entries.
    Read it at the start of any session about the shop or Houston."""
    if not WORKLOG.exists():
        return {"error": "No work log yet: notes/worklog.md is missing from the Houston folder."}
    changed = datetime.datetime.fromtimestamp(WORKLOG.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return {"last_changed": changed, "text": WORKLOG.read_text(encoding="utf-8")}


@mcp.tool()
def add_worklog_entry(title: str, text: str) -> dict:
    """Add a dated entry at the end of the work log's Log section. Entries are never edited afterwards.
    Use after a change, test, finding or decision worth keeping. Keep it short and factual; use '- ' bullets."""
    problem = _bad_text(text) or (None if title.strip() else "The title is empty.")
    if problem:
        return {"error": problem}
    if not WORKLOG.exists():
        return {"error": "notes/worklog.md is missing."}
    body = WORKLOG.read_text(encoding="utf-8").rstrip("\n")
    if "\n## Log" not in "\n" + body:
        return {"error": "The work log has no '## Log' section."}
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n\n### {stamp} — {title.strip()}\n{text.strip()}\n"
    _worklog_backup()
    WORKLOG.write_text(body + entry, encoding="utf-8")
    return {"added": f"{stamp} — {title.strip()}"}


@mcp.tool()
def replace_worklog_section(section: str, text: str) -> dict:
    """Rewrite one living section of the work log: 'Current state', 'Decisions' or 'Open items'.
    The Log section can't be rewritten. The previous version is kept as a backup."""
    if section not in LIVING_SECTIONS:
        return {"error": f"Only these sections can be rewritten: {', '.join(LIVING_SECTIONS)}."}
    problem = _bad_text(text)
    if problem:
        return {"error": problem}
    if not WORKLOG.exists():
        return {"error": "notes/worklog.md is missing."}
    doc = WORKLOG.read_text(encoding="utf-8")
    match = re.search(rf"(?m)^## {re.escape(section)}[ \t]*\n", doc)
    if not match:
        return {"error": f"Section '{section}' not found in the work log."}
    nxt = re.search(r"(?m)^## ", doc[match.end():])
    end = match.end() + nxt.start() if nxt else len(doc)
    new_doc = doc[:match.end()] + "\n" + text.strip() + "\n\n" + doc[end:]
    _worklog_backup()
    WORKLOG.write_text(new_doc, encoding="utf-8")
    return {"replaced": section}


# ---------------------------------------------------------------- code (read-only)
# Claude can read Houston's own code, so it always sees the current version (including edits made with other
# tools). Only text files under the Houston folder. Never data/, secrets/, the Python environment, git internals,
# backups, or anything named like a secret, token, key or credential. Nothing here can write.
CODE_ROOT = Path(__file__).resolve().parent
CODE_SKIP_DIRS = {"data", "secrets", ".venv", "venv", "env", ".git", "__pycache__", "node_modules", "backups",
                  ".pytest_cache", ".mypy_cache"}
CODE_EXT = {".py", ".md", ".txt", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".css", ".js", ".html", ".ps1",
            ".bat", ".sql", ".gitignore"}
CODE_SECRETISH = re.compile(r"secret|token|credential|password|passwd|apikey|api_key|\.env|\.key$|\.pem$|\.p12$", re.I)
CODE_MAX_LINES = 800


def _code_ok(p):
    try:
        rel = p.resolve().relative_to(CODE_ROOT)
    except ValueError:
        return False  # outside the Houston folder, including through links
    if any(part.lower() in CODE_SKIP_DIRS for part in rel.parts[:-1]):
        return False
    name = rel.name.lower()
    ext = ".gitignore" if name == ".gitignore" else p.suffix.lower()
    return p.is_file() and ext in CODE_EXT and not CODE_SECRETISH.search(name)
def _code_files():
    import os
    out = []
    for root, dirs, files in os.walk(CODE_ROOT):
        # never walk into data/, secrets/, .venv, .git, backups: faster, and nothing in there can break the list
        dirs[:] = sorted(d for d in dirs if d.lower() not in CODE_SKIP_DIRS)
        for name in sorted(files):
            p = Path(root) / name
            try:
                if _code_ok(p):
                    out.append(p)
            except OSError:
                continue  # a locked or unreadable file is skipped instead of breaking the tool
    return out


def _fingerprint(data):
    import hashlib
    return hashlib.sha1(data).hexdigest()[:10]


@mcp.tool()
def list_code_files() -> dict:
    """List Houston's code files, read-only: path, size, number of lines, last change and a fingerprint
    (changes whenever the file's content changes). data/, secrets/, the Python environment, .git and
    backups are never listed."""
    files, skipped = [], []
    for p in _code_files():
        rel = p.relative_to(CODE_ROOT).as_posix()
        try:
            data = p.read_bytes()
            modified = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError as e:
            skipped.append(f"{rel}: {type(e).__name__}")
            continue
        files.append({"path": rel, "bytes": len(data),
                      "lines": data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0),
                      "modified": modified, "fingerprint": _fingerprint(data)})
    out = {"folder": str(CODE_ROOT), "files": files}
    if skipped:
        out["skipped"] = skipped
    return out


@mcp.tool()
def read_code_file(path: str, start_line: int = 1, end_line: int = 0) -> dict:
    """Read one of Houston's code files (a path from list_code_files, e.g. 'core.py'), with line numbers.
    Read-only. At most 800 lines per call: use start_line/end_line to page through longer files."""
    p = CODE_ROOT / path.replace("\\", "/").lstrip("/")
    if not _code_ok(p):
        return {"error": "Not readable: only Houston's own code files can be read (see list_code_files)."}
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(start_line or 1))
    end = int(end_line or 0) or len(lines)
    end = min(end, len(lines), start + CODE_MAX_LINES - 1)
    width = len(str(end))
    body = "\n".join(f"{i:>{width}}| {lines[i - 1]}" for i in range(start, end + 1))
    out = {"path": p.relative_to(CODE_ROOT).as_posix(), "total_lines": len(lines), "from": start, "to": end,
           "modified": datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
           "fingerprint": _fingerprint(p.read_bytes()), "text": body}
    if end < len(lines):
        out["continue"] = f"More lines: call again with start_line={end + 1}."
    return out


@mcp.tool()
def search_code(text: str, max_results: int = 100) -> dict:
    """Find text in Houston's code files (case-insensitive): path, line number and the line. Read-only."""
    needle, hits = text.lower(), []
    if not needle.strip():
        return {"error": "Give some text to look for."}
    for p in _code_files():
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if needle in line.lower():
                hits.append({"path": p.relative_to(CODE_ROOT).as_posix(), "line": i, "text": line.strip()[:300]})
                if len(hits) >= min(int(max_results or 100), 500):
                    return {"hits": hits, "truncated": True}
    return {"hits": hits, "truncated": False}


if __name__ == "__main__":
    mcp.run()
