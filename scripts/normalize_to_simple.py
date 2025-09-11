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

# Normalize semantics
ZERO_BOND_AS_NULL = os.getenv("NORMALIZE_ZERO_BOND_AS_NULL", "0") == "1"

# How often to print a normalized sample (0 = disabled)
SAMPLE_EVERY   = int(os.getenv("NORMALIZE_SAMPLE_EVERY", "500"))

def _format_sample(d: Dict[str, Any]) -> str:
    # Show just the most important, human-checkable fields
    keys = [
        "county", "category",
        "full_name", "first_name", "last_name",
        "dob", "booking_date",
        "offense", "bond", "bond_amount",
        "booking_number", "case_number", "spn",
        "agency", "facility",
        "source", "time_bucket"
    ]
    # Build a stable, short line
    parts = []
    for k in keys:
        v = d.get(k)
        if v in (None, "", []):
            continue
        # keep long strings short for console
        s = str(v)
        if len(s) > 120:
            s = s[:117] + "..."
        parts.append(f"{k}={s}")
    return " | ".join(parts)

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

# --- Helper: compute_time_bucket and extract_charge_bonds
def compute_time_bucket(booking_date_iso: Optional[str]) -> Optional[str]:
    """
    Return a human-friendly recency bucket based on booking_date (YYYY-MM-DD).
    Buckets (most specific first):
      - 24_hours_or_less
      - 48_hours_or_less
      - 72_hours_or_less
      - 7_days_or_less
      - 0_to_30_days
      - 30_to_60_days
      - 60_to_180_days
      - 180_to_365_days
      - 365_days_or_older
    """
    if not booking_date_iso:
        return None
    try:
        d = dt.date.fromisoformat(booking_date_iso)
    except Exception:
        return None
    today = dt.datetime.now(dt.timezone.utc).date()
    delta = (today - d).days
    # Convert sub-day thresholds using integer days where 0 = today, 1 = yesterday, etc.
    if delta == 0:
        return "24_hours_or_less"
    if delta == 1:
        return "48_hours_or_less"
    if delta == 2:
        return "72_hours_or_less"
    if delta <= 7:
        return "7_days_or_less"
    if delta <= 30:
        return "0_to_30_days"
    if delta <= 60:
        return "30_to_60_days"
    if delta <= 180:
        return "60_to_180_days"
    if delta <= 365:
        return "180_to_365_days"
    return "365_days_or_older"

def extract_charge_bonds(charges, bonds=None) -> List[Dict[str, Any]]:
    """
    Build a normalized list of per-charge bond details when available.
    Each item looks like:
      {"charge": str|None, "bond": original_note|None, "amount": float|None}
    We attempt to read common shapes used across sources.
    """
    out: List[Dict[str, Any]] = []
    # From 'charges' list where each charge may have 'bond', 'bond_amount', 'amount', or similar.
    if isinstance(charges, list):
        for ch in charges:
            if not isinstance(ch, dict):
                # Sometimes charges are strings.
                out.append({"charge": str(ch), "bond": None, "amount": None})
                continue
            desc = ch.get("charge") or ch.get("description") or ch.get("offense") or ch.get("desc")
            # Various possible numeric fields
            amt = None
            for k in ("bond_amount", "bond_total", "amount", "bond", "bond/type", "fine/crt costs"):
                if k in ch and ch.get(k) not in (None, "", []):
                    amt = to_money(ch.get(k))
                    if amt is not None:
                        break
            note = ch.get("bond_note") or ch.get("bond") if isinstance(ch.get("bond"), str) else None
            out.append({"charge": desc, "bond": note, "amount": amt})
    # Some sources keep a parallel 'bonds' list with descriptions & amounts
    if isinstance(bonds, list):
        for b in bonds:
            if isinstance(b, dict):
                desc = b.get("desc") or b.get("description") or b.get("charge")
                amt = to_money(b.get("amount") or b.get("bond_amount") or b.get("value"))
                out.append({"charge": desc, "bond": None, "amount": amt})
    return out

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

