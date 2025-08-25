# ingestion/brazoria_ingest.py
import os
import time
import signal
import json
import re
import datetime as dt
from typing import Dict, Any, List, Tuple, Optional
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne, ASCENDING, IndexModel, errors

# Run from repo root as a module: python -m ingestion.brazoria_ingest ...
from ingestion.brazoria_jail import search_brazoria

load_dotenv()

# --- Config from environment --------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB  = os.getenv("MONGO_DB")
BRAZORIA_COLLECTION = os.getenv("BRAZORIA_COLL", "brazoria_inmates")

# Delay between consecutive remote searches to be polite
LETTER_DELAY_SEC = float(os.getenv("BRAZORIA_LETTER_DELAY_SEC", "0.8"))

# Fail fast if env isn't loaded
if not MONGO_URI or not MONGO_DB:
    raise SystemExit("Missing MONGO_URI or MONGO_DB (ensure .env is present and load_dotenv() ran).")
print(f"[brazoria_ingest] Target: db={MONGO_DB} coll={BRAZORIA_COLLECTION}", flush=True)

# --- Mongo helpers ------------------------------------------------------------
def _mongo() -> Tuple[MongoClient, Any, Any]:
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    return client, db, db[BRAZORIA_COLLECTION]

def _ensure_indexes(col) -> None:
    desired = [
        IndexModel([("booking_number", ASCENDING)],    name="booking_number_idx", unique=True),
        IndexModel([("name", ASCENDING)],              name="name_idx"),
        IndexModel([("fetched_at", ASCENDING)],        name="fetched_at_idx"),
        IndexModel([("detail_fetched_at", ASCENDING)], name="detail_fetched_at_idx"),
        IndexModel([("source", ASCENDING)],            name="source_idx"),
        # Enhanced indexes for new fields:
        IndexModel([("booking_date_iso", ASCENDING)],  name="booking_date_iso_idx"),
        IndexModel([("booking_age_category", ASCENDING)], name="booking_age_category_idx"),
        IndexModel([("booking_priority", ASCENDING)],  name="booking_priority_idx"),
        IndexModel([("is_recent_addition_24h", ASCENDING)], name="recent24_idx"),
        IndexModel([("bond_total", ASCENDING)],        name="bond_total_idx"),
        IndexModel([("scraped_at", ASCENDING)],        name="scraped_at_idx"),
    ]

    existing_by_keys = {}
    for ix in col.list_indexes():
        existing_by_keys[tuple(ix["key"].items())] = ix["name"]

    to_create = []
    for model in desired:
        spec = tuple(model.document["key"])
        if spec not in existing_by_keys:
            to_create.append(model)

    if not to_create:
        return

    try:
        col.create_indexes(to_create)
    except errors.OperationFailure as e:
        # Handle "already exists with different options"
        if e.code == 85:
            for m in to_create:
                try:
                    col.create_index(m.document["key"], name=m.document.get("name"))
                except errors.OperationFailure as ie:
                    if ie.code != 85:
                        raise
        else:
            raise

# --- Enhanced recency tagging -----------------------------------------------
def _recency_tag(booking_iso: Optional[str], now: Optional[dt.datetime] = None) -> Optional[str]:
    if not booking_iso:
        return None
    try:
        d = dt.date.fromisoformat(booking_iso)
    except Exception:
        return None
    n = (now or dt.datetime.now(dt.timezone.utc)).date()
    days = (n - d).days
    if days <= 1:
        return "<=1d"  # Changed from unicode
    if days <= 30:
        return "<=30d"
    if days <= 60:
        return "<=60d" 
    if days <= 180:
        return "<=180d"
    if days <= 365:
        return "<=365d"
    return ">365d"

