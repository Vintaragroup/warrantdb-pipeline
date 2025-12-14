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
from pathlib import Path
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import OperationFailure
try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

COUNTIES = ["harris", "galveston", "brazoria", "fortbend", "jefferson"]


def _db():
    # Load .env from repo root so MONGO_* are available when running as a module
    if load_dotenv is not None:
        # scripts/ -> repo root is parent
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
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

    def safe_create_index(keys, name: str | None = None, **kwargs):
        try:
            if name:
                return col.create_index(keys, name=name, **kwargs)
            return col.create_index(keys, **kwargs)
        except OperationFailure as e:
            # IndexOptionsConflict (code 85) occurs if same spec exists with different name
            if getattr(e, 'code', None) == 85 or 'Index already exists' in str(e):
                print(f"  [i] index exists (spec matches) -> {name or keys}")
                return None
            raise
    # Name search & common lookups
    safe_create_index([("full_name", ASCENDING)], name="full_name")
    safe_create_index([("booking_number", ASCENDING)], name="booking_number")
    safe_create_index([("case_number", ASCENDING)], name="case_number")
    # Upsert filter performance: compound on upsert key parts
    safe_create_index([
        ("_upsert_key.county", ASCENDING),
        ("_upsert_key.category", ASCENDING),
        ("_upsert_key.anchor", ASCENDING),
    ], name="upsert_key_parts")
    # FE/API sorting & filters
    safe_create_index([("booking_datetime", DESCENDING)], name="booking_datetime_desc")
    safe_create_index([("time_bucket_v2", ASCENDING)], name="time_bucket_v2")
    # SPN lookup convenience (some flows use a top-level spn field in simple docs)
    safe_create_index([("spn", ASCENDING)], name="spn")
    # Recency
    safe_create_index([("updated_at", DESCENDING)], name="updated_at_desc")
    safe_create_index([("created_at", DESCENDING)], name="created_at_desc")


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