# --- Agency cleaning for Brazoria (and similar) ---
AGENCY_SUFFIXES = [
    " POLICE DEPARTMENT",
    " POLICE DEPT",
    " SHERIFF'S OFFICE",
    " SHERIFFS OFFICE",
    " SHERIFF OFFICE",
    " CONSTABLE",
    " DEPARTMENT OF PUBLIC SAFETY",
    " DPS",
]

def clean_agency(name: Optional[str]) -> Optional[str]:
    if not name or not isinstance(name, str):
        return None
    s = name.strip()
    # If there's a long uppercase tail (likely offense text), try to truncate at known agency suffixes.
    up = s.upper()
    best = None
    for suf in AGENCY_SUFFIXES:
        idx = up.find(suf)
        if idx != -1:
            j = idx + len(suf)
            # keep the shortest reasonable cutoff beyond which text is usually charges
            cand = s[:j]
            if not best or len(cand) > len(best):
                best = cand
    # Fallback: if string contains two or more consecutive spaces, take the first token chunk
    if not best and "  " in s:
        best = s.split("  ", 1)[0].strip()
    # Final sanity: if best is too short, ignore
    if best and len(best) >= 6:
        return best
    return s

# --- Agency detector helper ---
AGENCY_KEYWORDS = [
    "POLICE", "SHERIFF", "CONSTABLE", "DEPARTMENT", "DPS", "CITY OF", "PD"
]

def is_agency_like(s: Optional[str]) -> bool:
    if not s or not isinstance(s, str):
        return False
    up = s.upper()
    return any(k in up for k in AGENCY_KEYWORDS)

def pick(*vals):
    for v in vals:
        if v not in (None, "", []): return v
    return None

# Normalize bond amount according to config
def normalize_bond_amount(val: Optional[float]) -> Optional[float]:
    if ZERO_BOND_AS_NULL and isinstance(val, (int, float)) and float(val) == 0.0:
        return None
    return val

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
    charge_bonds: Optional[List[Dict[str, Any]]] = None,
    agency: Optional[str] = None,
    facility: Optional[str] = None,
    race: Optional[str] = None,
    sex: Optional[str] = None,
) -> Dict[str, Any]:
    first, last = split_name(full_name)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    key = {
        "county": county,
        "category": (category or "Unknown"),
        "anchor": pick(booking_number, case_number, spn,
                       f"{(full_name or '').upper()}|{dob or ''}|{booking_date or ''}")
    }
    time_bucket = compute_time_bucket(booking_date)
    cb = charge_bonds or []
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
        "charge_bonds": cb,             # per-charge bond breakdowns when available
        "time_bucket": time_bucket,     # recency categorization
        "booking_number": booking_number,
        "case_number": case_number,
        "spn": spn,
        "agency": agency,
        "facility": facility,
        "race": race,
        "sex": sex,
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
    # If main field missing, some rows encode amounts in bond_note like "00000100"
    if bond_amount is None:
        alt_from_note = to_money(doc.get("bond_note"))
        if alt_from_note is not None:
            bond_amount = alt_from_note
    bond_note = pick(doc.get("bond_note"))
    src = (doc.get("source") or "").lower()
    # Misfel files are known to have inflated/scaled bond_amount values; avoid passing bad numbers downstream
    if "misfel" in src and isinstance(bond_amount, (int, float)) and bond_amount >= 500_000:
        # Keep the upstream value in extra (already preserved), but do not emit it as normalized bond_amount
        bond_amount = None
    # Map demographic codes when available
    race_map = {
        "A": "Asian",
        "B": "Black",
        "W": "White",
        "I": "Indigenous",
        "O": "Other",
        "U": "Unknown",
        "X": "Unknown",
    }
    sex_map = {
        "M": "Male",
        "J": "Male",   # some Harris feeds use J for Male
        "F": "Female",
        "X": "Unknown",
        "U": "Unknown",
    }
    race = race_map.get(str(doc.get("race_code") or "").strip().upper())
    sex  = sex_map.get(str(doc.get("sex_code")  or "").strip().upper())
    booking_number = None
    case_number = pick(doc.get("case_number"))
    spn = pick(doc.get("spn"))
    cb = extract_charge_bonds(doc.get("charges"))
    return build_norm_doc(
        county="harris",
        category=category,
        full_name=full_name,
        dob=parse_date_maybe(dob),
        booking_date=parse_date_maybe(booking_date),
        offense=offense,
        bond=bond_note,
        bond_amount=normalize_bond_amount(bond_amount),
        booking_number=booking_number,
        case_number=case_number,
        spn=spn,
        agency=None,
        facility=None,
        race=race,
        sex=sex,
        source=doc.get("source","harris"),
        source_id=doc.get("_id"),
        extra={k:v for k,v in doc.items() if k not in {"_id"}},
        charge_bonds=cb,
    )

