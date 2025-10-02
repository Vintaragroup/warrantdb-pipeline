#!/usr/bin/env python3
"""backfill_booking_datetime_harris.py

One-off / repeatable backfill to populate booking_datetime, booking_date_v2,
booking_derivation_source, and time_bucket_v2 for Harris simple collection
where they are missing, using the same precedence & bucket rules implemented
in normalize_to_simple.py under feature flags.

Safe Characteristics:
  * Read/modify only docs missing booking_datetime OR time_bucket_v2.
  * Does not overwrite existing booking_datetime.
  * Dry-run mode to preview impact.
  * Batch, limit controls.

ENV:
  MONGO_URI (required)
  MONGO_DB  (required)

Usage Examples:
  Dry run summary only:
    python scripts/backfill_booking_datetime_harris.py --dry-run

  Process all (no limit) in batches of 500:
    python scripts/backfill_booking_datetime_harris.py --batch 500

  Process at most 1000 docs:
    python scripts/backfill_booking_datetime_harris.py --limit 1000

Exit Codes:
  0 success, 2 missing env, 1 partial failure.
"""
from __future__ import annotations
import os, sys, argparse
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv

COUNTY = "harris"
SIMPLE_COLLECTION = f"simple_{COUNTY}"

# ---------- Parsing Helpers (mirrors logic in normalize_to_simple) ----------

def _parse_dt_any(val: Any) -> Optional[datetime]:
    if val in (None, ""): return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            # Interpret naive as America/Chicago
            val = val.replace(tzinfo=ZoneInfo("America/Chicago"))
        return val.astimezone(timezone.utc)
    if not isinstance(val, str):
        return None
    s = val.strip()
    if not s:
        return None
    try:
        if s.endswith('Z') and '+' not in s:
            s = s[:-1] + '+00:00'
        dtv = datetime.fromisoformat(s)
        if dtv.tzinfo is None:
            # Interpret naive strings as America/Chicago
            dtv = dtv.replace(tzinfo=ZoneInfo("America/Chicago"))
        return dtv.astimezone(timezone.utc)
    except Exception:
        pass
    # Try YYYY-MM-DD date only
    if len(s) == 10 and s[4:5] == '-' and s[7:8] == '-':
        try:
            central = ZoneInfo("America/Chicago")
            local_dt = datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]), tzinfo=central)
            return local_dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None


