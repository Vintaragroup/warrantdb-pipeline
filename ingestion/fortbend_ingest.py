# ingestion/fortbend_ingest.py
import os, sys, time, datetime as dt, signal, json, re
from typing import Dict, Any, List
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv
from pymongo import ASCENDING, IndexModel, errors

# local scraper
from ingestion.fortbend_jail import search_fort_bend

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB",  "warrantdb")
FORTBEND_COLLECTION = os.getenv("FORTBEND_COLL", "fortbend_inmates")

LETTER_DELAY_SEC = float(os.getenv("FORTBEND_LETTER_DELAY_SEC", "0.8"))

def _mongo():
    client = MongoClient(MONGO_URI)
    return client, client[MONGO_DB], client[MONGO_DB][FORTBEND_COLLECTION]


def _ensure_indexes(col):
    """Create indexes idempotently by comparing key specs (not names)."""
    desired = [
        IndexModel([("id", ASCENDING)],                name="id_idx"),
        IndexModel([("booking_number", ASCENDING)],    name="booking_number_idx"),
        IndexModel([("name", ASCENDING)],              name="name_idx"),
        IndexModel([("fetched_at", ASCENDING)],        name="fetched_at_idx"),
        IndexModel([("detail_fetched_at", ASCENDING)], name="detail_fetched_at_idx"),
        IndexModel([("source", ASCENDING)],            name="source_idx"),
        # Enhanced indexes for new fields:
        IndexModel([("booking_date_iso", ASCENDING)],  name="booking_date_iso_idx"),
        IndexModel([("booking_age_category", ASCENDING)], name="booking_age_category_idx"),
        IndexModel([("booking_priority", ASCENDING)],  name="booking_priority_idx"),
        IndexModel([("is_recent_addition_24h", ASCENDING)], name="recent24_idx"),
        IndexModel([("scraped_at", ASCENDING)],        name="scraped_at_idx"),
    ]

    # Map existing indexes by their key spec, e.g. (("name", 1),)
    existing_by_keys = {}
    for ix in col.list_indexes():
        existing_by_keys[tuple(ix["key"].items())] = ix["name"]

    to_create = []
    for model in desired:
        model_doc = model.document
        key_spec = tuple(model_doc["key"])  # list of (field, direction)
        if key_spec in existing_by_keys:
            continue
        to_create.append(model)

    if not to_create:
        return

    try:
        col.create_indexes(to_create)
    except errors.OperationFailure as e:
        # Defensive: create one-by-one and ignore "already exists" conflicts
        if e.code == 85:  # IndexOptionsConflict
            for m in to_create:
                try:
                    col.create_index(m.document["key"], name=m.document.get("name"))
                except errors.OperationFailure as ie:
                    if ie.code != 85:
                        raise
        else:
            raise