def norm_brazoria(doc: Dict[str, Any]) -> Dict[str, Any]:
    full_name = (doc.get("name") or doc.get("full_name") or "").upper() or None
    dob = None
    booking_date = pick(doc.get("booking_date_iso"), doc.get("booking_date"))
    # offense: pick first non-null charge from charges list
    offense = None
    ch = doc.get("charges")
    if isinstance(ch, list):
        for item in ch:
            if isinstance(item, dict) and item.get("charge"):
                offense = item.get("charge"); break
    offense = offense or doc.get("charges_summary")
    bond_amount = to_money(doc.get("bond_total"))
    if bond_amount is None:
        # try summing from per-charge fields (bond/type, fine/crt costs, etc.)
        parts = extract_charge_bonds(doc.get("charges"))
        vals = [p.get("amount") for p in parts if p.get("amount") is not None]
        bond_amount = sum(vals) if vals else None
    bond_amount = normalize_bond_amount(bond_amount)
    booking_number = pick(doc.get("booking_number"))
    case_number = None
    spn = None
    category = "Criminal"  # jail feed
    # Heuristic facility label when coming from Brazoria jail site
    facility = None
    try:
        url = doc.get("detail_url") or ""
        if isinstance(url, str) and "brazoriacountytx.gov" in url.lower():
            facility = "Brazoria County Jail"
    except Exception:
        pass
    cb = extract_charge_bonds(doc.get("charges"))
    # Prefer a cleaned arresting_agency only if it looks like an agency; otherwise, use charges_summary if it looks like an agency
    raw_agency = clean_agency(doc.get("arresting_agency"))
    cs = doc.get("charges_summary")
    if is_agency_like(raw_agency):
        agency_val = raw_agency
    elif is_agency_like(cs):
        agency_val = cs
    else:
        agency_val = None
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
        agency=agency_val,
        facility=facility,
        race=None,
        sex=None,
        source="brazoria_inmates",
        source_id=doc.get("_id"),
        extra={k:v for k,v in doc.items() if k not in {"_id"}},
        charge_bonds=cb,
    )