def _augment_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    now = dt.datetime.now(dt.timezone.utc)
    day_ago = now - dt.timedelta(days=1)
    out = []
    for r in rows:
        r = dict(r)  # shallow copy
        
        # Add legacy recency tag (backwards compatibility)
        tag = _recency_tag(r.get("booking_date_iso"), now)
        r["recency_tag"] = tag
        
        # Mark if this record was first seen in last 24h
        try:
            first_seen_str = r.get("first_seen_at") or r.get("fetched_at")
            first_seen = dt.datetime.fromisoformat(first_seen_str.replace("Z", "")) if first_seen_str else None
        except Exception:
            first_seen = None
        r["is_recent_addition_24h"] = bool(first_seen and first_seen >= day_ago)
        
        # Ensure standardized booking fields exist (from updated brazoria_jail.py)
        if "booking_age_category" not in r and r.get("booking_date_iso"):
            # Fallback calculation if not already present
            try:
                booked_date = dt.datetime.fromisoformat(r["booking_date_iso"].replace("Z", "")).date()
                current_date = now.date()
                days_diff = (current_date - booked_date).days
                
                if days_diff < 0:
                    r["booking_age_category"] = "future_date"
                elif days_diff <= 1:
                    r["booking_age_category"] = "24_hours_or_less"
                elif days_diff <= 30:
                    r["booking_age_category"] = "0_to_30_days"
                elif days_diff <= 60:
                    r["booking_age_category"] = "30_to_60_days"
                elif days_diff <= 180:
                    r["booking_age_category"] = "60_to_180_days"
                elif days_diff <= 365:
                    r["booking_age_category"] = "180_to_365_days"
                else:
                    r["booking_age_category"] = "365_days_or_older"
            except Exception:
                r["booking_age_category"] = "unknown"
                
        # Add priority if missing
        if "booking_priority" not in r:
            category = r.get("booking_age_category", "unknown")
            priority_map = {
                "24_hours_or_less": 1, "0_to_30_days": 2, "30_to_60_days": 3,
                "60_to_180_days": 4, "180_to_365_days": 5, "365_days_or_older": 6,
                "unknown": 7, "future_date": 8
            }
            r["booking_priority"] = priority_map.get(category, 7)
        
        out.append(r)
    return out

