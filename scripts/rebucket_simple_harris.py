#!/usr/bin/env python3
"""
Recompute simple_harris.time_bucket strictly from booking_date and remove
time_bucket when booking_date is missing. Intended as a safe, idempotent
housekeeping job you can schedule nightly to guarantee correct aging tags.

ENV:
  MONGO_URI - required
  MONGO_DB  - required

Usage:
  python3 -m scripts.rebucket_simple_harris
"""
from __future__ import annotations
import os
from pymongo import MongoClient
from dotenv import load_dotenv
from pathlib import Path


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    mongo_uri = os.environ.get("MONGO_URI")
    mongo_db = os.environ.get("MONGO_DB")
    if not mongo_uri or not mongo_db:
        print("ERROR: MONGO_URI and MONGO_DB env vars are required.")
        return 2

    client = MongoClient(mongo_uri)
    db = client[mongo_db]
    coll = db["simple_harris"]

    # 1) Recompute time_bucket for docs with booking_date
    #    Uses aggregation pipeline update for in-place recompute
    res1 = coll.update_many(
        {"booking_date": {"$exists": True, "$type": "string"}},
        [
            {"$set": {
                "booking_dt": {"$dateFromString": {"dateString": "$booking_date", "onError": None, "onNull": None}}
            }},
            {"$set": {
                # floor((now - booking_dt) / 1d)
                "days_since": {"$trunc": {"$divide": [{"$subtract": ["$$NOW", "$booking_dt"]}, 1000 * 60 * 60 * 24]}}
            }},
            {"$set": {
                "time_bucket": {
                    "$switch": {
                        "branches": [
                            {"case": {"$lte": ["$days_since", 1]}, "then": "24_hours_or_less"},
                            {"case": {"$lte": ["$days_since", 2]}, "then": "48_hours"},
                            {"case": {"$lte": ["$days_since", 3]}, "then": "72_hours"},
                            {"case": {"$lte": ["$days_since", 30]}, "then": "0_to_30_days"},
                            {"case": {"$lte": ["$days_since", 60]}, "then": "31_to_60_days"},
                            {"case": {"$lte": ["$days_since", 180]}, "then": "61_to_180_days"}
                        ],
                        "default": "365_days_or_older"
                    }
                }
            }},
            {"$unset": ["booking_dt", "days_since"]}
        ]
    )

    # 2) Remove time_bucket if booking_date is missing
    res2 = coll.update_many(
        {"$or": [{"booking_date": {"$exists": False}}, {"booking_date": None}]},
        {"$unset": {"time_bucket": ""}}
    )

    print({
        "matched_rebucket": res1.matched_count,
        "modified_rebucket": res1.modified_count,
        "unset_no_booking": res2.modified_count,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
