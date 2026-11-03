"""Sales channels: every WinMENTOR sale document gets one channel (rules agreed with Stefan, 23 Oct 2026).

rebuild(c, run_at) fills sales_docs: one row per document of GetVanzariLuna (wm_sales_month), for the database
that owns its month (winmentor.month_owner). The customer comes from GetVanzariExt (wm_sales_ext c11, the
fiscal code), joined by database, month, series and document number. The note is GetVanzariLuna c23 (field 24).
The Sales page joins sale lines to sales_docs by (company, month, series, number).

Rules, first match wins:
 1. A delivery note with every line at price 0 is a sample; a note with "donatie" is a donation. Not sales.
 2. Customer DANTE INTERNATIONAL SA (eMAG): series V-MKTP = eMAG Marketplace, any other series = eMAG wholesale.
 3. Customer BEBETEI INVESTMENTS GROUP SRL = BEBETEI.
 4. Note contains EMAG or MKTP = eMAG Marketplace.
 5. Note contains TELEFON = People offline, checked before SITE. Provisional: to confirm with Stefan's mother.
 6. Note contains SITE = Website.
 7. Customer with a company fiscal code (anything but a 13-digit personal number) = B2B.
 8. A return or correction (negative total) takes the channel of the earlier invoice it cancels: same address,
    same total with the opposite sign.
 9. An invoice to a person with no channel word, whose total with VAT matches a website order within 0.05 lei
    and is dated 0-14 days after it, = Website. Orders already matched to a Website or People offline invoice
    aren't reused, and each order is used once.
10. Anything else = Unknown.
Rules 8 and 9 only look at documents that rules 1-7 leave Unknown. Rule 9 runs first, so a return of an invoice
placed by rule 9 follows it.

Privacy: sales_docs keeps a fiscal code, a name and the shop or branch (place) for companies only. A person's personal number, name and
address are read from WinMENTOR's tables for the rules and not copied.
"""
import datetime
import re
from collections import defaultdict
from contextlib import closing

import store_orders
import winmentor as wm

EMAG = "14399840"          # DANTE INTERNATIONAL SA, fiscal code digits
BEBETEI_CODE = "32742199"  # BEBETEI INVESTMENTS GROUP SRL
WEBSITE, OFFLINE, B2B, BEBETEI = "Website", "People offline", "B2B", "BEBETEI"
MARKETPLACE, WHOLESALE, UNKNOWN, NOT_SALE = "eMAG Marketplace", "eMAG wholesale", "Unknown", "Not a sale"
ORDER = [WEBSITE, OFFLINE, B2B, BEBETEI, MARKETPLACE, WHOLESALE, UNKNOWN]
TOTAL = "Total"
MATCH_CENTS = 5            # rule 9: totals within 0.05 lei
MATCH_DAYS = 14            # rule 9: invoiced 0-14 days after the order
RECENT_MONTHS = 3          # Data health lists Unknown documents of this month and the two before

CHECK = {"id": "sale_no_channel", "group": "Sales channels", "level": "caution",
         "title": "Sale with no channel",
         "how": "Houston gives every WinMENTOR sale document a channel (Website, eMAG, BEBETEI, B2B and so on) from "
                "its customer and its note. This lights up for documents of this month and the two before that no "
                "rule places: an invoice to a person with no SITE, EMAG or TELEFON in its note and no website order "
                "with the same total, or a return whose original invoice isn't found.",
         "why": "Its sales land in Unknown instead of their channel.",
         "fixer": "The operator, by writing the channel in the invoice note",
         "head": ("Document", "Date and total")}

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sales_docs (company TEXT, month TEXT, doc_date TEXT, doc TEXT, doc_type TEXT,
        series TEXT, number TEXT, customer_code TEXT, customer TEXT, place TEXT, note TEXT, lines INTEGER, value REAL,
        total_vat REAL, channel TEXT, rule INTEGER, why TEXT, website_order TEXT, built_at TEXT,
        PRIMARY KEY (company, month, doc))""",
    "CREATE INDEX IF NOT EXISTS ix_sales_docs_month ON sales_docs (month, channel)",
    "CREATE INDEX IF NOT EXISTS ix_sales_docs_number ON sales_docs (company, month, series, number)",
]
COLS = ["company", "month", "doc_date", "doc", "doc_type", "series", "number", "customer_code", "customer", "place", "note",
        "lines", "value", "total_vat", "channel", "rule", "why", "website_order", "built_at"]

# One row per document. Numbers use a decimal comma and no thousands separator (checked on all 698,080 lines,
# 23 Oct 2026), so REPLACE(',', '.') is safe. c9 (document total with VAT) is the same on every line.
DOCS_SQL = """
SELECT c13, c14, c2, MAX(c15), MIN(CAST(c1 AS INTEGER)), TRIM(COALESCE(MAX(c23), '')), TRIM(COALESCE(MAX(c21), '')),
       CAST(REPLACE(MAX(c9), ',', '.') AS REAL),
       SUM(CAST(REPLACE(c5, ',', '.') AS REAL) * CAST(REPLACE(c7, ',', '.') AS REAL)),
       SUM(CASE WHEN CAST(REPLACE(c7, ',', '.') AS REAL) <> 0 THEN 1 ELSE 0 END), COUNT(*)