def _derive_booking_datetime(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return updates dict if derivation possible, else None."""
    if doc.get("booking_datetime"):
        return None  # already set
    sources = [
        ("first_seen_at", doc.get("first_seen_at")),
        ("updated_at", doc.get("updated_at")),
        ("legacy_booking_date", doc.get("booking_date")),
    ]
    chosen_src = None
    chosen_dt: Optional[datetime] = None
    for name, raw in sources:
        dtv = _parse_dt_any(raw) if raw else None
        if dtv:
            chosen_src = name
            chosen_dt = dtv
            break
    if not chosen_dt:
        return None
    now = datetime.now(timezone.utc)
    updates: Dict[str, Any] = {}
    # Future anomaly tagging (12h window)
    if chosen_dt - now > timedelta(hours=12):
        tags = doc.get("tags") or []
        if "future_date_candidate" not in tags:
            updates["tags"] = tags + ["future_date_candidate"]
    updates["booking_datetime"] = chosen_dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
    updates["booking_derivation_source"] = chosen_src
    updates["booking_date_v2"] = chosen_dt.astimezone(ZoneInfo("America/Chicago")).date().isoformat()
    return updates


def _bucket_v2(dt_iso: str) -> Optional[str]:
    dtv = _parse_dt_any(dt_iso)
    if not dtv:
        return None
    delta = datetime.now(timezone.utc) - dtv
    hours = delta.total_seconds() / 3600.0
    days = delta.total_seconds() / 86400.0
    if hours < 24:
        return "0_24h"
    if hours < 48:
        return "24_48h"
    if hours < 72:
        return "48_72h"
    if hours < 24*7:
        return "3d_7d"
    if days < 30:
        return "7d_30d"
    if days < 60:
        return "30d_60d"
    return "60d_plus"


def _maybe_bucket_v2_update(doc: Dict[str, Any], pending_updates: Dict[str, Any]) -> None:
    if doc.get("time_bucket_v2"):
        return
    bdt = pending_updates.get("booking_datetime") or doc.get("booking_datetime")
    if not bdt:
        return
    bucket = _bucket_v2(bdt)
    if bucket:
        pending_updates["time_bucket_v2"] = bucket

# ---------- Backfill Runner ----------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Backfill booking_datetime/time_bucket_v2 for Harris")
    p.add_argument("--batch", type=int, default=500, help="Bulk write batch size")
    p.add_argument("--limit", type=int, default=0, help="Max docs to process (0=all)")
    p.add_argument("--dry-run", action="store_true", help="Do not write; just report counts")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main():
    # Load .env so MONGO_* can be used without exporting
    load_dotenv()
    args = parse_args()
    uri = os.environ.get("MONGO_URI")
    dbname = os.environ.get("MONGO_DB")
    if not uri or not dbname:
        print("ERROR: MONGO_URI and MONGO_DB required", file=sys.stderr)
        return 2

    client = MongoClient(uri)
    db = client[dbname]
    coll = db[SIMPLE_COLLECTION]

    selector = {"county": COUNTY, "$or": [
        {"booking_datetime": {"$exists": False}},
        {"time_bucket_v2": {"$exists": False}}
    ]}

    total_candidates = coll.count_documents(selector)
    print(f"Candidates (missing booking_datetime or time_bucket_v2): {total_candidates}")
    if total_candidates == 0:
        return 0

    processed = 0
    updated = 0
    skipped_no_source = 0
    ops: List[UpdateOne] = []

    last_id = None
    while True:
        q = selector.copy()
        if last_id is not None:
            q["_id"] = {"$gt": last_id}
        cur = coll.find(q, sort=[("_id", 1)], limit=args.batch)
        batch_got = 0
        for doc in cur:
            last_id = doc["_id"]
            batch_got += 1
            processed += 1

            updates = _derive_booking_datetime(doc) or {}
            _maybe_bucket_v2_update(doc, updates)

            if not updates:
                skipped_no_source += 1
                continue

            if args.verbose:
                print(f"_id={doc['_id']} updates={list(updates.keys())}")

            if not args.dry_run:
                ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": updates}))

            updated += 1
            if args.limit and processed >= args.limit:
                break
        if (args.limit and processed >= args.limit) or batch_got < args.batch:
            break

        # Flush ops if large
        if ops and len(ops) >= args.batch and not args.dry_run:
            coll.bulk_write(ops, ordered=False)
            ops.clear()

    # Final flush
    if ops and not args.dry_run:
        coll.bulk_write(ops, ordered=False)

    print("=== Backfill Summary ===")
    print(f"Processed: {processed}")
    print(f"Updated: {updated}")
    print(f"Skipped (no derivation source): {skipped_no_source}")
    if args.dry_run:
        print("(dry run - no writes committed)")

    # Post metrics (only if not dry run)
    if not args.dry_run:
        post_cov = coll.aggregate([
            {"$group": {
                "_id": None,
                "withBookingDT": {"$sum": {"$cond": [{"$ifNull": ["$booking_datetime", False]}, 1, 0]}},
                "withBucketV2": {"$sum": {"$cond": [{"$ifNull": ["$time_bucket_v2", False]}, 1, 0]}},
                "total": {"$sum": 1}
            }},
            {"$project": {
                "_id": 0,
                "total": 1,
                "pct_booking_datetime": {"$divide": ["$withBookingDT", "$total"]},
                "pct_time_bucket_v2": {"$divide": ["$withBucketV2", "$total"]}
            }}
        ])
        print("Coverage After:")
        for row in post_cov:
            print(row)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
