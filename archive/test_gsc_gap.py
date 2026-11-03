"""Read-only test (Houston step 5, Phase A, item 2): can Houston get Search Console data for 23 Jun - 8 Sep 2026?

Houston asks Google for finalised data only (dataState "final"), and for these dates Google answered with nothing
on 9 and 24 Oct, while Search Console showed them as still being processed. Google can also send data that isn't
final yet (dataState "all"). This script asks both ways, for the gap and for two weeks on each side, for every
Search Console property the Google sign-in can see, and prints what Houston already holds for the same dates.

It writes nothing: not to Houston's database, not to Google.
Run from the Houston folder:   .\\.venv\\Scripts\\python.exe test_gsc_gap.py
Then paste everything it prints into the chat.
"""
import sqlite3
from contextlib import closing
from pathlib import Path

import google_sc

WINDOWS = [("2 weeks before", "2026-06-09", "2026-06-22"),
           ("the gap", "2026-06-23", "2026-09-08"),
           ("2 weeks after", "2026-09-09", "2026-09-22")]
DB = Path(__file__).resolve().parent / "data" / "houston.db"


def ask(svc, prop, start, end, state):
    body = {"startDate": start, "endDate": end, "dimensions": ["date"], "rowLimit": 25000,
            "dataState": state, "type": "web"}
    resp = google_sc._execute(svc.searchanalytics().query(siteUrl=prop, body=body))
    rows = resp.get("rows", [])
    return {"days": len(rows), "clicks": sum(int(r.get("clicks", 0)) for r in rows),
            "impressions": sum(int(r.get("impressions", 0)) for r in rows),
            "first": rows[0]["keys"][0] if rows else "", "last": rows[-1]["keys"][0] if rows else "",
            "metadata": resp.get("metadata") or {}}


def stored(prop, start, end):
    """What Houston's database holds for the same dates (opened read-only)."""
    if not DB.exists():
        return None
    with closing(sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)) as c:
        days, clicks, impr = c.execute("SELECT COUNT(*), SUM(clicks), SUM(impressions) FROM gsc_totals "
                                       "WHERE property = ? AND date BETWEEN ? AND ?", (prop, start, end)).fetchone()
    return {"days": days or 0, "clicks": clicks or 0, "impressions": impr or 0}


def _line(r):
    if r is None:
        return "no database"
    if not r["days"]:
        return "nothing"
    return f"{r['days']} days, {r['clicks']:,} clicks, {r['impressions']:,} impressions"


def main(svc=None, props=None):
    svc = svc or google_sc.service()
    props = props if props is not None else google_sc.properties(svc)
    if not props:
        print("This Google sign-in sees no Search Console property.")
        return
    gap_days = {}
    for prop, level in props:
        print(f"\nProperty {prop} ({level})")
        for label, start, end in WINDOWS:
            try:
                final = ask(svc, prop, start, end, "final")
                every = ask(svc, prop, start, end, "all")
            except google_sc.GoogleError as e:
                print(f"  {label}: Google refused: {e}")
                continue
            print(f"  {label}, {start} to {end}")
            print(f"    finalised only (what Houston asks for): {_line(final)}")
            print(f"    including unfinished data:              {_line(every)}"
                  + (f", {every['first']} to {every['last']}" if every["days"] else ""))
            print(f"    Houston holds:                          {_line(stored(prop, start, end))}")
            if every["metadata"]:
                print(f"    Google's note on unfinished data: {every['metadata']}")
            if label == "the gap":
                gap_days[prop] = every["days"]
    print()
    for prop, n in gap_days.items():
        if n:
            print(f"Result for {prop}: Google sends {n} of the gap's 78 days when unfinished data is accepted.")
        else:
            print(f"Result for {prop}: Google sends nothing for the gap, finalised or not.")


if __name__ == "__main__":
    main()
