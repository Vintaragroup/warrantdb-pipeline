#!/usr/bin/env python3
"""
Harris post-normalize housekeeping:
 - Convert booking_datetime strings -> Date
 - Derive booking_datetime from booking_date when missing
 - Compute time_bucket_v2 from booking_datetime
 - Optionally rebucket recent docs so tags age correctly

Usage examples:
  python3 scripts/harris_post_normalize.py --dry-run
  python3 scripts/harris_post_normalize.py
  python3 scripts/harris_post_normalize.py --max-days 14

This script uses storage.mongo_client for MONGO_URI/MONGO_DB.
Target collection defaults to 'simple_harris'.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any, Dict

import os
import sys

# Ensure repo root is on sys.path when run as a file (python scripts/harris_post_normalize.py)
_CURRENT_DIR = os.path.dirname(__file__)
_REPO_ROOT = os.path.dirname(_CURRENT_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from storage.mongo_client import get_db


def _computed_bucket_from(date_expr: Any) -> Dict[str, Any]:
    """Return an aggregation expression that computes time_bucket_v2 from a date field."""
    hours_since = {"$dateDiff": {"startDate": date_expr, "endDate": "$$NOW", "unit": "hour"}}
    return {
        "$let": {
            "vars": {"hrs": hours_since},
            "in": {
                "$switch": {
                    "branches": [
                        {"case": {"$and": [{"$gte": ["$$hrs", 0]}, {"$lt": ["$$hrs", 24]}]}, "then": "0_24h"},
                        {"case": {"$and": [{"$gte": ["$$hrs", 24]}, {"$lt": ["$$hrs", 48]}]}, "then": "24_48h"},
                        {"case": {"$and": [{"$gte": ["$$hrs", 48]}, {"$lt": ["$$hrs", 72]}]}, "then": "48_72h"},
                        {"case": {"$and": [{"$gte": ["$$hrs", 72]}, {"$lt": ["$$hrs", 168]}]}, "then": "3d_7d"},
                        {"case": {"$and": [{"$gte": ["$$hrs", 168]}, {"$lt": ["$$hrs", 720]}]}, "then": "7d_30d"},
                        {"case": {"$and": [{"$gte": ["$$hrs", 720]}, {"$lt": ["$$hrs", 1440]}]}, "then": "30d_60d"},
                        {"case": {"$gte": ["$$hrs", 1440]}, "then": "60d_plus"},
                    ],
                    "default": None,
                }
            },
        }
    }


def backfill_and_rebucket(collection_name: str, dry_run: bool, max_days: int | None) -> None:
    db = get_db()
    coll = db[collection_name]
    now_iso = datetime.now(timezone.utc).isoformat()
    print(f"[harris_post_normalize] Now: {now_iso}")

    # Pre stats
    pre_stats = list(
        coll.aggregate([
            {"$group": {"_id": {"dtType": {"$type": "$booking_datetime"}, "bucketType": {"$type": "$time_bucket_v2"}}, "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ])
    )
    print("[pre] booking_datetime/time_bucket_v2 type distribution:")
    print(pre_stats)

    # 1) booking_datetime string -> Date
    filter1 = {"booking_datetime": {"$type": "string", "$regex": r"^\d{4}-\d{2}-\d{2}"}}
    pipeline1 = [
        {"$set": {
            "booking_datetime": {"$dateFromString": {"dateString": "$booking_datetime", "timezone": "UTC"}},
            # booking_date_v2 becomes yyyy-mm-dd from booking_datetime string
            "booking_date_v2": {"$substrBytes": ["$booking_datetime", 0, 10]},
            "booking_derivation_source": "booking_datetime_string",
            "time_bucket_v2": _computed_bucket_from({"$dateFromString": {"dateString": "$booking_datetime", "timezone": "UTC"}}),
        }}
    ]

    # 2) Derive from booking_date (string)
    filter2 = {"booking_datetime": {"$exists": False}, "booking_date": {"$type": "string", "$regex": r"^\d{4}-\d{2}-\d{2}"}}
    pipeline2 = [
        {"$set": {
            "booking_datetime": {"$dateFromString": {"dateString": "$booking_date", "timezone": "UTC"}},
            "booking_date_v2": "$booking_date",
            "booking_derivation_source": "legacy_booking_date",
            "time_bucket_v2": _computed_bucket_from({"$dateFromString": {"dateString": "$booking_date", "timezone": "UTC"}}),
        }}
    ]

    # 3) Derive from booking_date (date)
    filter3 = {"booking_datetime": {"$exists": False}, "booking_date": {"$type": "date"}}
    pipeline3 = [
        {"$set": {
            "booking_datetime": "$booking_date",
            "booking_date_v2": {"$dateToString": {"date": "$booking_date", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "booking_derivation_source": "legacy_booking_date",
            "time_bucket_v2": _computed_bucket_from("$booking_date"),
        }}
    ]

    # Execute updates
    if dry_run:
        n1 = coll.count_documents(filter1)
        n2 = coll.count_documents(filter2)
        n3 = coll.count_documents(filter3)
        print(f"[dry-run] convert booking_datetime strings -> Date: {n1}")
        print(f"[dry-run] derive booking_datetime from booking_date (string): {n2}")
        print(f"[dry-run] derive booking_datetime from booking_date (date): {n3}")
    else:
        r1 = coll.update_many(filter1, pipeline1)
        print("[update] booking_datetime string -> Date:", {"matched": r1.matched_count, "modified": r1.modified_count})
        r2 = coll.update_many(filter2, pipeline2)
        print("[update] derive from booking_date (string):", {"matched": r2.matched_count, "modified": r2.modified_count})
        r3 = coll.update_many(filter3, pipeline3)
        print("[update] derive from booking_date (date):", {"matched": r3.matched_count, "modified": r3.modified_count})

    # 4) Optional rebucket recent docs so tags age correctly
    if max_days is not None:
        recent_filter = {
            "booking_datetime": {"$type": "date"},
            "$expr": {"$lte": [{"$dateDiff": {"startDate": "$booking_datetime", "endDate": "$$NOW", "unit": "day"}}, max_days]},
        }
        rebucket_pipeline = [{"$set": {"time_bucket_v2": _computed_bucket_from("$booking_datetime")}}]
        if dry_run:
            n_recent = coll.count_documents(recent_filter)
            print(f"[dry-run] would rebucket recent docs (<= {max_days} days): {n_recent}")
        else:
            r = coll.update_many(recent_filter, rebucket_pipeline)
            print(f"[update] rebucket recent (<= {max_days} days):", {"matched": r.matched_count, "modified": r.modified_count})

    # Post stats
    post_stats = list(
        coll.aggregate([
            {"$group": {"_id": {"dtType": {"$type": "$booking_datetime"}, "bucketType": {"$type": "$time_bucket_v2"}}, "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ])
    )
    print("[post] booking_datetime/time_bucket_v2 type distribution:")
    print(post_stats)


def main():
    ap = argparse.ArgumentParser(description="Harris post-normalize fixer (booking_datetime + time_bucket_v2)")
    ap.add_argument("--collection", default="simple_harris", help="Target collection (default: simple_harris)")
    ap.add_argument("--dry-run", action="store_true", help="Do not write changes; only report counts")
    ap.add_argument("--max-days", type=int, default=90, help="Rebucket docs within this many days (set to 0 to skip)")
    args = ap.parse_args()

    max_days = None if args.max_days == 0 else args.max_days
    backfill_and_rebucket(args.collection, args.dry_run, max_days)


if __name__ == "__main__":
    main()