def _upserts(col, rows: List[Dict[str, Any]]) -> Dict[str, int]:
    rows = _augment_rows(rows)
    ops = []
    for r in rows:
        key = r.get("booking_number")
        if not key:
            continue
        ops.append(
            UpdateOne(
                {"booking_number": key},
                {
                    "$set": r,
                    "$setOnInsert": {"first_seen_at": r.get("fetched_at")}
                },
                upsert=True,
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

# --- Letter expansion / logging ----------------------------------------------
def _expand_letters(spec: str) -> str:
    """
    Accepts:
      - 'A-Z' or 'A-M' ranges (inclusive)
      - a free set like 'SMT' (non-letters ignored)
    Returns an uppercase string of unique letters in ASCII order.
    """
    spec = (spec or "").strip().upper()
    if re.fullmatch(r"[A-Z]-[A-Z]", spec):
        a, b = spec.split("-")
        return "".join(chr(c) for c in range(ord(a), ord(b) + 1))
    if spec == "A-Z":
        return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    letters = [ch for ch in spec if "A" <= ch <= "Z"]
    return "".join(sorted(set(letters)))

def _fmt_hms(seconds: float) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

class Logger:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
    def log(self, msg: str) -> None:
        elapsed = _fmt_hms(time.monotonic() - self.t0)
        print(f"[{elapsed}] {msg}", flush=True)

# --- Core ingest paths --------------------------------------------------------
def _ingest_exact(last: str, first: str, include_details: bool, verbose: bool, tick_every: int, since_days: Optional[int]) -> Dict[str, Any]:
    """
    Ingest a single exact First/Last pair.
    """
    L = Logger()
    client, db, col = _mongo()
    _ensure_indexes(col)
    try:
        L.log(f"→ EXACT search last='{last}' first='{first}'")
        rows = search_brazoria(
            last=last,
            first=first,
            include_details=include_details,
            progress_cb=(lambda i,n,*a,**k: L.log(f"   processed {i}/{n} rows"))
                      if verbose and tick_every > 0 else None,
            since_days=since_days,
        )
        L.log(f"   scraped {len(rows)} record(s) — upserting...")
        
        # Enhanced progress reporting with booking categories
        categories = {}
        for r in rows:
            cat = r.get("booking_age_category", "unknown")
            categories[cat] = categories.get(cat, 0) + 1
        
        if categories:
            L.log(f"   booking age breakdown: {dict(sorted(categories.items()))}")
        
        stats = _upserts(col, rows)
        L.log(f"✓ DONE: upserted={stats['upserted']} matched={stats['matched']} modified={stats['modified']}")
        
        # Log success messages with categories
        for r in rows:
            name = r.get("name", "UNKNOWN")
            category = r.get("booking_age_category", "unknown")
            L.log(f"[brazoria] SUCCESS: {name} [{category}]")
        
        return {
            "totals": {**stats, "scraped": len(rows)},
            "per_letter": {},
            "collection": f"{MONGO_DB}.{BRAZORIA_COLLECTION}",
            "booking_categories": categories,
        }
    finally:
        client.close()

def ingest_all_letters(
    include_details: bool = True,
    letters: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ",       # last-name letters/spec
    first_letters: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ",  # first-name initials/spec
    verbose: bool = True,
    tick_every: int = 10,
    append_wildcard: bool = False,  # see note below
    since_days: Optional[int] = 60,
) -> Dict[str, Any]:
    """
    NOTE on wildcards: Tyler typically requires >=3 characters before '*' for LAST names
    (first name allows >=1). If you pass a single letter with '*', the server may reject it.
    Default is NOT to add '*' automatically; use --append-wildcard only with longer prefixes.
    """

    L = Logger()
    client, db, col = _mongo()
    _ensure_indexes(col)

    totals = {"matched": 0, "modified": 0, "upserted": 0, "scraped": 0}
    per_letter: Dict[str, Dict[str, int]] = {}
    booking_categories: Dict[str, int] = {}

    stop_requested = {"yes": False}
    def _sigint(_sig, _frm):
        stop_requested["yes"] = True
        L.log("Ctrl+C received — finishing current letter, then stopping…")
    signal.signal(signal.SIGINT, _sigint)

    def progress_cb(i: int, n: int, *_, **__):
        if not verbose or tick_every <= 0:
            return
        if i % tick_every == 0 or i == n:
            L.log(f"      processed {i}/{n} rows")

    last_letters   = _expand_letters(letters)
    first_initials = _expand_letters(first_letters)

    try:
        for idx, last_ch in enumerate(last_letters, start=1):
            if stop_requested["yes"]:
                break

            L.log(f"→ START last-name letter {last_ch} ({idx}/{len(last_letters)})")
            t_letter0 = time.monotonic()
            letter_scraped = letter_matched = letter_modified = letter_upserted = 0
            letter_categories: Dict[str, int] = {}

            for jdx, first_ch in enumerate(first_initials, start=1):
                if stop_requested["yes"]:
                    break

                L.log(f"   ↳ First-name initial {first_ch} ({jdx}/{len(first_initials)})")
                t0 = time.monotonic()
                try:
                    q_last  = (last_ch  + "*") if append_wildcard else last_ch
                    q_first = (first_ch + "*") if append_wildcard else first_ch

                    rows = search_brazoria(
                        last=q_last,
                        first=q_first,
                        include_details=include_details,
                        progress_cb=progress_cb if verbose else None,
                        since_days=since_days,
                    )
                except Exception as e:
                    L.log(f"   !! {last_ch}/{first_ch}: search failed: {e}")
                    rows = []

                letter_scraped += len(rows)
                
                # Track booking categories
                for r in rows:
                    cat = r.get("booking_age_category", "unknown")
                    letter_categories[cat] = letter_categories.get(cat, 0) + 1
                    booking_categories[cat] = booking_categories.get(cat, 0) + 1
                
                L.log(f"      scraped {len(rows)} record(s) in {time.monotonic()-t0:.1f}s — upserting…")

                stats = _upserts(col, rows)
                letter_matched  += stats["matched"]
                letter_modified += stats["modified"]
                letter_upserted += stats["upserted"]
                L.log(f"      upserted={stats['upserted']} matched={stats['matched']} modified={stats['modified']}")

                # Enhanced success logging with categories
                for r in rows:
                    name = r.get("name", "UNKNOWN")
                    category = r.get("booking_age_category", "unknown")
                    L.log(f"[brazoria] SUCCESS: {name} [{category}]")

                time.sleep(LETTER_DELAY_SEC)

            per_letter[last_ch] = {
                "scraped":  letter_scraped,
                "matched":  letter_matched,
                "modified": letter_modified,
                "upserted": letter_upserted,
                "booking_categories": dict(sorted(letter_categories.items())),
            }
            totals["scraped"]  += letter_scraped
            totals["matched"]  += letter_matched
            totals["modified"] += letter_modified
            totals["upserted"] += letter_upserted

            L.log(
                f"✓ DONE last={last_ch}: scraped={letter_scraped} upserted={letter_upserted} "
                f"matched={letter_matched} modified={letter_modified} "
                f"(elapsed {time.monotonic()-t_letter0:.1f}s)"
            )
            
            # Log booking category breakdown for this letter
            if letter_categories:
                L.log(f"    booking age breakdown: {dict(sorted(letter_categories.items()))}")

        L.log("All requested letters processed." if not stop_requested["yes"] else "Stopped by user.")
        
        # Final summary with booking categories
        L.log("BOOKING AGE SUMMARY:")
        for category, count in sorted(booking_categories.items()):
            L.log(f"  {category}: {count} inmates")
            
    finally:
        client.close()

    return {
        "totals": totals, 
        "per_letter": per_letter, 
        "collection": f"{MONGO_DB}.{BRAZORIA_COLLECTION}",
        "booking_categories": dict(sorted(booking_categories.items())),
    }

# --- CLI ----------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser("Brazoria Jail (Tyler Public Access) → MongoDB Atlas ingester")

    # Exact-name mode (provide both to trigger)
    ap.add_argument("--last", default="", help="Exact last name (or Tyler-accepted partial)")
    ap.add_argument("--first", default="", help="Exact first name (or Tyler-accepted partial)")

    # Sweep mode (used when exact names are not both provided)
    ap.add_argument("--letters", default="A-Z",
                    help="Last-name letters or range: 'A-Z', 'A-M', or a set like 'SMT' or 'SMI'")
    ap.add_argument("--first-letters", default="A-Z",
                    help="First-name initials (same formats as --letters)")
    ap.add_argument("--append-wildcard", action="store_true",
                    help="Append '*' to both names (be mindful: last-name wildcards usually need ≥3 chars).")

    ap.add_argument("--since-days", type=int, default=60, help="Only include bookings within the last N days (default 60)")
    ap.add_argument("--no-details", action="store_true", help="Skip detail pages (charges/bond)")
    ap.add_argument("--tick-every", type=int, default=10, help="Log every N rows (0=off)")
    args = ap.parse_args()

    # If both --last and --first are provided, do exact mode; otherwise sweep mode.
    if args.last and args.first:
        out = _ingest_exact(args.last, args.first, include_details=not args.no_details,
                            verbose=True, tick_every=args.tick_every, since_days=args.since_days)
    else:
        out = ingest_all_letters(
            include_details=not args.no_details,
            letters=args.letters,
            first_letters=args.first_letters,
            verbose=True,
            tick_every=args.tick_every,
            append_wildcard=args.append_wildcard,
            since_days=args.since_days,
        )

    print(json.dumps(out, indent=2))