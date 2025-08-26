#!/usr/bin/env python3
import os, re, sys, time, datetime as dt
from typing import Any, Dict, Iterable, List, Optional, Tuple
from pymongo import MongoClient, UpdateOne, ASCENDING, DESCENDING
from bson import ObjectId

# --- Load .env so Atlas creds are available -------------------------------
from pathlib import Path
try:
    from dotenv import load_dotenv  # pip install python-dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

# ---------- Config ----------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB",  "warrantdb")

COUNTY_ALL = ["harris", "brazoria", "galveston", "fortbend", "jefferson"]

# Tunables
PROGRESS_EVERY = int(os.getenv("NORMALIZE_PROGRESS_EVERY", "1000"))  # print every N docs
CHUNK_SIZE     = int(os.getenv("NORMALIZE_CHUNK_SIZE", "5000"))      # stream chunk size

# ---------- Tiny logger ----------
class Logger:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
    def _hms(self) -> str:
        s = int(time.monotonic() - self.t0)
        return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"
    def info(self, msg: str) -> None:
        print(f"[{self._hms()}] {msg}", flush=True)

L = Logger()

# ---------- Utilities ----------
DATE_PATTERNS = [
    "%Y-%m-%d",
    "%m/%d/%Y", "%m/%d/%y",
    "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
]

def parse_date_maybe(s: Optional[str]) -> Optional[str]:
    if not s or not isinstance(s, str): return None
    s = s.strip()
    # quick yymmdd or mmddyy helpers
    if re.fullmatch(r"\d{6}", s):
        mm, dd, yy = s[:2], s[2:4], s[4:]
        year = 2000 + int(yy) if int(yy) < 70 else 1900 + int(yy)
        try: return dt.date(year, int(mm), int(dd)).isoformat()
        except Exception: pass
    for fmt in DATE_PATTERNS:
        try:
            if "%z" in fmt:
                return dt.datetime.strptime(s, fmt).date().isoformat()
            d = dt.datetime.strptime(s, fmt)
            return (d.date() if isinstance(d, dt.datetime) else d).isoformat()
        except Exception:
            continue
    try:
        return dt.datetime.fromisoformat(s.replace("Z","")).date().isoformat()
    except Exception:
        return None

