"""Supplier order forms: each supplier's own Excel file, read by EAN and filled in in its own format.

- The EAN column is the only key: suppliers change their own product codes too often (Stefan, Oct 2026).
- Houston finds the EAN column itself (the column full of valid EANs) and the header row above it, and
  proposes the name, pieces-per-carton, order and price columns from their headers. A confirmed layout is
  saved per supplier and reused whenever a file with the same headers comes back.
- Suggested quantity (Stefan, Oct 2026): average monthly sales x 3 - stock, never below 0, rounded up to
  whole cartons. Rows whose name contains DELISTAT get no suggestion.
- Some products are held in WinMENTOR under another EAN than the supplier's (measured 21 Oct 2026: Isola Bio's
  coconut cooking cream is 8023678423416 on the form, 8023678423409 in WinMENTOR and Magento). Those are linked
  by hand once (wm_ean_links), with a factor when the units differ. The supplier's own copy of our code, when
  the form has one, is only used to propose the link; nothing is linked without a person confirming it.
- Filling changes only the order column. Formulas that point into another workbook keep their current value
  instead, so Excel doesn't ask to update links. Everything else in the supplier's file stays as it was.
"""
import datetime
import hashlib
import io
import json
import math
import re
import unicodedata

import winmentor as wm

FACTOR = 3
SKIP_WORDS = ("DELISTAT",)
KEYWORDS = {
    "ppc": ["pezzi", "pz/c", "pz x c", "pcs/c", "pcs per", "pieces per", "units per", "buc/bax", "bucati/bax",
            "per cartone", "per carton"],
    "order": ["ordine", "order", "qty", "quantit", "cantitate", "comanda"],
    "name": ["prodotto", "product", "descr", "denumire", "articolo", "item", "nome", "name"],
    "price": ["prezzo", "price", "listino", "pret", "pl "],
}
CARTON_WORDS = ["cart", "colli", "box", "bax", "carton", "cutii"]


def _load(data, values):
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise RuntimeError("Houston needs openpyxl for Excel files: run  pip install -r requirements.txt") from None
    return load_workbook(io.BytesIO(data), data_only=values)


def _norm(text):
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", t).strip().lower()


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def letter(col):
    from openpyxl.utils import get_column_letter
    return get_column_letter(col)


class Form:
    """One supplier file, opened twice: values (to read) and formulas (to fill in)."""

    def __init__(self, data, name):
        self.data, self.name = data, name
        self.wb = _load(data, values=True)
        self.sha = hashlib.sha1(data).hexdigest()


def ean_rows(ws, ean_col):
    return [r for r in range(1, ws.max_row + 1) if wm.ean(ws.cell(r, ean_col).value)]


def headers(ws, header_row):
    if not header_row:
        return {}
    return {c.column: str(c.value).strip() for c in ws[header_row] if c.value not in (None, "")}


def _find(heads, kind, taken):
    for col, text in sorted(heads.items()):
        if col in taken:
            continue
        t = _norm(text) + " "
        if any(k in t for k in KEYWORDS[kind]):
            return col
    return None


def detect(form):
    """Best guess at the layout: sheet, header row, EAN column and the other columns."""
    best = None
    for ws in form.wb.worksheets:
        counts = {}
        for row in ws.iter_rows():
            for cell in row:
                if wm.ean(cell.value):
                    counts[cell.column] = counts.get(cell.column, 0) + 1
        for col, n in counts.items():
            if not best or n > best[0]:
                best = (n, ws.title, col)
    if not best or best[0] < 3:
        raise ValueError("This file has no column with EAN codes, so Houston can't read it as an order form.")
    _, sheet, ean_col = best
    ws = form.wb[sheet]
    rows = ean_rows(ws, ean_col)
    header_row = 0
    for r in range(rows[0] - 1, 0, -1):
        if sum(1 for c in ws[r] if isinstance(c.value, str) and c.value.strip()) >= 3:
            header_row = r
            break
    heads = headers(ws, header_row)
    taken = {ean_col}
    lay = {"sheet": sheet, "header_row": header_row, "ean_col": ean_col}
    for kind in ("ppc", "order", "name", "price"):
        lay[f"{kind}_col"] = _find(heads, kind, taken)
        if lay[f"{kind}_col"]:
            taken.add(lay[f"{kind}_col"])
    if not lay["name_col"]:                      # longest texts in the data rows
        lengths = {}
        for r in rows:
            for cell in ws[r]:
                if isinstance(cell.value, str) and cell.column not in taken:
                    lengths[cell.column] = lengths.get(cell.column, 0) + len(cell.value)
        lay["name_col"] = max(lengths, key=lengths.get) if lengths else None
    order_head = _norm(heads.get(lay["order_col"], ""))
    lay["order_unit"] = "cartons" if any(w in order_head for w in CARTON_WORDS) else "pieces"
    return lay


def signature(form, lay):
    ws = form.wb[lay["sheet"]]
    heads = headers(ws, lay["header_row"])
    text = lay["sheet"] + "|" + "|".join(f"{c}:{_norm(t)}" for c, t in sorted(heads.items()))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def recognise(form, saved):
    """The saved (supplier, layout) whose headers match this file, if any."""
    for supplier, sig, lay, _ in saved:
        if lay.get("sheet") in form.wb.sheetnames and signature(form, lay) == sig:
            return supplier, lay
    return None, None


