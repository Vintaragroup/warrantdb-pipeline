"""
Generic county roster vs Atlas comparison
----------------------------------------
Compare an official county inmate roster (PDF) against the normalized
Mongo Atlas data in simple_{county}. Designed to work without changing
any existing ingestion/normalization code.

Usage:
  MONGO_URI=... MONGO_DB=warrantdb \
  python -m scripts.compare_roster_county --county jefferson /path/to/roster.pdf

Output:
  - Console summary (matches / missing_on_db / extra_in_db)
  - CSV at ./debug/{county}/roster_compare_<timestamp>.csv

Dependencies:
  pip install pdfminer.six pymongo python-dateutil

Counties supported (initial): harris, brazoria, galveston, fortbend, jefferson
You can extend PARSERS with a county-specific parser if a roster format differs.
"""
import os
import re
import sys
import csv
import argparse
import datetime as dt
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Callable

from pymongo import MongoClient
from dateutil import parser as dtparser

# Optional: auto-load .env if present so MONGO_URI/MONGO_DB are available
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# --- PDF parsing dependency (pdfminer) ---
try:
    from pdfminer.high_level import extract_text
except Exception as e:  # pragma: no cover
    print("pdfminer.six not installed. Run: pip install pdfminer.six", file=sys.stderr)
    raise

# ---------------------------
# Generic parsing primitives
# ---------------------------
NAME_LINE_RE_GENERIC = re.compile(r"^[A-Z'\-\.]+,\s+[A-Z][A-Z\-\.' ]*(?:\s+[IVX]{1,3})?\b")
DOB_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")


def _strip_punct(s: str) -> str:
    return re.sub(r"[^A-Z ]", "", s.upper())


def _norm_key_first_last(first: str, last: str) -> str:
    return _strip_punct(f"{first} {last}").strip()


def _norm_key_initial_last(first: str, last: str) -> str:
    fi = (first[:1] if first else "").upper()
    return _strip_punct(f"{fi}. {last}").replace(" ", "").replace(".", "")


def _parse_dob_any(s: str) -> Optional[str]:
    try:
        d = dtparser.parse(s, dayfirst=False, yearfirst=False).date()
        return d.isoformat()
    except Exception:
        return None


# ---------------------------
# County-specific PDF parsers
# ---------------------------
# Each parser returns a list of dicts with keys:
# { line, first, last, key, key_initial, dob? }


