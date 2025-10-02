#!/usr/bin/env python3
"""
Health check for simple_harris time buckets
------------------------------------------
Verifies that time_bucket is computed strictly from booking_date by re-computing
the expected bucket server-side and comparing. Reports:
 - total docs
 - counts by time_bucket
 - number of docs with missing booking_date
 - number of docs where computed != stored time_bucket

Optionally POST a JSON payload to a webhook (set HEALTH_WEBHOOK_URL).

Usage:
  ./.venv/bin/python -m scripts.health_simple_harris
"""
from __future__ import annotations
import os, json
from datetime import datetime, timezone
from dotenv import load_dotenv
from pymongo import MongoClient
import urllib.request


def _db():
    load_dotenv()
    uri = os.environ["MONGO_URI"]
    name = os.environ.get("MONGO_DB", "warrantdb")
    return MongoClient(uri)[name]


def _computed_bucket_expr(date_expr):
    # days since booking_date
    return {
        "$let": {
            "vars": {
                "days": {
                    "$trunc": {
                        "$divide": [
                            {"$subtract": ["$$NOW", date_expr]},
                            1000 * 60 * 60 * 24,
                        ]
                    }
                }
            },
            "in": {
                "$switch": {
                    "branches": [
                        {"case": {"$lte": ["$$days", 1]}, "then": "24_hours_or_less"},
                        {"case": {"$lte": ["$$days", 2]}, "then": "48_hours"},
                        {"case": {"$lte": ["$$days", 3]}, "then": "72_hours"},
                        {"case": {"$lte": ["$$days", 30]}, "then": "0_to_30_days"},
                        {"case": {"$lte": ["$$days", 60]}, "then": "31_to_60_days"},
                        {"case": {"$lte": ["$$days", 180]}, "then": "61_to_180_days"},
                    ],
                    "default": "365_days_or_older",
                }
            },
        }
    }


def run() -> dict:
    db = _db()
    coll = db["simple_harris"]

    # Aggregate counts and detect mismatches in one pass using $setWindowFields-like logic via $project
    pipeline = [
        {
            "$project": {
                "time_bucket": 1,
                "time_bucket_v2": 1,
                "booking_date": 1,
                # compute expected bucket from booking_date string when present
                "expected_bucket": {
                    "$cond": [
                        {"$and": [
                            {"$ne": ["$booking_date", None]},
                            {"$eq": [{"$type": "$booking_date"}, "string"]},
                        ]},
                        _computed_bucket_expr({
                            "$dateFromString": {
                                "dateString": "$booking_date",
                                "onError": None,
                                "onNull": None,
                            }
                        }),
                        None,
                    ]
                },
            }
        },
        {
            "$project": {
                "time_bucket": 1,
                "time_bucket_v2": 1,
                "booking_date": 1,
                "expected_bucket": 1,
                "mismatch": {
                    "$cond": [
                        {"$eq": ["$expected_bucket", None]},
                        False,  # if no booking_date, we don't count as mismatch
                        {"$ne": ["$time_bucket", "$expected_bucket"]}
                    ]
                }
            }
        },
        {
            "$facet": {
                "by_bucket": [
                    {"$group": {"_id": {"$ifNull": ["$time_bucket", "missing"]}, "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                ],
                "by_bucket_v2": [
                    {"$group": {"_id": {"$ifNull": ["$time_bucket_v2", "missing"]}, "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                ],
                "missing_booking_date": [
                    {"$match": {"booking_date": {"$in": [None, None]},}},
                    {"$count": "n"}
                ],
                "coverage_v2": [
                    {"$group": {
                        "_id": None,
                        "withBookingDT": {"$sum": {"$cond": [["$booking_datetime", 1, 0], 1, 0]}},
                        "withBucketV2": {"$sum": {"$cond": [["$time_bucket_v2", 1, 0], 1, 0]}},
                        "total": {"$sum": 1}
                    }},
                    {"$project": {
                        "_id": 0,
                        "total": 1,
                        "pct_booking_datetime": {"$cond": [{"$gt": ["$total", 0]}, {"$divide": ["$withBookingDT", "$total"]}, None]},
                        "pct_time_bucket_v2": {"$cond": [{"$gt": ["$total", 0]}, {"$divide": ["$withBucketV2", "$total"]}, None]}
                    }}
                ],
                "mismatches": [
                    {"$match": {"mismatch": True}},
                    {"$count": "n"}
                ]
            }
        }
    ]

    res = list(coll.aggregate(pipeline, allowDiskUse=True))
    if not res:
        return {"ok": True, "by_bucket": [], "missing_booking_date": 0, "mismatches": 0}
    doc = res[0]
    missing_bd = (doc.get("missing_booking_date") or [{}])[0].get("n", 0)
    mismatches = (doc.get("mismatches") or [{}])[0].get("n", 0)
    coverage = (doc.get("coverage_v2") or [{}])[0] or {}
    payload = {
        "ok": True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "by_bucket": doc.get("by_bucket", []),
        "by_bucket_v2": doc.get("by_bucket_v2", []),
        "missing_booking_date": missing_bd,
        "mismatches": mismatches,
        "coverage_v2": coverage,
    }

    # optional webhook
    url = os.environ.get("HEALTH_WEBHOOK_URL")
    if url:
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
    print(json.dumps(payload, indent=2, default=str))
    return payload


if __name__ == "__main__":
    run()