def rows_of(form, lay):
    ws = form.wb[lay["sheet"]]
    out = []
    for r in ean_rows(ws, lay["ean_col"]):
        def val(key):
            col = lay.get(key)
            return ws.cell(r, col).value if col else None
        out.append({"row": r, "ean": wm.ean(val("ean_col")), "name": str(val("name_col") or "").strip(),
                    "ppc": _num(val("ppc_col")), "price": _num(val("price_col")), "in_file": _num(val("order_col"))})
    return out


def hint_column(form, lay, code_map):
    """The column (other than EAN) holding the supplier's copy of our internal codes, if one matches well."""
    ws = form.wb[lay["sheet"]]
    rows = ean_rows(ws, lay["ean_col"])
    best, best_n = None, 0
    for col in range(1, ws.max_column + 1):
        if col == lay["ean_col"]:
            continue
        n = sum(1 for r in rows if _code(ws.cell(r, col).value) in code_map)
        if n > best_n:
            best, best_n = col, n
    return best if best_n >= max(3, len(rows) // 20) else None   # only ever a proposal, confirmed by a person


def _code(v):
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return str(v).strip() if v not in (None, "") else ""


def suggest(rows, stock, sales, months, known=frozenset(), links=None, factor=FACTOR, skip_words=SKIP_WORDS):
    """One suggestion per row: average monthly sales over `months` x factor - stock, rounded up to cartons.
    known: every EAN the live WinMENTOR database holds. links: {supplier EAN: (WinMENTOR EAN, units factor)}."""
    links = links or {}
    owner = {}
    for r in rows:
        r["skip"] = any(w and w.lower() in r["name"].lower() for w in skip_words)
        if not r["skip"] and r["ean"] not in owner:
            owner[r["ean"]] = r["row"]
    out = []
    for r in rows:
        e, f = links.get(r["ean"], (r["ean"], 1.0))
        f = f or 1.0
        s = stock.get(e)
        sold = [sales.get((e, m), 0.0) / f for m in months]
        avg = sum(sold) / len(months) if months else 0.0
        st = s["total"] / f if s else 0.0
        notes = []
        active = not r["skip"] and owner.get(r["ean"]) == r["row"]
        if r["skip"]:
            notes.append("delisted")
        elif not active:
            notes.append(f"same EAN as row {owner[r['ean']]}")
        if r["ean"] in links:
            notes.append(f"linked to WinMENTOR EAN {e}" + (f", {f:g} WinMENTOR units each" if f != 1 else ""))
        found = e in known or s is not None or any(sold)
        if not found:
            notes.append("not in WinMENTOR")
        elif not s and not any(sold):
            notes.append(f"no stock, no sales in the last {len(months)} months")
        pieces = max(0, math.ceil(avg * factor - st - 1e-9)) if active else 0
        ppc = r["ppc"] if r["ppc"] and r["ppc"] > 0 else None
        cartons = (math.ceil(pieces / ppc - 1e-9) if pieces else 0) if ppc else None
        out.append({**r, "wm_ean": e, "units_factor": f, "found": found, "stock": st, "sold": sold, "avg": avg,
                    "pieces": pieces, "ppc": ppc, "cartons": cartons, "active": active, "note": ", ".join(notes),
                    "winmentor_name": s["name"] if s else ""})
    return out


_EXTERNAL = re.compile(r"\[\d+\]")


def fill(form, lay, quantities):
    """The supplier's own file with the order column filled in: {row: quantity}; empty where 0 or missing.
    Returns (file bytes, number of external-link formulas replaced by their values)."""
    from openpyxl.cell.cell import MergedCell
    wb = _load(form.data, values=False)
    ws = wb[lay["sheet"]]
    vws = form.wb[lay["sheet"]]
    for r in ean_rows(vws, lay["ean_col"]):
        cell = ws.cell(r, lay["order_col"])
        if isinstance(cell, MergedCell):
            continue
        q = quantities.get(r)
        cell.value = int(q) if q else None
    replaced = 0
    for sh in wb.worksheets:
        vsh = form.wb[sh.title]
        for row in sh.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("=") and _EXTERNAL.search(cell.value):
                    cell.value = vsh[cell.coordinate].value
                    replaced += 1
    if replaced:
        wb._external_links = []
    wb.calculation.fullCalcOnLoad = True      # totals and pallet counts recalculate when the file is opened
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), replaced


def output_name(original, day=None):
    """Same name with today's date: Order_BIO_MANIA_06_10_2026.xlsx -> Order_BIO_MANIA_22_10_2026.xlsx."""
    day = day or datetime.date.today()
    stem, dot, ext = original.rpartition(".")
    if not dot:
        stem, ext = original, "xlsx"
    pat = re.compile(r"(\d{2})([._-])(\d{2})\2(\d{4})")
    if pat.search(stem):
        stem = pat.sub(lambda m: f"{day:%d}{m.group(2)}{day:%m}{m.group(2)}{day:%Y}", stem, count=1)
    else:
        stem = f"{stem}_{day:%d_%m_%Y}"
    return f"{stem}.{ext}"


def layout_json(lay):
    return json.dumps({k: lay[k] for k in ("sheet", "header_row", "ean_col", "ppc_col", "order_col", "name_col",
                                           "price_col", "order_unit")})