def norm_galveston(person: Dict[str, Any], event: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    full_name = pick(person.get("full_name"),
                     (person.get("name") if isinstance(person.get("name"), str) else None))
    dob = person.get("dob")
    arrest_date = (event or {}).get("arrest_date")
    booked_at   = (event or {}).get("booked_at")
    total_bond  = (event or {}).get("total_bond")
    charges     = (event or {}).get("charges") or []
    offense = None
    if isinstance(charges, list) and charges:
        for ch in charges:
            if isinstance(ch, dict) and ch.get("charge"):
                offense = ch.get("charge"); break
    booking_number = (event or {}).get("booking_number")
    cb = extract_charge_bonds((event or {}).get("charges"), (event or {}).get("bonds"))
    # Robust bond_amount: prefer total_bond; if empty/unparsable, sum per-charge amounts
    bond_amount_val = to_money(total_bond)
    if bond_amount_val is None:
        parts = [p.get("amount") for p in cb if p.get("amount") is not None]
        if parts:
            bond_amount_val = sum(parts)
    bond_amount_val = normalize_bond_amount(bond_amount_val)

    return build_norm_doc(
        county="galveston",
        category="Criminal",
        full_name=full_name,
        dob=parse_date_maybe(dob),
        booking_date=parse_date_maybe(pick(booked_at, arrest_date)),
        offense=offense,
        bond=None,
        bond_amount=bond_amount_val,
        booking_number=booking_number,
        case_number=None,
        spn=None,
        agency=(event or {}).get("agency"),
        facility=(event or {}).get("facility"),
        race=(event or {}).get("race"),
        sex=(event or {}).get("sex"),
        source="galveston_events",
        source_id=person.get("_id") or (event or {}).get("_id"),
        extra={"person": {k:v for k,v in person.items() if k!="_id"},
               "event":  {k:v for k,v in (event or {}).items() if k!="_id"}},
        charge_bonds=cb,
    )

def norm_fortbend(doc: Dict[str, Any]) -> Dict[str, Any]:
    full_name = (doc.get("name") or doc.get("full_name") or "").upper() or None
    dob = parse_date_maybe(doc.get("dob"))
    booking_date = parse_date_maybe(pick(doc.get("booking_date_iso"), doc.get("booking_date")))
    offense = None
    ch = doc.get("charges")
    if isinstance(ch, list):
        for item in ch:
            if isinstance(item, dict) and (item.get("charge") or item.get("charge description")):
                offense = item.get("charge") or item.get("charge description")
                break
    offense = offense or doc.get("charge_summary")
    bond_amount = to_money(doc.get("bond_total"))
    if bond_amount is None:
        # Fallback: sum per-charge amounts if present
        parts = extract_charge_bonds(doc.get("charges"))
        vals = [p.get("amount") for p in parts if p.get("amount") is not None]
        bond_amount = sum(vals) if vals else None
    bond_amount = normalize_bond_amount(bond_amount)
    booking_number = doc.get("booking_number")
    cb = extract_charge_bonds(doc.get("charges"))
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
        agency=None,
        facility=None,
        race=None,
        sex=None,
        source="fortbend_jail",
        source_id=doc.get("_id"),
        extra={k:v for k,v in doc.items() if k not in {"_id"}},
        charge_bonds=cb,
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
    bond_amount = normalize_bond_amount(bond_amount)
    booking_number = pick(doc.get("booking_number"), doc.get("bookingNo"))
    cb = extract_charge_bonds(doc.get("charges"))
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
        agency=doc.get("agency"),
        facility=doc.get("facility"),
        race=doc.get("race"),
        sex=doc.get("sex"),
        source=doc.get("source", "jefferson_jail"),
        source_id=doc.get("_id"),
        extra={k: v for k, v in doc.items() if k != "_id"},
        charge_bonds=cb,
    )

# ---------- Harvesters (read from existing collections) ----------
def _count_coll(db, name: str) -> int:
    try:
        return db[name].estimated_document_count()
    except Exception:
        return 0