def split_name(full: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not full: return None, None
    f = full.strip()
    if "," in f:
        last, first = [x.strip() or None for x in f.split(",", 1)]
        return first, last
    parts = f.split()
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    return f, None

def to_money(x: Any) -> Optional[float]:
    if x is None: return None
    if isinstance(x, (int, float)): return float(x)
    s = str(x).replace(",", "")
    m = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", s)
    return float(m.group(1)) if m else None

def pick(*vals):
    for v in vals:
        if v not in (None, "", []): return v
    return None

def build_norm_doc(
    county: str,
    category: Optional[str],
    full_name: Optional[str],
    dob: Optional[str],
    booking_date: Optional[str],
    offense: Optional[str],
    bond: Optional[str],
    bond_amount: Optional[float],
    booking_number: Optional[str],
    case_number: Optional[str],
    spn: Optional[str],
    source: str,
    source_id: Any,
    extra: Dict[str, Any],
) -> Dict[str, Any]:
    first, last = split_name(full_name)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    key = {
        "county": county,
        "category": (category or "Unknown"),
        "anchor": pick(booking_number, case_number, spn,
                       f"{(full_name or '').upper()}|{dob or ''}|{booking_date or ''}")
    }
    return {
        "_upsert_key": key,
        "county": county,
        "category": category or "Unknown",         # "Criminal" | "Civil" | "Unknown"
        "full_name": full_name,
        "first_name": first,
        "last_name": last,
        "dob": dob,
        "booking_date": booking_date,
        "offense": offense,
        "bond": bond,
        "bond_amount": bond_amount,
        "booking_number": booking_number,
        "case_number": case_number,
        "spn": spn,
        "source": source,               # original collection/source
        "source_id": str(source_id) if isinstance(source_id, ObjectId) else source_id,
        "extra": extra,                 # preserve everything else
        "normalized_at": now_iso,
    }

# ---------- Chunked streaming (Atlas-friendly) ----------
def stream_collection(col, query=None, projection=None, chunk: int = CHUNK_SIZE):
    """
    Yield documents from `col` in ascending _id order, chunk at a time,
    without using no_cursor_timeout (safe on Atlas free/low tiers).
    """
    query = dict(query or {})
    last_id = None
    while True:
        q = query.copy()
        if last_id is not None:
            q["_id"] = {"$gt": last_id}
        cur = col.find(q, projection).sort("_id", ASCENDING).limit(chunk)
        batch = 0
        for doc in cur:
            last_id = doc["_id"]
            batch += 1
            yield doc
        if batch < chunk:
            break

# ---------- Normalizers per county ----------
def norm_harris(doc: Dict[str, Any]) -> Dict[str, Any]:
    group = (doc.get("group") or "").strip() or None
    category = "Criminal" if (group and group.lower() == "criminal") else ("Civil" if group else "Unknown")
    full_name = pick(doc.get("name"),
                     ", ".join([doc.get("last_name",""), doc.get("first_middle","")]).strip(", ") or None)
    dob = pick(doc.get("dob"))
    booking_date = pick(doc.get("case_date"), doc.get("file_date"))
    offense = pick(doc.get("offense"))
    bond_amount = to_money(doc.get("bond_amount"))
    bond_note = pick(doc.get("bond_note"))
    booking_number = None
    case_number = pick(doc.get("case_number"))
    spn = pick(doc.get("spn"))
    return build_norm_doc(
        county="harris",
        category=category,
        full_name=full_name,
        dob=parse_date_maybe(dob),
        booking_date=parse_date_maybe(booking_date),
        offense=offense,
        bond=bond_note,
        bond_amount=bond_amount,
        booking_number=booking_number,
        case_number=case_number,
        spn=spn,
        source=doc.get("source","harris"),
        source_id=doc.get("_id"),
        extra={k:v for k,v in doc.items() if k not in {"_id"}},
    )

def norm_brazoria(doc: Dict[str, Any]) -> Dict[str, Any]:
    full_name = (doc.get("name") or doc.get("full_name") or "").upper() or None
    dob = None
    booking_date = pick(doc.get("booking_date_iso"), doc.get("booking_date"))
    offense = None
    if isinstance(doc.get("charges"), list) and doc["charges"]:
        offense = doc["charges"][1].get("charge") if len(doc["charges"])>1 else doc["charges"][0].get("charge")
    offense = offense or doc.get("charges_summary")
    bond_amount = to_money(doc.get("bond_total"))
    booking_number = pick(doc.get("booking_number"))
    case_number = None
    spn = None
    category = "Criminal"  # jail feed
    return build_norm_doc(
        county="brazoria",
        category=category,
        full_name=full_name,
        dob=parse_date_maybe(dob),
        booking_date=parse_date_maybe(booking_date),
        offense=offense,
        bond=None,
        bond_amount=bond_amount,
        booking_number=booking_number,
        case_number=case_number,
        spn=spn,
        source="brazoria_inmates",
        source_id=doc.get("_id"),
        extra={k:v for k,v in doc.items() if k not in {"_id"}},
    )

def norm_galveston(person: Dict[str, Any], event: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    full_name = pick(person.get("full_name"),
                     (person.get("name") if isinstance(person.get("name"), str) else None))
    dob = person.get("dob")
    arrest_date = (event or {}).get("arrest_date")
    total_bond = (event or {}).get("total_bond")
    charges = (event or {}).get("charges") or []
    offense = None
    if isinstance(charges, list) and charges:
        offense = charges[0].get("charge")
    booking_number = (event or {}).get("booking_number")
    return build_norm_doc(
        county="galveston",
        category="Criminal",
        full_name=full_name,
        dob=parse_date_maybe(dob),
        booking_date=parse_date_maybe(arrest_date),
        offense=offense,
        bond=None,
        bond_amount=to_money(total_bond),
        booking_number=booking_number,
        case_number=None,
        spn=None,
        source="galveston_p2c",
        source_id=person.get("_id") or (event or {}).get("_id"),
        extra={"person": {k:v for k,v in person.items() if k!="_id"},
               "event":  {k:v for k,v in (event or {}).items() if k!="_id"}},
    )

def norm_fortbend(doc: Dict[str, Any]) -> Dict[str, Any]:
    full_name = (doc.get("name") or doc.get("full_name") or "").upper() or None
    dob = parse_date_maybe(doc.get("dob"))
    booking_date = parse_date_maybe(pick(doc.get("booking_date_iso"), doc.get("booking_date")))
    offense = pick((doc.get("charges") or [{}])[0].get("charge") if isinstance(doc.get("charges"), list) else None,
                   doc.get("charge_summary"))
    bond_amount = to_money(doc.get("bond_total"))
    booking_number = doc.get("booking_number")
    return build_norm_doc(
        county="fortbend",
        category="Criminal",
        full_name=full_name,
        dob=dob,
        booking_date=booking_date,
        offense=offense,
        bond=None,
        bond_amount=bond_amount,
        booking_number=booking_number,
        case_number=None,
        spn=None,
        source="fortbend_jail",
        source_id=doc.get("_id"),
        extra={k:v for k,v in doc.items() if k not in {"_id"}},
    )

# --- Jefferson normalizer
def norm_jefferson(doc: Dict[str, Any]) -> Dict[str, Any]:
    full_name = pick(doc.get("full_name"), doc.get("name"))
    dob = parse_date_maybe(pick(doc.get("dob")))
    booking_date = parse_date_maybe(pick(doc.get("booking_date"), doc.get("arrest_date"), doc.get("booked_at")))
    # charges may be a list of dicts or a string summary
    offense = None
    ch = doc.get("charges")
    if isinstance(ch, list) and ch:
        offense = ch[0].get("charge") or ch[0].get("description")
    offense = pick(offense, doc.get("charge_summary"), doc.get("offense"))
    bond_amount = to_money(pick(doc.get("bond_total"), doc.get("total_bond"), doc.get("bond_amount"), doc.get("bond")))
    booking_number = pick(doc.get("booking_number"), doc.get("bookingNo"))
    return build_norm_doc(
        county="jefferson",
        category="Criminal",
        full_name=full_name,
        dob=dob,
        booking_date=booking_date,
        offense=offense,
        bond=None,
        bond_amount=bond_amount,
        booking_number=booking_number,
        case_number=None,
        spn=None,
        source=doc.get("source", "jefferson_jail"),
        source_id=doc.get("_id"),
        extra={k: v for k, v in doc.items() if k != "_id"},
    )

# ---------- Harvesters (read from existing collections) ----------
def _count_coll(db, name: str) -> int:
    try:
        return db[name].estimated_document_count()
    except Exception:
        return 0

def iter_harris(db) -> Iterable[Dict[str, Any]]:
    names = ("harris_bond", "harris_misfel", "harris_nafiling")
    for name in names:
        if name in db.list_collection_names():
            approx = _count_coll(db, name)
            L.info(f"Harris: scanning {name} (~{approx} docs) in chunks of {CHUNK_SIZE}")
            i = 0
            for d in stream_collection(db[name]):
                i += 1
                if i % PROGRESS_EVERY == 0:
                    L.info(f"Harris: processed {i} {name} docs…")
                yield norm_harris(d)
            L.info(f"Harris: finished {name} (processed {i})")

def iter_brazoria(db) -> Iterable[Dict[str, Any]]:
    name = "brazoria_inmates"
    if name in db.list_collection_names():
        approx = _count_coll(db, name)
        L.info(f"Brazoria: scanning {name} (~{approx} docs) in chunks of {CHUNK_SIZE}")
        i = 0
        for d in stream_collection(db[name]):
            i += 1
            if i % PROGRESS_EVERY == 0:
                L.info(f"Brazoria: processed {i} docs…")
            yield norm_brazoria(d)
        L.info(f"Brazoria: finished {name} (processed {i})")

def iter_galveston(db) -> Iterable[Dict[str, Any]]:
    persons_name = "persons"
    ev_name = "custody_events"

    if persons_name not in db.list_collection_names():
        L.info("Galveston: persons collection missing; nothing to do.")
        return

    # Build latest custody event per person (if the collection exists)
    latest = {}
    if ev_name in db.list_collection_names():
        L.info("Galveston: indexing latest custody_events by person_id (chunked)…")
        ev_fields = {
            "person_id": 1, "scraped_at": 1,
            "booking_number": 1, "status": 1, "booked_at": 1, "released_at": 1,
            "source_url": 1, "charges": 1, "bonds": 1, "total_bond": 1,
            "agency": 1, "arrest_date": 1, "race": 1, "sex": 1, "age": 1,
        }
        ev_count = 0
        for ev in stream_collection(db[ev_name], {"county": "Galveston"}, projection=ev_fields):
            ev_count += 1
            pid = ev.get("person_id")
            if not pid:
                continue
            prev = latest.get(pid)
            cur_ts = ev.get("scraped_at","")
            if not prev or cur_ts > prev.get("scraped_at",""):
                latest[pid] = ev
        L.info(f"Galveston: events indexed = {ev_count}; persons with events = {len(latest)}")
    else:
        L.info("Galveston: custody_events missing; will normalize from persons only.")

    # Stream persons with p2c links
    L.info("Galveston: streaming persons with p2c links (chunked)…")
    i = 0
    for p in stream_collection(db[persons_name], {"links.rel": "p2c_detail"}, projection={"_id":1,"full_name":1,"dob":1}):
        i += 1
        if i % PROGRESS_EVERY == 0:
            L.info(f"Galveston: processed {i} persons…")
        ev = latest.get(p["_id"]) if latest else None
        yield norm_galveston(p, ev)
    L.info(f"Galveston: finished persons (processed {i})")

def iter_fortbend(db) -> Iterable[Dict[str, Any]]:
    name = "fortbend_inmates"
    if name in db.list_collection_names():
        approx = _count_coll(db, name)
        L.info(f"Fort Bend: scanning {name} (~{approx} docs) in chunks of {CHUNK_SIZE}")
        i = 0
        for d in stream_collection(db[name]):
            i += 1
            if i % PROGRESS_EVERY == 0:
                L.info(f"Fort Bend: processed {i} docs…")
            yield norm_fortbend(d)
        L.info(f"Fort Bend: finished {name} (processed {i})")

# --- Jefferson iterator
def iter_jefferson(db) -> Iterable[Dict[str, Any]]:
    # Support both the correct and a common misspelling seen in Atlas
    candidates = ["jefferson_events", "jeffereson_events"]
    existing = [n for n in candidates if n in db.list_collection_names()]

    if not existing:
        L.info("Jefferson: jefferson_events/jeffereson_events not found; nothing to normalize.")
        return

    name = existing[0]
    approx = _count_coll(db, name)
    L.info(f"Jefferson: scanning {name} (~{approx} docs) in chunks of {CHUNK_SIZE}")
    i = 0
    for d in stream_collection(db[name]):
        i += 1
        if i % PROGRESS_EVERY == 0:
            L.info(f"Jefferson: processed {i} docs…")
        yield norm_jefferson(d)
    L.info(f"Jefferson: finished {name} (processed {i})")

# ---------- Upsert ----------
def upsert_normals(db, county: str, docs: Iterable[Dict[str, Any]], *, dry_run: bool=False) -> Tuple[int,int,int]:
    tgt = f"simple_{county}"
    col = db[tgt]

    if not dry_run:
        try:
            L.info(f"{county.title()}: ensuring indexes on {tgt}…")
            col.create_index([("_upsert_key.county", ASCENDING),
                              ("_upsert_key.category", ASCENDING),
                              ("_upsert_key.anchor", ASCENDING)], unique=True, background=True)
            col.create_index([("booking_date", ASCENDING)], background=True)
            col.create_index([("dob", ASCENDING)], background=True)
            col.create_index([("last_name", ASCENDING), ("first_name", ASCENDING)], background=True)
        except Exception:
            pass

    ops: List[UpdateOne] = []
    ins = upd = seen = 0
    batch = 0
    t_batch = time.monotonic()

    for d in docs:
        seen += 1
        if seen % PROGRESS_EVERY == 0:
            L.info(f"{county.title()}: normalized {seen} docs…")

        if dry_run:
            continue

        key = d["_upsert_key"]
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        ops.append(UpdateOne(
            {"_upsert_key": key},
            {"$set": {**{k:v for k,v in d.items() if k != "_upsert_key"},
                      "normalized_at": now_iso},
             "$setOnInsert": {"created_at": now_iso}},
            upsert=True
        ))
        batch += 1

        if len(ops) == 1000:
            dt_s = time.monotonic() - t_batch
            L.info(f"{county.title()}: writing batch of 1000 (took {dt_s:.1f}s)…")
            res = col.bulk_write(ops, ordered=False)
            upd += (res.modified_count or 0)
            ins += (res.upserted_count or 0)
            ops = []
            batch = 0
            t_batch = time.monotonic()

    if not dry_run and ops:
        dt_s = time.monotonic() - t_batch
        L.info(f"{county.title()}: writing final batch of {len(ops)} (took {dt_s:.1f}s)…")
        res = col.bulk_write(ops, ordered=False)
        upd += (res.modified_count or 0)
        ins += (res.upserted_count or 0)

    return ins, upd, seen

# ---------- CLI ----------
def main():
    import argparse
    ap = argparse.ArgumentParser("Normalize county data into simple_* collections")
    ap.add_argument(
        "--county",
        default="all",
        choices=["all", "harris", "brazoria", "galveston", "fortbend", "jefferson"],
        help="Which county to normalize (default: all)"
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse & show progress but do not write to Mongo")
    args = ap.parse_args()

    L.info(f"Connecting to Mongo… db={MONGO_DB}")
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    L.info("Connected.")

    total_ins = total_upd = total_seen = 0

    def run_one(county: str):
        nonlocal total_ins, total_upd, total_seen
        L.info(f"=== START {county.upper()} ===")

        if county == "harris":
            docs = iter_harris(db)
        elif county == "brazoria":
            docs = iter_brazoria(db)
        elif county == "galveston":
            docs = iter_galveston(db)
        elif county == "fortbend":
            docs = iter_fortbend(db)
        elif county == "jefferson":
            docs = iter_jefferson(db)
        else:
            return

        ins, upd, seen = upsert_normals(db, county, docs, dry_run=args.dry_run)
        L.info(f"=== DONE {county.upper()} | seen={seen} inserted={ins} updated={upd} ===")
        total_ins += ins; total_upd += upd; total_seen += seen

    if args.county == "all":
        for c in COUNTY_ALL:
            run_one(c)
    else:
        run_one(args.county)

    L.info(f"All done. Total seen={total_seen} inserted={total_ins} updated={total_upd} (dry_run={args.dry_run})")

if __name__ == "__main__":
    main()