def _augment_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add recent addition tracking and ensure standardized fields."""
    now = dt.datetime.now(dt.timezone.utc)
    day_ago = now - dt.timedelta(days=1)
    out = []
    
    for r in rows:
        r = dict(r)  # shallow copy
        
        # Mark if this record was first seen in last 24h
        try:
            first_seen_str = r.get("first_seen_at") or r.get("fetched_at")
            first_seen = dt.datetime.fromisoformat(first_seen_str.replace("Z", "")) if first_seen_str else None
        except Exception:
            first_seen = None
        r["is_recent_addition_24h"] = bool(first_seen and first_seen >= day_ago)
        
        # Ensure booking age fields exist (should be added by updated fortbend_jail.py)
        if "booking_age_category" not in r:
            r["booking_age_category"] = "unknown"
        if "booking_priority" not in r:
            r["booking_priority"] = 7
            
        out.append(r)
    return out

def _upserts(col, rows):
    rows = _augment_rows(rows)
    ops = []
    for r in rows:
        key = r.get("id") or r.get("booking_number")
        if not key:
            continue
        ops.append(
            UpdateOne(
                {"id": r.get("id")} if r.get("id") else {"booking_number": r.get("booking_number")},
                {"$set": r, "$setOnInsert": {"first_seen_at": r.get("fetched_at")}},
                upsert=True
            )
        )
    if not ops:
        return {"matched": 0, "modified": 0, "upserted": 0}
    res = col.bulk_write(ops, ordered=False)
    return {
        "matched":  res.matched_count,
        "modified": res.modified_count,
        "upserted": len(res.upserted_ids or {}),
    }

def _expand_letters(spec: str) -> str:
    spec = spec.strip().upper()
    if re.fullmatch(r"[A-Z]-[A-Z]", spec):
        a, b = spec.split("-")
        return "".join(chr(c) for c in range(ord(a), ord(b)+1))
    if spec == "A-Z":
        return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    # otherwise treat as literal set (e.g., "ABC" or "MX")
    return "".join(ch for ch in spec if "A" <= ch <= "Z")

def _fmt_hms(seconds: float) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

class Logger:
    def __init__(self):
        self.t0 = time.monotonic()
    def log(self, msg: str):
        elapsed = _fmt_hms(time.monotonic() - self.t0)
        print(f"[{elapsed}] {msg}", flush=True)

def ingest_all_letters(
    include_details: bool = True,
    letters: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    verbose: bool = True,
    detail_every: int = 10,  # log every N rows processed
) -> Dict[str, Any]:

    L = Logger()
    client, db, col = _mongo()
    _ensure_indexes(col)

    totals = {"matched": 0, "modified": 0, "upserted": 0, "scraped": 0}
    per_letter: Dict[str, Dict[str, Any]] = {}
    booking_categories: Dict[str, int] = {}

    stop_requested = {"yes": False}
    def _sigint(_sig, _frm):
        stop_requested["yes"] = True
        L.log("Ctrl+C received — finishing current letter, then stopping…")
    signal.signal(signal.SIGINT, _sigint)

    # compatible with search_fort_bend progress_cb (i,total, last, first) OR (i,total)
    def progress_cb(i: int, n: int, *_, **__):
        if not verbose or detail_every <= 0:
            return
        if i % detail_every == 0 or i == n:
            L.log(f"   processed {i}/{n} rows")

    try:
        for idx, ch in enumerate(letters, start=1):
            if stop_requested["yes"]:
                break

            L.log(f"→ START letter {ch} ({idx}/{len(letters)})")
            t_letter = time.monotonic()
            letter_categories: Dict[str, int] = {}

            # scrape
            try:
                rows = search_fort_bend(
                    last=ch,
                    first="",
                    include_details=include_details,
                    progress_cb=progress_cb if verbose else None,
                )
            except Exception as e:
                L.log(f"!! Letter {ch}: search failed: {e}")
                rows = []

            scrape_sec = time.monotonic() - t_letter
            
            # Track booking categories for this letter
            for r in rows:
                cat = r.get("booking_age_category", "unknown")
                letter_categories[cat] = letter_categories.get(cat, 0) + 1
                booking_categories[cat] = booking_categories.get(cat, 0) + 1

            per_letter[ch] = {
                "scraped": len(rows),
                "booking_categories": dict(sorted(letter_categories.items()))
            }
            totals["scraped"] += len(rows)
            L.log(f"   scraped {len(rows)} record(s) in {scrape_sec:.1f}s — upserting to MongoDB Atlas…")

            # Log booking category breakdown for this letter
            if letter_categories:
                L.log(f"   booking age breakdown: {dict(sorted(letter_categories.items()))}")

            # upsert
            stats = _upserts(col, rows)
            for k in ("matched", "modified", "upserted"):
                per_letter[ch][k] = stats[k]
                totals[k] += stats[k]

            L.log(f"✓ DONE letter {ch}: upserted={stats['upserted']} matched={stats['matched']} modified={stats['modified']} (letter elapsed {time.monotonic()-t_letter:.1f}s)")

            # Enhanced success logging with categories
            for r in rows:
                name = r.get("name", "UNKNOWN")
                category = r.get("booking_age_category", "unknown")
                L.log(f"[fortbend] SUCCESS: {name} [{category}]")

            time.sleep(LETTER_DELAY_SEC)

        L.log("All requested letters processed." if not stop_requested["yes"] else "Stopped by user.")
        
        # Final summary with booking categories
        L.log("BOOKING AGE SUMMARY:")
        for category, count in sorted(booking_categories.items()):
            L.log(f"  {category}: {count} inmates")
            
    finally:
        client.close()

    summary = {
        "totals": totals, 
        "per_letter": per_letter, 
        "collection": f"{MONGO_DB}.{FORTBEND_COLLECTION}",
        "booking_categories": dict(sorted(booking_categories.items()))
    }
    return summary

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser("Fort Bend Jail → MongoDB Atlas ingester with enhanced monitoring")
    ap.add_argument("--no-details", action="store_true", help="Skip detail pages (charges/bond)")
    ap.add_argument("--letters", default="A-Z", help="Letters to search: 'A-Z', 'A-M', or a set like 'SMT'")
    ap.add_argument("--detail-every", type=int, default=10, help="Log every N rows (0=off)")
    args = ap.parse_args()

    letters = _expand_letters(args.letters)
    out = ingest_all_letters(
        include_details=not args.no_details,
        letters=letters,
        verbose=True,
        detail_every=args.detail_every,
    )
    print(json.dumps(out, indent=2))