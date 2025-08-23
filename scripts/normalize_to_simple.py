#!/usr/bin/env python3
import os, re, sys, datetime as dt
from typing import Any, Dict, Iterable, List, Optional, Tuple
from pymongo import MongoClient, UpdateOne, ASCENDING
from bson import ObjectId

# ---------- Config ----------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB",  "warrantdb")

COUNTY_ALL = ["harris", "brazoria", "galveston", "fortbend"]

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
    # try digits like 2025-08-22T... without Z
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

# ---------- Normalizers per county ----------
def norm_harris(doc: Dict[str, Any]) -> Dict[str, Any]:
    group = (doc.get("group") or "").strip() or None
    category = "Criminal" if (group and group.lower() == "criminal") else ("Civil" if group else "Unknown")
    # full name may be in different places
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
    # We’ll accept either a `persons` doc or a `custody_events` doc; event wins for dates/offenses.
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

# ---------- Harvesters (read from existing collections) ----------
def iter_harris(db) -> Iterable[Dict[str, Any]]:
    for name in ("harris_bond", "harris_misfel", "harris_nafiling"):
        if name in db.list_collection_names():
            for d in db[name].find({}, no_cursor_timeout=True):
                yield norm_harris(d)

def iter_brazoria(db) -> Iterable[Dict[str, Any]]:
    name = "brazoria_inmates"
    if name in db.list_collection_names():
        for d in db[name].find({}, no_cursor_timeout=True):
            yield norm_brazoria(d)

def iter_galveston(db) -> Iterable[Dict[str, Any]]:
    # prefer pairing persons with most recent custody_event (if any)
    persons = list(db["persons"].find({"links.rel":"p2c_detail"}, {"_id":1,"full_name":1,"dob":1}) if "persons" in db.list_collection_names() else [])
    ev_coll = db["custody_events"] if "custody_events" in db.list_collection_names() else None
    if ev_coll:
        # map person_id -> latest event
        latest = {}
        for ev in ev_coll.find({"county":"Galveston"}):
            pid = ev.get("person_id")
            if not pid: continue
            prev = latest.get(pid)
            cur_ts = ev.get("scraped_at","")
            if not prev or cur_ts > prev.get("scraped_at",""):
                latest[pid] = ev
        for p in persons:
            ev = latest.get(p["_id"])
            yield norm_galveston(p, ev)
    else:
        # fall back to persons only
        for p in persons:
            yield norm_galveston(p, None)

def iter_fortbend(db) -> Iterable[Dict[str, Any]]:
    name = "fortbend_inmates"
    if name in db.list_collection_names():
        for d in db[name].find({}, no_cursor_timeout=True):
            yield norm_fortbend(d)

# ---------- Upsert ----------
def upsert_normals(db, county: str, docs: Iterable[Dict[str, Any]]) -> Tuple[int,int]:
    tgt = f"simple_{county}"
    col = db[tgt]
    try:
        col.create_index([("_upsert_key.county", ASCENDING),
                          ("_upsert_key.category", ASCENDING),
                          ("_upsert_key.anchor", ASCENDING)], unique=True, background=True)
        col.create_index([("booking_date", ASCENDING)], background=True)
        col.create_index([("dob", ASCENDING)], background=True)
        col.create_index([("last_name", ASCENDING), ("first_name", ASCENDING)], background=True)
    except Exception:
        pass

    ops = []
    ins = upd = 0
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for d in docs:
        key = d["_upsert_key"]
        ops.append(UpdateOne(
            {"_upsert_key": key},
            {"$set": {**{k:v for k,v in d.items() if k != "_upsert_key"},
                      "normalized_at": now},
             "$setOnInsert": {"created_at": now}},
            upsert=True
        ))
        if len(ops) == 1000:
            res = col.bulk_write(ops, ordered=False)
            upd += (res.modified_count or 0)
            ins += (res.upserted_count or 0)
            ops = []
    if ops:
        res = col.bulk_write(ops, ordered=False)
        upd += (res.modified_count or 0)
        ins += (res.upserted_count or 0)
    return ins, upd

# ---------- CLI ----------
def main():
    import argparse
    ap = argparse.ArgumentParser("Normalize county data into simple_* collections")
    ap.add_argument("--county", default="all", choices=["all"] + COUNTY_ALL,
                    help="Which county to normalize (default: all)")
    args = ap.parse_args()

    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]

    total_ins = total_upd = 0

    def run_one(county: str):
        nonlocal total_ins, total_upd
        if county == "harris":
            docs = iter_harris(db)
        elif county == "brazoria":
            docs = iter_brazoria(db)
        elif county == "galveston":
            docs = iter_galveston(db)
        elif county == "fortbend":
            docs = iter_fortbend(db)
        else:
            return
        ins, upd = upsert_normals(db, county, docs)
        print(f"[normalize] {county}: inserted={ins} updated={upd}")
        total_ins += ins; total_upd += upd

    if args.county == "all":
        for c in COUNTY_ALL:
            run_one(c)
    else:
        run_one(args.county)

    print(f"Done. Total inserted={total_ins} updated={total_upd}")

if __name__ == "__main__":
    main()