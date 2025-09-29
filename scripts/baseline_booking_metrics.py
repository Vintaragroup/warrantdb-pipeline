#!/usr/bin/env python3
"""baseline_booking_metrics.py

Read-only baseline snapshot of booking date related coverage for a single simple_<county>
collection (initially focused on Harris) before we introduce any new derivation
or bucket logic.

Outputs:
  - Pretty console summary
  - Optional JSON file (--out baseline_booking_metrics_<county>.json)

Metrics captured:
  * Total docs
  * Docs with booking_date (string) present
  * Docs with booking_date_iso present (if any)
  * Format distribution of booking_date (YYYY-MM-DD, MM/DD/YYYY, other, empty)
  * time_bucket distribution (existing logic)
  * Candidate fallback date field presence counts:
        file_date, first_seen_file_date, last_seen_file_date,
        arrest_date, arrest_datetime, scraped_at, fetched_at
  * Earliest / latest parsable booking_date (ISO forms only)
  * Recent window counts (last 1 / 2 / 3 / 7 / 30 days) based on booking_date if
    parseable; falls back to file_date if booking_date absent for a doc.

This script DOES NOT modify any database records.

ENV:
  MONGO_URI (required)
  MONGO_DB  (required)

Example:
  python scripts/baseline_booking_metrics.py --county harris --out harris_baseline.json --limit 0

Limit note:
  --limit 0 means scan entire collection. A positive limit scans only that many
  docs (useful for quick dry runs). We stream in batches to avoid large memory use.
"""
from __future__ import annotations
import os, sys, argparse, json, re
from datetime import datetime, timezone, date
from typing import Dict, Any, Iterable, Optional, Tuple
from pymongo import MongoClient

CANDIDATE_DATE_FIELDS = [
    "booking_date", "booking_date_iso", "file_date", "first_seen_file_date",
    "last_seen_file_date", "arrest_date", "arrest_datetime", "scraped_at", "fetched_at"
]

DATE_RE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_RE_SLASH = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Baseline booking metrics (read-only)")
    p.add_argument("--county", required=True, help="County slug (e.g. harris)")
    p.add_argument("--batch", type=int, default=2000, help="Batch size for streaming cursor")
    p.add_argument("--limit", type=int, default=0, help="Max docs to scan (0 = all)")
    p.add_argument("--out", type=str, default=None, help="Optional JSON output path")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def _iter_simple(coll, batch: int, limit: int) -> Iterable[Dict[str, Any]]:
    last_id = None
    scanned = 0
    while True:
        query = {"_id": {"$gt": last_id}} if last_id is not None else {}
        cur = coll.find(query, sort=[("_id", 1)], limit=batch)
        got = 0
        for d in cur:
            last_id = d.get("_id")
            scanned += 1
            got += 1
            yield d
            if limit and scanned >= limit:
                return
        if got < batch:
            break


def _coerce_date(val: Any) -> Optional[date]:
    if val in (None, ""):
        return None
    if isinstance(val, datetime):
        return val.astimezone(timezone.utc).date()
    if isinstance(val, date):
        return val
    if isinstance(val, (int, float)):
        # treat numeric epoch seconds
        try:
            return datetime.utcfromtimestamp(float(val)).date()
        except Exception:
            return None
    if isinstance(val, str):
        s = val.strip()
        # Strip time if full iso8601
        try:
            if len(s) == 10 and DATE_RE_ISO.match(s):
                return date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
            # Try direct ISO parse
            iso_candidate = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.date()
        except Exception:
            pass
        # Try slash format
        try:
            if DATE_RE_SLASH.match(s):
                parts = re.split(r"[/-]", s)
                if len(parts) == 3:
                    m, d, y = parts
                    if len(y) == 2:
                        y = "20" + y if int(y) < 70 else "19" + y
                    return date(int(y), int(m), int(d))
        except Exception:
            return None
    return None