def parse_pdf_generic(pdf_path: Path) -> List[Dict]:
    text = extract_text(str(pdf_path)) or ""
    out: List[Dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if NAME_LINE_RE_GENERIC.match(line):
            # Expect format "LAST, FIRST MIDDLE ..."
            try:
                last, first = line.split(",", 1)
            except ValueError:
                continue
            last = last.strip().upper()
            # Trim at first double-space or known tokens (sex/race/etc.)
            first = re.split(r"\s{2,}|\s+MALE|\s+FEMALE|\s+BLACK|\s+WHITE|\s+HISPANIC|\s+UNAVAILABLE",
                             first.strip(), 1)[0]
            first = re.sub(r"\s+", " ", first.upper()).strip()
            dob = None
            m = DOB_RE.search(line)
            if m:
                dob = _parse_dob_any(m.group(1))
            rec = {
                "line": line,
                "first": first,
                "last": last,
                "key": _norm_key_first_last(first, last),
                "key_initial": _norm_key_initial_last(first, last),
                "dob": dob,
            }
            out.append(rec)
    # De-dup while preserving order (by key+dob)
    seen = set()
    uniq: List[Dict] = []
    for r in out:
        tup = (r["key"], r.get("dob"))
        if tup not in seen:
            uniq.append(r)
            seen.add(tup)
    return uniq


# For now Jefferson uses generic; add specialized parsers here if needed later
PARSERS: Dict[str, Callable[[Path], List[Dict]]] = {
    "jefferson": parse_pdf_generic,
    "galveston": parse_pdf_generic,
    "brazoria": parse_pdf_generic,
    "fortbend": parse_pdf_generic,
    "harris": parse_pdf_generic,
}


# ---------------------------
# DB access & indexing
# ---------------------------

def _db():
    uri = os.environ["MONGO_URI"]
    name = os.environ.get("MONGO_DB", "warrantdb")
    return MongoClient(uri)[name]


def build_db_index(db, county: str) -> Tuple[Dict[str, List[Dict]], Dict[str, List[Dict]], Dict[str, List[Dict]]]:
    """
    Returns three indices for simple_{county}:
      idx_key:        "FIRST LAST" -> [doc,...]
      idx_initial:    "F.LAST"     -> [doc,...]
      idx_last_dob:   "LAST|YYYY-MM-DD" -> [doc,...]
    """
    coll_name = f"simple_{county}"
    if coll_name not in db.list_collection_names():
        return {}, {}, {}

    idx_key: Dict[str, List[Dict]] = {}
    idx_initial: Dict[str, List[Dict]] = {}
    idx_last_dob: Dict[str, List[Dict]] = {}

    proj = {"_id": 1, "full_name": 1, "first_name": 1, "last_name": 1, "dob": 1, "booking_number": 1}
    for d in db[coll_name].find({}, proj):
        first = (d.get("first_name") or "").upper().strip()
        last = (d.get("last_name") or "").upper().strip()
        full = (d.get("full_name") or "").upper().strip()
        if not (first and last) and full:
            if "," in full:
                parts = [p.strip() for p in full.split(",", 1)]
                last = last or parts[0]
                first = first or parts[1]
            else:
                parts = full.split()
                if len(parts) >= 2:
                    first = first or " ".join(parts[:-1])
                    last = last or parts[-1]
        if not (first and last):
            continue

        k1 = _norm_key_first_last(first, last)
        k2 = _norm_key_initial_last(first, last)
        idx_key.setdefault(k1, []).append(d)
        idx_initial.setdefault(k2, []).append(d)

        dob = d.get("dob")
        if isinstance(dob, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", dob):
            idx_last_dob.setdefault(f"{last}|{dob}", []).append(d)

    return idx_key, idx_initial, idx_last_dob


# ---------------------------
# Comparison & report
# ---------------------------

def compare(pdf_recs: List[Dict], db_idx_key, db_idx_initial, db_idx_lastdob):
    matches: List[Dict] = []
    missing_on_db: List[Dict] = []

    for r in pdf_recs:
        hit = None
        # 1) FIRST LAST exact
        lst = db_idx_key.get(r["key"])
        if lst:
            hit = {"via": "first_last", "db": lst[0], "pdf": r}
        # 2) F.LAST fallback
        if not hit:
            lst = db_idx_initial.get(r["key_initial"])
            if lst:
                hit = {"via": "initial_last", "db": lst[0], "pdf": r}
        # 3) LAST + DOB (if DOB present)
        if not hit and r.get("dob"):
            lst = db_idx_lastdob.get(f"{r['last']}|{r['dob']}")
            if lst:
                hit = {"via": "last_dob", "db": lst[0], "pdf": r}

        if hit:
            matches.append(hit)
        else:
            missing_on_db.append(r)

    return matches, missing_on_db


def compute_extra_in_db(pdf_recs: List[Dict], db_idx_key) -> List[Dict]:
    pdf_keys = {r["key"] for r in pdf_recs}
    extras: List[Dict] = []
    for key, docs in db_idx_key.items():
        if key not in pdf_keys:
            for d in docs:
                extras.append(d)
    return extras


def write_csv(out_path: Path, matches, missing_on_db, extra_in_db):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["type", "pdf_name", "pdf_dob", "db_full_name", "db_dob", "db_booking_number", "match_via"])
        for m in matches:
            pdfn = f"{m['pdf']['first']} {m['pdf']['last']}"
            w.writerow([
                "match",
                pdfn,
                m["pdf"].get("dob") or "",
                (m["db"].get("full_name") or "").upper(),
                m["db"].get("dob") or "",
                m["db"].get("booking_number") or "",
                m["via"],
            ])
        for r in missing_on_db:
            pdfn = f"{r['first']} {r['last']}"
            w.writerow(["missing_on_db", pdfn, r.get("dob") or "", "", "", "", ""])
        for d in extra_in_db:
            w.writerow([
                "extra_in_db",
                "",
                "",
                (d.get("full_name") or "").upper(),
                d.get("dob") or "",
                d.get("booking_number") or "",
                "",
            ])


# ---------------------------
# CLI
# ---------------------------

def main() -> int:
    ap = argparse.ArgumentParser("Compare county roster PDF against Atlas simple_* collection")
    ap.add_argument("--county", required=True, choices=["harris", "brazoria", "galveston", "fortbend", "jefferson"],
                    help="County name (determines simple_{county} target and parser)")
    ap.add_argument("pdf", help="Path to the official roster PDF to compare")
    args = ap.parse_args()

    print(f"[compare] starting | county={args.county} | pdf_arg={args.pdf}")

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        # Try common repo-relative locations
        candidates = [
            Path("rosters") / f"{args.county}-county" / args.pdf,
            Path("rosters") / args.county / args.pdf,
            Path("rosters") / args.pdf,
        ]
        for alt_path in candidates:
            if alt_path.exists():
                pdf_path = alt_path
                break
        else:
            print(f"[compare] PDF not found. Tried: {Path(args.pdf)} and "
                  f"{', '.join(str(c) for c in candidates)}", file=sys.stderr)
            return 2

    parser_fn = PARSERS.get(args.county)
    if not parser_fn:
        print(f"No parser configured for county: {args.county}", file=sys.stderr)
        return 2

    print(f"[compare] County={args.county} | PDF={pdf_path}")
    print("[compare] Parsing PDF roster…")
    pdf_recs = parser_fn(pdf_path)
    print(f"[compare] PDF names parsed: {len(pdf_recs)}")

    print(f"[compare] Building DB index from simple_{args.county}…")
    db = _db()
    idx_key, idx_initial, idx_lastdob = build_db_index(db, args.county)
    print(f"[compare] DB index keys: {len(idx_key)} primary keys")

    matches, missing_on_db = compare(pdf_recs, idx_key, idx_initial, idx_lastdob)
    extra_in_db = compute_extra_in_db(pdf_recs, idx_key)

    print("=== Roster Comparison ===")
    print(f"County:               {args.county}")
    print(f"PDF total:            {len(pdf_recs)}")
    print(f"Matches:              {len(matches)}")
    print(f"Missing on DB:        {len(missing_on_db)}")
    print(f"Extra in DB:          {len(extra_in_db)}")

    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_csv = Path("debug") / args.county / f"roster_compare_{ts}.csv"
    write_csv(out_csv, matches, missing_on_db, extra_in_db)
    print(f"[compare] CSV written → {out_csv}")

    # Print a few examples to console
    for m in matches[:10]:
        print(" match:", f"{m['pdf']['first']} {m['pdf']['last']}", "↔", (m["db"].get("full_name") or "").upper(), f"via={m['via']}")
    for r in missing_on_db[:10]:
        print(" miss :", f"{r['first']} {r['last']}", "dob=", r.get("dob"))
    for d in extra_in_db[:10]:
        print(" extra:", (d.get("full_name") or "").upper(), "dob=", d.get("dob"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
