#!/usr/bin/env python3
"""
Sync Harris addresses from simple_harris into persons.contact.addresses.

Rules:
- Use SPN first (identifiers.spn array), else fallback to case_number (digits prefix) if available.
- Append address if not already present for that person (match on line1+city+zip).
- Do not delete or overwrite existing addresses.
- Safe to run repeatedly (idempotent-ish by matching key fields).

Usage:
  python -m scripts.person_address_sync_harris --limit 5000

Env:
  MONGO_URI (required)
  MONGO_DB  (required)
"""
from __future__ import annotations
import os, sys, argparse
from typing import Dict, Any, Optional
from pymongo import MongoClient
from dotenv import load_dotenv


def _db():
    load_dotenv()
    uri = os.environ.get("MONGO_URI")
    dbn = os.environ.get("MONGO_DB")
    if not uri or not dbn:
        print("ERROR: MONGO_URI and MONGO_DB required", file=sys.stderr)
        raise SystemExit(2)
    return MongoClient(uri)[dbn]


def _addr_key(addr: Dict[str, Any]) -> Optional[str]:
    if not addr:
        return None
    l1 = (addr.get("line1") or "").strip().upper()
    city = (addr.get("city") or "").strip().upper()
    zipc = (addr.get("zip") or "").strip()
    if not l1 and not city and not zipc:
        return None
    return f"{l1}|{city}|{zipc}"


def sync(limit: int) -> Dict[str, int]:
    db = _db()
    simple = db["simple_harris"]
    persons = db["persons"]

    q = {"county": "harris", "address": {"$type": "object"}}
    proj = {"_id": 0, "spn": 1, "case_number": 1, "address": 1, "full_name": 1}

    added = 0
    scanned = 0
    skipped_no_match = 0
    cursor = simple.find(q, proj).limit(limit)
    for doc in cursor:
        scanned += 1
        addr = doc.get("address") or {}
        akey = _addr_key(addr)
        if not akey:
            continue

        # Build person selector by SPN first
        spn = (doc.get("spn") or "").strip()
        person = None
        if spn:
            person = persons.find_one({"identifiers.spn": spn}, {"_id": 1, "contact.addresses": 1})
        if not person:
            cn = (doc.get("case_number") or "").strip()
            if cn:
                person = persons.find_one({"identifiers.case_number": cn}, {"_id": 1, "contact.addresses": 1})

        if not person:
            skipped_no_match += 1
            continue

        addrs = (person.get("contact", {}).get("addresses") or [])
        have_keys = { _addr_key(a) for a in addrs }
        if akey in have_keys:
            continue

        # Append address
        persons.update_one(
            {"_id": person["_id"]},
            {"$push": {"contact.addresses": addr}, "$set": {"updated_at": __import__('datetime').datetime.utcnow()}},
        )
        added += 1

    return {"scanned": scanned, "added": added, "skipped_no_match": skipped_no_match}


def main():
    ap = argparse.ArgumentParser(description="Sync Harris addresses into persons.contact.addresses")
    ap.add_argument("--limit", type=int, default=10000)
    args = ap.parse_args()
    res = sync(args.limit)
    print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