def main():
    args = parse_args()
    county = args.county.lower().strip()

    mongo_uri = os.environ.get("MONGO_URI")
    mongo_db = os.environ.get("MONGO_DB")
    if not mongo_uri or not mongo_db:
        print("ERROR: MONGO_URI and MONGO_DB environment variables required", file=sys.stderr)
        return 2

    client = MongoClient(mongo_uri)
    db = client[mongo_db]
    simple_name = f"simple_{county}"
    if simple_name not in db.list_collection_names():
        print(f"ERROR: Collection {simple_name} not found", file=sys.stderr)
        return 2

    coll = db[simple_name]

    totals = {
        "total_docs": 0,
        "booking_date_present": 0,
        "booking_date_iso_present": 0,
        "time_bucket_present": 0,
    }

    format_counts = {"iso_yyyy_mm_dd": 0, "mm_dd_yyyy": 0, "other": 0, "empty": 0}
    time_bucket_counts: Dict[str, int] = {}
    fallback_presence: Dict[str, int] = {f: 0 for f in CANDIDATE_DATE_FIELDS if f not in ("booking_date", "booking_date_iso")}

    earliest: Optional[date] = None
    latest: Optional[date] = None

    recent_windows = {"last_1d": 0, "last_2d": 0, "last_3d": 0, "last_7d": 0, "last_30d": 0}

    today = datetime.now(timezone.utc).date()

    for doc in _iter_simple(coll, args.batch, args.limit):
        totals["total_docs"] += 1
        bd_raw = doc.get("booking_date")
        bdi = doc.get("booking_date_iso")
        tb = doc.get("time_bucket")

        if bd_raw not in (None, ""):
            totals["booking_date_present"] += 1
            s = str(bd_raw).strip()
            if DATE_RE_ISO.match(s):
                format_counts["iso_yyyy_mm_dd"] += 1
            elif DATE_RE_SLASH.match(s):
                format_counts["mm_dd_yyyy"] += 1
            else:
                format_counts["other"] += 1
        else:
            format_counts["empty"] += 1

        if bdi not in (None, ""):
            totals["booking_date_iso_present"] += 1

        if tb:
            totals["time_bucket_present"] += 1
            time_bucket_counts[tb] = time_bucket_counts.get(tb, 0) + 1

        # Count fallback presence (excluding booking_date/_iso which already tracked)
        for f in fallback_presence.keys():
            if doc.get(f) not in (None, ""):
                fallback_presence[f] += 1

        # Establish a representative date for recency windows: prefer booking_date/iso -> file_date -> first_seen_file_date
        representative = (
            _coerce_date(bd_raw)
            or _coerce_date(bdi)
            or _coerce_date(doc.get("file_date"))
            or _coerce_date(doc.get("first_seen_file_date"))
        )
        if representative:
            if earliest is None or representative < earliest:
                earliest = representative
            if latest is None or representative > latest:
                latest = representative
            delta = (today - representative).days
            if delta <= 1: recent_windows["last_1d"] += 1
            if delta <= 2: recent_windows["last_2d"] += 1
            if delta <= 3: recent_windows["last_3d"] += 1
            if delta <= 7: recent_windows["last_7d"] += 1
            if delta <= 30: recent_windows["last_30d"] += 1

    out = {
        "county": county,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": totals,
        "booking_date_format_counts": format_counts,
        "time_bucket_counts": time_bucket_counts,
        "fallback_presence": fallback_presence,
        "earliest_date": earliest.isoformat() if earliest else None,
        "latest_date": latest.isoformat() if latest else None,
        "recent_windows": recent_windows,
        "candidate_date_fields": CANDIDATE_DATE_FIELDS,
        "limit_applied": args.limit if args.limit else None,
    }

    # Console summary
    def pct(part, whole):
        return f"{(part/whole*100):.2f}%" if whole else "0.00%"

    total_docs = totals["total_docs"]
    print("=== BASELINE BOOKING METRICS ===")
    print(f"County: {county}")
    print(f"Total docs: {total_docs}")
    print("-- Coverage --")
    print(f"booking_date present: {totals['booking_date_present']} ({pct(totals['booking_date_present'], total_docs)})")
    print(f"booking_date_iso present: {totals['booking_date_iso_present']} ({pct(totals['booking_date_iso_present'], total_docs)})")
    print(f"time_bucket present: {totals['time_bucket_present']} ({pct(totals['time_bucket_present'], total_docs)})")
    print("-- booking_date format distribution --")
    for k, v in format_counts.items():
        print(f"  {k}: {v} ({pct(v, total_docs)})")
    print("-- time_bucket distribution --")
    for k, v in sorted(time_bucket_counts.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {k}: {v} ({pct(v, total_docs)})")
    print("-- fallback presence (non booking_date*) --")
    for k, v in sorted(fallback_presence.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {k}: {v} ({pct(v, total_docs)})")
    print("-- recency windows (representative date) --")
    for k, v in recent_windows.items():
        print(f"  {k}: {v} ({pct(v, total_docs)})")
    print(f"Earliest representative date: {out['earliest_date']}")
    print(f"Latest representative date:   {out['latest_date']}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, sort_keys=True)
        print(f"\nJSON written to {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
