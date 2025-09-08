"""
Additional convenience/performance indexes (non-breaking)
This script is OPTIONAL and does not modify existing collections' schemas.
It adds helpful indexes for:
  • simple_* collections (lookups and recency queries)
  • simple_fp_state_* (unique state key for delta reports)
  • reports (sort by latest)

Env:
  MONGO_URI  - required
  MONGO_DB   - default: warrantdb
"""
import os
from pymongo import MongoClient, ASCENDING, DESCENDING

COUNTIES = ["harris", "galveston", "brazoria", "fortbend", "jefferson"]


def _db():
    uri = os.environ["MONGO_URI"]
    name = os.environ.get("MONGO_DB", "warrantdb")
    return MongoClient(uri)[name]


def _ensure_simple_indexes(db, county: str) -> None:
    name = f"simple_{county}"
    if name not in db.list_collection_names():
        print(f"[setup_indexes_extra] skip {name} (collection not found)")
        return
    col = db[name]
    print(f"[setup_indexes_extra] indexing {name}…")
    # Name search & common lookups
    col.create_index([("full_name", ASCENDING)])
    col.create_index([("booking_number", ASCENDING)])
    col.create_index([("case_number", ASCENDING)])
    # Recency
    col.create_index([("updated_at", DESCENDING)])
    col.create_index([("created_at", DESCENDING)])


def _ensure_fp_state_indexes(db, county: str) -> None:
    name = f"simple_fp_state_{county}"
    col = db[name]
    print(f"[setup_indexes_extra] indexing {name}…")
    col.create_index([("key", ASCENDING)], unique=True)


def _ensure_reports_indexes(db) -> None:
    col = db["reports"]
    print("[setup_indexes_extra] indexing reports…")
    col.create_index([("generated_at", DESCENDING)])
    col.create_index([("type", ASCENDING), ("generated_at", DESCENDING)])


def main() -> int:
    db = _db()
    for c in COUNTIES:
        _ensure_simple_indexes(db, c)
        _ensure_fp_state_indexes(db, c)
    _ensure_reports_indexes(db)
    print("[setup_indexes_extra] done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
