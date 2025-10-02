#!/usr/bin/env python3
"""
Recompute simple_harris.time_bucket_v2 from booking_datetime.

Rules (canon):
  0_24h:   0 ≤ age < 24h
  24_48h: 24h ≤ age < 48h
  48_72h: 48h ≤ age < 72h
  3d_7d:  72h ≤ age < 7d
  7d_30d: 7d ≤ age < 30d
  30d_60d: 30d ≤ age < 60d
  60d_plus: age ≥ 60d

booking_datetime is interpreted as UTC instant; any derivation of that value
from date-only/naive strings must already apply America/Chicago semantics
(handled in normalization/backfill). This script only recomputes the bucket.

ENV:
  MONGO_URI (required)
  MONGO_DB  (required)

Usage:
  python -m scripts.rebucket_simple_harris_v2 --dry-run
  python -m scripts.rebucket_simple_harris_v2 --batch 1000
"""
from __future__ import annotations
import os, sys, argparse
from datetime import datetime, timezone
from typing import Dict, Any
from pymongo import MongoClient
from dotenv import load_dotenv


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Recompute time_bucket_v2 for simple_harris")
    p.add_argument("--batch", type=int, default=1000)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main():
    load_dotenv()
    args = parse_args()
    uri = os.environ.get("MONGO_URI")
    dbn = os.environ.get("MONGO_DB")
    if not uri or not dbn:
        print("ERROR: MONGO_URI and MONGO_DB required", file=sys.stderr)
        return 2

    client = MongoClient(uri)
    coll = client[dbn]["simple_harris"]

    # Server-side computation pipeline: compute expected time_bucket_v2
    now = datetime.now(timezone.utc)
    pipeline = [
        {"$match": {"booking_datetime": {"$exists": True}}},
        {"$project": {
            "booking_datetime": 1,
            "time_bucket_v2": 1,
            "ageHours": {
                "$divide": [
                    {"$subtract": [now, {"$toDate": "$booking_datetime"}]},
                    1000 * 60 * 60
                ]
            },
        }},
        {"$project": {
            "booking_datetime": 1,
            "time_bucket_v2": 1,
            "expected": {
                "$switch": {
                    "branches": [
                        {"case": {"$lt": ["$ageHours", 24]}, "then": "0_24h"},
                        {"case": {"$lt": ["$ageHours", 48]}, "then": "24_48h"},
                        {"case": {"$lt": ["$ageHours", 72]}, "then": "48_72h"},
                        {"case": {"$lt": ["$ageHours", 24*7]}, "then": "3d_7d"},
                        {"case": {"$lt": ["$ageHours", 24*30]}, "then": "7d_30d"},
                        {"case": {"$lt": ["$ageHours", 24*60]}, "then": "30d_60d"},
                    ],
                    "default": "60d_plus"
                }
            }
        }},
        {"$project": {
            "expected": 1,
            "needs_update": {"$ne": ["$expected", "$time_bucket_v2"]}
        }},
    ]

    # Iterate in batches and update diffs only
    updated = 0
    scanned = 0
    cur = coll.aggregate(pipeline, allowDiskUse=True)
    bulk = []
    from pymongo import UpdateOne
    for doc in cur:
        scanned += 1
        if doc.get("needs_update"):
            if args.verbose:
                print(f"_id={doc.get('_id')} {doc.get('time_bucket_v2')} -> {doc.get('expected')}")
            if not args.dry_run:
                bulk.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"time_bucket_v2": doc["expected"]}}))
                if len(bulk) >= args.batch:
                    res = coll.bulk_write(bulk, ordered=False)
                    updated += res.modified_count
                    bulk.clear()

    if bulk and not args.dry_run:
        res = coll.bulk_write(bulk, ordered=False)
        updated += res.modified_count

    print({"scanned": scanned, "updated": updated, "dry_run": args.dry_run})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