def iter_harris(db) -> Iterable[Dict[str, Any]]:
    """Yield normalized Harris docs from bond + nafiling only; MISFEL is explicitly skipped."""
    configured = ("harris_bond", "harris_nafiling")
    L.info(f"Harris: configured sources = {configured}; MISFEL is skipped")

    # Always guard against MISFEL even if another code path attempts to include it
    candidates = ("harris_bond", "harris_nafiling", "harris_misfel")
    for name in candidates:
        if name == "harris_misfel":
            if name in db.list_collection_names():
                L.info("Harris: skipping harris_misfel (redundant/low-quality bond values)")
            continue
        if name in db.list_collection_names():
            approx = _count_coll(db, name)
            L.info(f"Harris: scanning {name} (~{approx} docs) in chunks of {CHUNK_SIZE}")
            i = 0
            for d in stream_collection(db[name]):
                i += 1
                # Guard: never emit MISFEL docs even if they appear in a configured source by mistake
                src_val = (d.get("source") or "").lower()
                if "misfel" in src_val:
                    if i % PROGRESS_EVERY == 0:
                        L.info(f"Harris: skipped MISFEL doc injected into {name}…")
                    continue
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
    ev_candidates = ["galveston_events", "custody_events"]
    ev_name = next((n for n in ev_candidates if n in db.list_collection_names()), None)

    if persons_name not in db.list_collection_names():
        L.info("Galveston: persons collection missing; nothing to do.")
        return

    # Build latest custody event per person (if the collection exists)
    latest_by_pid = {}
    latest_by_url = {}
    if ev_name:
        L.info(f"Galveston: indexing latest {ev_name} by person_id (chunked)…")
        ev_fields = {
            "person_id": 1, "scraped_at": 1,
            "booking_number": 1, "status": 1, "booked_at": 1, "released_at": 1,
            "source_url": 1, "charges": 1, "bonds": 1, "total_bond": 1,
            "agency": 1, "arrest_date": 1, "race": 1, "sex": 1, "age": 1,
            "facility": 1,
        }
        ev_count = 0
        for ev in stream_collection(db[ev_name], projection=ev_fields):
            ev_count += 1
            pid = ev.get("person_id")
            # Normalize person_id type: events store person_id as a hex string, while persons._id is an ObjectId
            if isinstance(pid, str):
                try:
                    pid = ObjectId(pid)
                except Exception:
                    # leave as-is if not a valid ObjectId hex string
                    pass
            if pid:
                prev = latest_by_pid.get(pid)
                cur_ts = (ev.get("scraped_at") or "")
                prev_ts = (prev.get("scraped_at") if prev else "")
                if not prev or str(cur_ts) > str(prev_ts):
                    latest_by_pid[pid] = ev
            # Index by source_url (newest-wins)
            url = ev.get("source_url")
            if url:
                prev_url_ev = latest_by_url.get(url)
                cur_ts2 = (ev.get("scraped_at") or "")
                prev_ts2 = (prev_url_ev.get("scraped_at") if prev_url_ev else "")
                if not prev_url_ev or str(cur_ts2) > str(prev_ts2):
                    latest_by_url[url] = ev
        L.info(f"Galveston: events indexed = {ev_count}; persons with events (by_id) = {len(latest_by_pid)}; by_url = {len(latest_by_url)}")
    else:
        L.info("Galveston: custody_events/galveston_events missing; will normalize from persons only.")

    # Stream persons with p2c links
    L.info("Galveston: streaming persons with p2c links (chunked)…")
    i = 0
    for p in stream_collection(db[persons_name], {"links.rel": "p2c_detail"}, projection={"_id":1,"full_name":1,"dob":1}):
        i += 1
        if i % PROGRESS_EVERY == 0:
            L.info(f"Galveston: processed {i} persons…")
        ev = None
        if latest_by_pid:
            ev = latest_by_pid.get(p["_id"]) or ev
        if not ev and latest_by_url:
            # find the person's P2C link and join by URL
            links = p.get("links") or []
            for link in links:
                if isinstance(link, dict) and link.get("rel") == "p2c_detail" and link.get("url"):
                    ev = latest_by_url.get(link["url"]) or ev
                    if ev:
                        break
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

        if SAMPLE_EVERY and (seen % SAMPLE_EVERY == 0):
            try:
                L.info(f"{county.title()}: sample → " + _format_sample(d))
            except Exception:
                # Never let sampling break normalization
                pass

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