FROM wm_sales_month WHERE company = ? AND period = ? GROUP BY c13, c14, c2
"""
EXT_SQL = ("SELECT c19, c2, MAX(TRIM(COALESCE(c11, ''))), MAX(TRIM(COALESCE(c9, ''))) FROM wm_sales_ext "
           "WHERE company = ? AND period = ? GROUP BY c19, c2")


def _digits(code):
    return re.sub(r"\D", "", code or "")


def _person(code):
    """A person: no fiscal code, or a 13-digit personal number (CNP)."""
    return not (code or "").strip() or len(_digits(code)) == 13


def _cents(lei):
    return int(round((lei or 0.0) * 100))


# --- reading ---------------------------------------------------------------------------------------
def _names(c):
    """{company: {fiscal code digits: customer name}} from WinMENTOR's customer lists."""
    out = defaultdict(dict)
    for company, code, name in c.execute("SELECT company, TRIM(c3), TRIM(c2) FROM wm_customers "
                                         "WHERE TRIM(COALESCE(c3, '')) <> ''"):
        out[company].setdefault(_digits(code), name or "")
    return out


def _documents(c):
    names = _names(c)
    docs = []
    for month, company in sorted(wm.month_owner(c).items()):
        ext = {(s or "", n or ""): (code, place) for s, n, code, place in c.execute(EXT_SQL, (company, month))}
        y, m = (int(x) for x in month.split("_"))
        for t, s, n, doc, day, note, addr, total, value, priced, lines in c.execute(DOCS_SQL, (company, month)):
            t, s, n = t or "", s or "", n or ""
            code, place = ext.get((s, n), ("", ""))
            try:
                date = datetime.date(y, m, day or 1)
            except ValueError:
                date = datetime.date(y, m, 1)
            company_code = "" if _person(code) else code
            name = (names.get(company, {}).get(_digits(code))
                    or next((v[_digits(code)] for v in names.values() if _digits(code) in v), "")) if company_code else ""
            docs.append({"company": company, "month": month, "date": date, "doc": doc or f"{t}.{s}{n}", "type": t,
                         "series": s, "number": n, "code": code, "customer_code": company_code, "customer": name,
                         "place": place if company_code else "",
                         "note": note, "addr": addr, "total": round(total or 0.0, 2), "value": round(value or 0.0, 4),
                         "priced": priced or 0, "lines": lines or 0, "channel": UNKNOWN, "rule": 10, "why": "",
                         "website_order": ""})
    return docs


# --- the rules -------------------------------------------------------------------------------------
def _base(d):
    """Rules 1-7: (channel, rule, why) or None when the document is left Unknown."""
    note = d["note"].upper()
    if d["type"] == "AE" and d["priced"] == 0:
        return NOT_SALE, 1, "sample: every line at price 0"
    if "DONATIE" in note:
        return NOT_SALE, 1, "donation"
    digits = _digits(d["code"])
    if digits == EMAG:
        if "MKTP" in d["series"].upper():
            return MARKETPLACE, 2, f"customer eMAG, series {d['series']}"
        return WHOLESALE, 2, "customer eMAG"
    if digits == BEBETEI_CODE:
        return BEBETEI, 3, "customer BEBETEI"
    if "EMAG" in note or "MKTP" in note:
        return MARKETPLACE, 4, f"note: {d['note']}"
    if "TELEFON" in note:
        return OFFLINE, 5, f"note: {d['note']}"
    if "SITE" in note:
        return WEBSITE, 6, f"note: {d['note']}"
    if not _person(d["code"]):
        return B2B, 7, f"customer {d['customer'] or d['code']}"
    return None


def _match_orders(docs, orders):
    """Rule 9, plus the website order behind each Website or People offline invoice."""
    pool = defaultdict(list)
    for number, placed, status, total in orders:
        if (status or "").lower() == "canceled" or total is None:
            continue
        try:
            day = datetime.date.fromisoformat(placed)
        except ValueError:
            continue
        pool[_cents(total)].append([day, number, False])

    def take(d):
        cents, best = _cents(d["total"]), None
        for k in range(cents - MATCH_CENTS, cents + MATCH_CENTS + 1):
            for o in pool.get(k, ()):
                gap = (d["date"] - o[0]).days
                if not o[2] and 0 <= gap <= MATCH_DAYS:
                    key = (abs(k - cents), gap, o[1])
                    if best is None or key < best[0]:
                        best = (key, o)
        if best is None:
            return ""
        best[1][2] = True
        return best[1][1]

    ordered = sorted(docs, key=lambda x: (x["date"], x["doc"]))
    for d in ordered:
        if d["channel"] in (WEBSITE, OFFLINE) and d["total"] > 0:
            d["website_order"] = take(d)
    for d in ordered:
        if d["channel"] == UNKNOWN and d["total"] > 0:
            number = take(d)
            if number:
                d.update(channel=WEBSITE, rule=9, why=f"matches website order {number}", website_order=number)


