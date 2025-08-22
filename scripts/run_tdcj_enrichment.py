# scripts/run_tdcj_enrichment.py
from __future__ import annotations

import os
import time
from typing import Dict

from storage.mongo_client import get_db
from enrichment.tdcj_enrich import tdcj_lookup

BATCH_LIMIT = int(os.getenv("TDCJ_LIMIT", "100"))       # how many persons to attempt
SLEEP_BETWEEN = float(os.getenv("TDCJ_SLEEP", "0.8"))   # seconds between lookups

def main():
    db = get_db()

    # Target: Galveston persons missing DOB (highest value)
    q = {
        "dob": {"$in": [None, "", "N/A"]},
        "links.url": {"$regex": "galvestoncountytx.gov/.*InmateDetail", "$options": "i"},
    }

    cur = db.persons.find(q, {"full_name": 1, "dob": 1, "_ext_id": 1, "links": 1}).limit(BATCH_LIMIT)

    total = 0
    updated = 0
    for p in cur:
        total += 1
        full_name = (p.get("full_name") or "").strip()
        if not full_name:
            continue

        print(f"[{total}] TDCJ lookup → {full_name}")
        info = tdcj_lookup(full_name)
        time.sleep(SLEEP_BETWEEN)

        if not info:
            print("   ... no match")
            continue

        updates: Dict[str, object] = {}
        if info.get("dob"):
            updates["dob"] = info["dob"]
        # store identifiers
        ident = (p.get("identifiers") or {}).copy()
        if info.get("tdcj"):
            ident["tdcj"] = [info["tdcj"]]
        if info.get("sid"):
            ident["sid"] = [info["sid"]]
        if ident:
            updates["identifiers"] = ident
        # optionally normalize race/sex if missing
        demo = (p.get("demographics") or {}).copy()
        if not demo.get("race") and info.get("race"):
            demo["race"] = info["race"]
        if not demo.get("sex") and info.get("sex"):
            demo["sex"] = info["sex"]
        if demo:
            updates["demographics"] = demo

        # add a link back to TDCJ detail page for provenance
        links = list(p.get("links") or [])
        if info.get("source_url") and not any((l for l in links if l.get("url") == info["source_url"])):
            links.append({"rel": "tdcj_detail", "url": info["source_url"]})
            updates["links"] = links

        if not updates:
            print("   ... nothing to update")
            continue

        key = {"_id": p["_id"]}
        res = db.persons.update_one(key, {"$set": updates, "$currentDate": {"updated_at": True}})
        if res.modified_count:
            updated += 1
            print(f"   ✓ updated (dob={updates.get('dob')})")
        else:
            print("   ... no change")

    print(f"\nDone. Attempted: {total}, Updated: {updated}")

if __name__ == "__main__":
    main()