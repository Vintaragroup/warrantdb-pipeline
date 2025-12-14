

"""
Delta reporter for simple_* collections
--------------------------------------
This script compares the current contents of each simple_{county} collection to
its previous snapshot (stored as fingerprints in simple_fp_state_{county}).
It records counts of NEW and CHANGED documents and writes a summary document to
`reports` while printing a human-readable summary to stdout.

• No changes to your existing writers/normalizer are required.
• Fingerprints ignore `_id` and are stable across key order.
• State is stored per-county so you can run per-county or all at once.

Env:
  MONGO_URI  - required
  MONGO_DB   - default: warrantdb
  REPORT_COUNTIES - optional comma-separated list (default: all known)
"""
import os
import json
import hashlib
import datetime as dt
from typing import Dict, List, Tuple, Iterable

from pymongo import MongoClient
from dotenv import load_dotenv

COUNTIES: List[str] = ["harris", "galveston", "brazoria", "fortbend", "jefferson"]
SIMPLE_PREFIX = "simple_"              # e.g., simple_harris
STATE_PREFIX  = "simple_fp_state_"     # e.g., simple_fp_state_harris
REPORTS_COLL  = "reports"


def _db():
    # Load .env so MONGO_* are available when not exported
    load_dotenv()
    uri = os.environ["MONGO_URI"]
    name = os.environ.get("MONGO_DB", "warrantdb")
    return MongoClient(uri)[name]


def _report_counties() -> List[str]:
    raw = os.environ.get("REPORT_COUNTIES")
    if not raw:
        return COUNTIES
    return [c.strip().lower() for c in raw.split(",") if c.strip()]


def stable_key(doc: Dict) -> str:
    """Heuristic identity without schema changes.
    Tries common id fields; falls back to Mongo _id string.
    """
    for k in ("source_id", "booking_number", "case_number", "_ext_id"):
        v = doc.get(k)
        if v not in (None, ""):
            return f"{k}:{str(v)}"
    return str(doc.get("_id"))


def fingerprint(doc: Dict) -> str:
    """Content hash ignoring Mongo internals."""
    pruned = {k: v for k, v in doc.items() if k not in ("_id",)}
    blob = json.dumps(pruned, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_prev_state(state_coll) -> Dict[str, str]:
    """Return key->fp map from state collection."""
    out: Dict[str, str] = {}
    for row in state_coll.find({}, {"key": 1, "fp": 1}):
        k = str(row.get("key"))
        f = row.get("fp")
        if k and f:
            out[k] = f
    return out


def persist_state(state_coll, cur_map: Dict[str, str]) -> None:
    """Bulk upsert all key/fp pairs for performance."""
    if not cur_map:
        return
    from pymongo import UpdateOne
    ops = [UpdateOne({"key": k}, {"$set": {"key": k, "fp": f}}, upsert=True) 
           for k, f in cur_map.items()]
    if ops:
        state_coll.bulk_write(ops)


def generate_for_county(db, county: str) -> Tuple[int, int, List[Dict]]:
    simple_name = f"{SIMPLE_PREFIX}{county}"
    state_name  = f"{STATE_PREFIX}{county}"

    if simple_name not in db.list_collection_names():
        return 0, 0, []

    simple = db[simple_name]
    state  = db[state_name]

    prev = load_prev_state(state)

    new_cnt = 0
    chg_cnt = 0
    examples: List[Dict] = []
    cur_map: Dict[str, str] = {}

    # Stream through the collection (projection minimizes payload)
    # Iterate in ascending _id order with short-lived cursors to avoid Atlas tier restrictions
    last_id = None
    batch_size = 5000
    while True:
        q = {"_id": {"$gt": last_id}} if last_id is not None else {}
        got = 0
        for doc in simple.find(q, sort=[("_id", 1)], limit=batch_size):
            last_id = doc.get("_id", last_id)
            got += 1
            k = stable_key(doc)
            f = fingerprint(doc)
            cur_map[k] = f
            pf = prev.get(k)
            if pf is None:
                new_cnt += 1
                if len(examples) < 10:
                    examples.append({
                        "type": "new",
                        "key": k,
                        "name": doc.get("full_name") or doc.get("name"),
                        "id": str(doc.get("_id")),
                    })
            elif pf != f:
                chg_cnt += 1
                if len(examples) < 10:
                    examples.append({
                        "type": "changed",
                        "key": k,
                        "name": doc.get("full_name") or doc.get("name"),
                        "id": str(doc.get("_id")),
                    })
        if got < batch_size:
            break

    persist_state(state, cur_map)
    return new_cnt, chg_cnt, examples


def main() -> int:
    db = _db()
    ts = dt.datetime.now(dt.timezone.utc)

    counties = _report_counties()
    items: List[Dict] = []
    total_new = 0
    total_changed = 0

    for c in counties:
        new_cnt, chg_cnt, examples = generate_for_county(db, c)
        total_new += new_cnt
        total_changed += chg_cnt
        items.append({
            "county": c,
            "new": new_cnt,
            "changed": chg_cnt,
            "examples": examples,
        })

    db[REPORTS_COLL].insert_one({
        "type": "simple_delta",
        "generated_at": ts,
        "totals": {"new": total_new, "changed": total_changed},
        "items": items,
    })

    # Human-readable console output for Render logs
    print("=== SIMPLE COLLECTION DELTA REPORT ===")
    print(f"Generated at: {ts.isoformat()}Z")
    for it in items:
        print(f"- {it['county']}: +{it['new']} new, {it['changed']} changed")
    print(f"TOTAL: +{total_new} new, {total_changed} changed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())