def _match_returns(docs):
    """Rule 8: a return takes the channel of the earlier invoice to the same address with the opposite total."""
    sales = defaultdict(list)
    for d in docs:
        if d["total"] > 0 and d["addr"] and d["channel"] not in (UNKNOWN, NOT_SALE):
            sales[(d["addr"], _cents(d["total"]))].append(d)
    for d in sorted(docs, key=lambda x: (x["date"], x["doc"])):
        if d["channel"] != UNKNOWN or d["total"] >= 0 or not d["addr"]:
            continue
        earlier = [o for o in sales.get((d["addr"], -_cents(d["total"])), ()) if o["date"] <= d["date"]]
        if earlier:
            o = max(earlier, key=lambda x: (x["date"], x["doc"]))
            d.update(channel=o["channel"], rule=8, why=f"cancels {o['doc']} of {o['date']:%d %b %Y}")


def classify(docs, orders):
    for d in docs:
        hit = _base(d)
        if hit:
            d["channel"], d["rule"], d["why"] = hit
    _match_orders(docs, orders)
    _match_returns(docs)
    for d in docs:
        if d["channel"] == UNKNOWN:
            d["why"] = ("return or correction: no earlier invoice to the same address with the same total"
                        if d["total"] < 0 else
                        f"invoice to a person, {'note: ' + d['note'] if d['note'] else 'no note'}, "
                        "and no website order with the same total")
    return docs


# --- running and reading -----------------------------------------------------------------------------
def _recent_months(today):
    cur = f"{today.year:04d}_{today.month:02d}"
    return {wm.month_add(cur, -i) for i in range(RECENT_MONTHS)}


def rebuild(c, run_at, today=None):
    """Classify every sale document into sales_docs, on connection c. Returns the Data health issues."""
    have = [r[1] for r in c.execute("PRAGMA table_info(sales_docs)")]
    if have and "place" not in have:             # built before the shop column existed: rebuilt in full anyway
        c.execute("DROP TABLE sales_docs")
    for sql in SCHEMA:
        c.execute(sql)
    docs = classify(_documents(c), store_orders.for_matching(c))
    c.execute("DELETE FROM sales_docs")
    c.executemany(f"INSERT OR REPLACE INTO sales_docs VALUES ({','.join('?' * len(COLS))})",
                  [(d["company"], d["month"], d["date"].isoformat(), d["doc"], d["type"], d["series"], d["number"],
                    d["customer_code"], d["customer"], d["place"], d["note"], d["lines"], d["value"], d["total"],
                    d["channel"],
                    d["rule"], d["why"], d["website_order"], run_at) for d in docs])
    recent = _recent_months(today or wm.today())
    return [(CHECK["id"], d["doc"], f"{d['date']:%d %b %Y}, {d['total']:,.2f} lei", d["why"])
            for d in sorted(docs, key=lambda x: (x["date"], x["doc"]))
            if d["channel"] == UNKNOWN and d["month"] in recent]


def rebuild_quietly(c, run_at):
    """rebuild() inside health's run. A failure is undone and shown on Data health instead of stopping the checks."""
    c.execute("SAVEPOINT channels")
    try:
        found = rebuild(c, run_at)
    except Exception as e:                       # noqa: BLE001 (shown on the Data health page)
        c.execute("ROLLBACK TO channels")
        c.execute("RELEASE channels")
        return [(CHECK["id"], "-", "The channel rules couldn't run", f"{type(e).__name__}: {e}")]
    c.execute("RELEASE channels")
    return found


def summary(first, last):
    """Channel totals for months first..last ('yyyy_MM'), in a fixed order, with a Total line last:
    [{'channel', 'documents', 'value' (lei without VAT), 'total_vat'}]. Samples and donations are left out."""
    with closing(wm.connect()) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'sales_docs'").fetchone():
            return []
        got = {ch: (n, v, t) for ch, n, v, t in c.execute(
            "SELECT channel, COUNT(*), SUM(value), SUM(total_vat) FROM sales_docs WHERE month BETWEEN ? AND ? "
            "AND channel <> ? GROUP BY channel", (first, last, NOT_SALE))}
    rows = [{"channel": ch, "documents": got.get(ch, (0, 0, 0))[0], "value": round(got.get(ch, (0, 0, 0))[1] or 0, 2),
             "total_vat": round(got.get(ch, (0, 0, 0))[2] or 0, 2)} for ch in ORDER]
    rows.append({"channel": TOTAL, "documents": sum(r["documents"] for r in rows),
                 "value": round(sum(r["value"] for r in rows), 2), "total_vat": round(sum(r["total_vat"] for r in rows), 2)})
    return rows
