#!/usr/bin/env python3
"""
normalize_to_simple.py

Reads a county mapping YAML, iterates raw collection in batches,
applies the mapping engine, and upserts into simple_<county>.

ENV:
  MONGO_URI   - required
  MONGO_DB    - required
  DEBUG_MAP=1 - enable per-doc mapping debug lines (bond fields, etc.)
"""

from __future__ import annotations
import os
import sys
import argparse
from typing import Any, Dict, Iterable, Optional, Tuple
import logging
import time

import yaml
from pymongo import MongoClient
from pymongo import UpdateOne, WriteConcern
from dotenv import load_dotenv

# mapping engine (your existing modules)
from pipeline.mapping.apply import apply_mapping
from pipeline.mapping.transforms import get_path  # for debug only

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Load environment from .env so MONGO_URI/MONGO_DB are available when not exported
load_dotenv()


# ------------------ Utils ------------------

def iter_raw(coll, batch_size: int = 1000, max_docs: Optional[int] = None) -> Iterable[Dict[str, Any]]:
    """
    Iterate a collection in ascending _id order using short-lived cursors.
    Avoid noCursorTimeout by reopening a fresh cursor per batch.
    """
    last_id = None
    seen = 0
    while True:
        query = {"_id": {"$gt": last_id}} if last_id is not None else {}
        cursor = coll.find(query, sort=[("_id", 1)], limit=batch_size)
        got = 0
        for doc in cursor:
            last_id = doc.get("_id", last_id)
            got += 1
            seen += 1
            yield doc
            if max_docs is not None and seen >= max_docs:
                return
        if got < batch_size:
            break


def load_mapping(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def mapping_path_for_county(county: str) -> str:
    """
    Conventional path used in this repo:
      mappings/<county>/<county>_events.yaml
    """
    guess = os.path.join("mappings", county, f"{county}_events.yaml")
    if os.path.exists(guess):
        return guess
    # fall back to first .yaml in that folder
    folder = os.path.join("mappings", county)
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            if name.endswith(".yaml") or name.endswith(".yml"):
                return os.path.join(folder, name)
    raise FileNotFoundError(f"No mapping file found for county '{county}' under 'mappings/{county}/'.")


def ensure_simple_collection_name(map_cfg: Dict[str, Any]) -> str:
    c = (map_cfg.get("county") or "").strip().lower()
    return f"simple_{c}" if c else "simple_unknown"


def build_upsert_key_or_none(
    normalized: Dict[str, Any],
    primary_key_fields: Iterable[str],
    raw: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Build _upsert_key dict from normalized fields.
    If 'anchor' is missing/empty, synthesize "full_name||booking_date" as last resort.
    Returns dict or None if still missing required fields.
    """
    pkeys = list(primary_key_fields or [])
    upsert_key: Dict[str, Any] = {}
    missing = []

    for f in pkeys:
        v = normalized.get(f, None)
        if v in (None, ""):
            missing.append(f)
        else:
            upsert_key[f] = v

    # Last-resort anchor fallback if needed
    if "anchor" in missing:
        nm = (normalized.get("full_name") or "").strip()
        bdt = (normalized.get("booking_date") or "").strip()
        if nm and bdt:
            upsert_key["anchor"] = f"{nm}||{bdt}"
            missing = [m for m in missing if m != "anchor"]

    if missing:
        if os.environ.get("DEBUG_MAP"):
            print(f"[WARN] skip _id={raw.get('_id')}: missing primary_key fields {missing}")
            print("        normalized.anchor:", normalized.get("anchor"))
            print("        normalized.full_name:", normalized.get("full_name"))
            print("        normalized.booking_date:", normalized.get("booking_date"))
        return None

    return upsert_key


def debug_map_line(raw: Dict[str, Any], norm: Dict[str, Any]):
    if not os.environ.get("DEBUG_MAP"):
        return
    rid = raw.get("_id")
    tb = get_path(raw, "total_bond")
    ch0 = get_path(raw, "charges[0].bond")
    ch1 = get_path(raw, "charges[1].bond")
    nbond = norm.get("bond")
    nbam = norm.get("bond_amount")
    nbl = norm.get("bond_label")
    print(
        f"[MAP-DBG] raw._id= {rid} src.total_bond= {tb} src.ch0_bond= {ch0} src.ch1_bond= {ch1} "
        f"norm.bond= {nbond} norm.bond_amount= {nbam} norm.bond_label= {nbl}"
    )

    # Extra field visibility to help verify new mappings
    case_no = norm.get("case_number")
    docket = norm.get("docket_number") or norm.get("case_number")
    charge = norm.get("charge") or norm.get("offense")
    status = norm.get("status")
    race = norm.get("race")
    age = norm.get("age")
    sex = norm.get("sex")
    bucket = norm.get("time_bucket") or norm.get("booking_age_category")
    tags = norm.get("tags")
    print(
        f"[MAP-DBG2] case_no={case_no} docket={docket} charge={charge} status={status} "
        f"race={race} age={age} sex={sex} bucket={bucket} tags={tags}"
    )


def _days_since(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        # Accept 'YYYY-MM-DD' or ISO8601; fall back to naive parse
        if len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-":
            dt = datetime(int(date_str[0:4]), int(date_str[5:7]), int(date_str[8:10]), tzinfo=timezone.utc)
        else:
            # Let fromisoformat try; add Z -> +00:00 if present
            s = date_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - dt).days
    except Exception:
        return None


def _normalize_anchor_with_case_number(doc: Dict[str, Any], county: str) -> bool:
    """
    For counties like Harris, prefer purely numeric case_number as anchor when available.
    Returns True if anchor was changed.
    """
    upk = doc.get("_upsert_key") or {}
    anchor = upk.get("anchor")
    case_no = doc.get("case_number")
    changed = False

    if not anchor and case_no:
        m = re.match(r"^(\d+)", str(case_no))
        if m:
            upk["anchor"] = m.group(1)
            doc["_upsert_key"] = upk
            return True

    # If anchor exists but has non-digits, and case_number starts with digits, normalize.
    if anchor and case_no:
        m = re.match(r"^(\d+)", str(case_no))
        if m and not re.fullmatch(r"\d+", str(anchor)):
            upk["anchor"] = m.group(1)
            doc["_upsert_key"] = upk
            changed = True

    return changed


def _normalize_bond_fields(doc: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Ensure bond, bond_amount, bond_label are consistent:
      - If bond is numeric and bond_amount missing => set bond_amount
      - If bond is a string like 'REFER TO MAGISTRATE' or 'SUMMONS ISSUED' => make it the label, clear bond_amount if unset
    Returns (changed, label_detected)
    """
    changed = False
    label_detected: Optional[str] = None

    bond = doc.get("bond")
    bond_amount = doc.get("bond_amount")
    bond_label = doc.get("bond_label")

    # Case 1: numeric bond -> ensure bond_amount
    if isinstance(bond, (int, float)) and bond_amount is None:
        doc["bond_amount"] = float(bond)
        changed = True

    # Case 2: textual bond -> treat as label if label not already set
    if isinstance(bond, str):
        s = bond.strip().upper()
        if s in {"REFER TO MAGISTRATE", "SUMMONS ISSUED", "J", "PR BOND", "PR-BOND"}:
            label_detected = bond if bond_label in (None, "") else None
            if bond_label in (None, ""):
                doc["bond_label"] = bond
                changed = True
            # If amount not set, keep it None (do not overwrite existing numeric)
            if "bond_amount" not in doc:
                doc["bond_amount"] = None
                changed = True

    # Ensure bond_label exists as empty string if still missing (for schema parity)
    if "bond_label" not in doc:
        doc["bond_label"] = ""
        changed = True

    return changed, label_detected


def _ensure_time_bucket(doc: Dict[str, Any]) -> bool:
    """
    Absolute rule: compute time_bucket strictly from booking_date.
    Ignore any existing value; do NOT fall back to ingestion time.
    If booking_date is missing, remove time_bucket to avoid stale tags.
    Returns True iff the document was modified.
    """
    changed = False
    days = _days_since(doc.get("booking_date"))
    if days is None:
        if "time_bucket" in doc:
            del doc["time_bucket"]
            changed = True
        return changed
    if days <= 1:
        tb = "24_hours_or_less"
    elif days <= 2:
        tb = "48_hours"
    elif days <= 3:
        tb = "72_hours"
    elif days <= 30:
        tb = "0_to_30_days"
    elif days <= 60:
        tb = "31_to_60_days"
    elif days <= 180:
        tb = "61_to_180_days"
    else:
        tb = "365_days_or_older"
    if doc.get("time_bucket") != tb:
        doc["time_bucket"] = tb
        changed = True
    return changed


def postprocess_simple_doc(doc: Dict[str, Any], county: str, debug: bool = False) -> None:
    """
    Safe, schema-wide cleanup that runs after mapping but before upsert.
    Does not mutate required keys other than normalizing anchor/bond/time_bucket and ensuring tags.
    """
    anchor_changed = _normalize_anchor_with_case_number(doc, county)
    bond_changed, label = _normalize_bond_fields(doc)
    bucket_changed = _ensure_time_bucket(doc)

    # Compute ingest lag for observability (gap from booking_date to when we normalized)
    # Does not affect time_bucket tags.
    try:
        bdt = doc.get("booking_date")
        nat = doc.get("normalized_at")
        if bdt and nat:
            # parse to dates
            # booking_date: YYYY-MM-DD
            by, bm, bd = [int(x) for x in bdt.split("-")]
            from datetime import datetime, timezone
            # normalized_at may have +00:00; use fromisoformat
            nstr = nat
            if nstr.endswith('Z') and '+' not in nstr:
                nstr = nstr[:-1] + '+00:00'
            ndt = datetime.fromisoformat(nstr)
            if ndt.tzinfo is None:
                ndt = ndt.replace(tzinfo=timezone.utc)
            days_delta = (ndt.date() - datetime(by, bm, bd).date()).days
            hours_delta = days_delta * 24
            # store as simple numbers for dashboards
            doc["ingest_lag_days"] = max(days_delta, 0)
            doc["ingest_lag_hours"] = max(hours_delta, 0)
    except Exception:
        # best effort; ignore if parsing fails
        pass

    # Ensure tags array exists
    if "tags" not in doc or doc["tags"] is None:
        doc["tags"] = []

    if debug and os.environ.get("DEBUG_MAP"):
        print(f"[POST-CLEAN] anchor_changed={anchor_changed} bond_changed={bond_changed} bucket_changed={bucket_changed} label={label}")

    # Always derive booking_datetime/booking_date_v2 and compute time_bucket_v2
    # using America/Chicago semantics.
    _maybe_derive_booking_datetime(doc, debug)
    _maybe_compute_time_bucket_v2(doc, debug)


# ------------------ New Derivation Helpers (feature-flagged) ------------------

def _parse_dt_any(val: str) -> Optional[datetime]:
    if not val or not isinstance(val, str):
        return None
    s = val.strip()
    if not s:
        return None
    try:
        # Normalize 'Z' to +00:00 for fromisoformat
        if s.endswith('Z') and '+' not in s:
            s = s[:-1] + '+00:00'
        dtv = datetime.fromisoformat(s)
        # Interpret naive datetimes as America/Chicago
        if dtv.tzinfo is None:
            dtv = dtv.replace(tzinfo=ZoneInfo("America/Chicago"))
        # Return UTC for storage/computation
        return dtv.astimezone(timezone.utc)
    except Exception:
        # Try YYYY-MM-DD fallback (interpret as midnight America/Chicago)
        if len(s) == 10 and s[4] == '-' and s[7] == '-':
            try:
                central = ZoneInfo("America/Chicago")
                local_dt = datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]), tzinfo=central)
                return local_dt.astimezone(timezone.utc)
            except Exception:
                return None
    return None


def _maybe_derive_booking_datetime(doc: Dict[str, Any], debug: bool = False) -> None:
    """Populate booking_datetime / booking_date_v2 derivation fields if missing.
    Precedence (first non-null parsable):
      first_seen_at -> updated_at -> booking_date (legacy day only)
    This does NOT overwrite existing booking_date. Adds:
      booking_datetime (ISO8601 Z)
      booking_date_v2 (YYYY-MM-DD)
      booking_derivation_source
    """
    # Do not overwrite an existing booking_datetime
    if doc.get("booking_datetime"):
        return

    sources = [
        ("first_seen_at", doc.get("first_seen_at")),
        ("updated_at", doc.get("updated_at")),
        ("legacy_booking_date", doc.get("booking_date")),
    ]
    chosen_src = None
    chosen_dt: Optional[datetime] = None
    for name, raw in sources:
        dtv = _parse_dt_any(raw) if raw else None
        if dtv:
            # If the source was a legacy date (day only), dtv time = 00:00:00Z
            chosen_src = name
            chosen_dt = dtv
            break

    if not chosen_dt:
        if debug:
            print("[DERIVE] No booking_datetime derivation source found")
        return

    # Guard against future anomaly (>12h ahead of now)
    now = datetime.now(timezone.utc)
    if chosen_dt - now > timedelta(hours=12):  # type: ignore
        # Tag anomaly but still record (could indicate clock skew)
        tags = doc.get("tags") or []
        if "future_date_candidate" not in tags:
            tags.append("future_date_candidate")
        doc["tags"] = tags

    # Store booking_datetime in UTC ISO8601 Z form
    doc["booking_datetime"] = chosen_dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    doc["booking_derivation_source"] = chosen_src
    # booking_date_v2 is the date in America/Chicago
    doc["booking_date_v2"] = chosen_dt.astimezone(ZoneInfo("America/Chicago")).date().isoformat()
    if debug:
        print(f"[DERIVE] booking_datetime set from {chosen_src}: {doc['booking_datetime']}")


from datetime import timedelta  # placed after helper definitions above for clarity


def _maybe_compute_time_bucket_v2(doc: Dict[str, Any], debug: bool = False) -> None:
    """Compute time_bucket_v2 using booking_datetime if available.
    Bucket design (collapsed after 60d):
        <24h          -> 0_24h
        24-48h        -> 24_48h
        48-72h        -> 48_72h
        72h-7d        -> 3d_7d
        7d-30d        -> 7d_30d
        30d-60d       -> 30d_60d
        60d+          -> 60d_plus
    """
    # Always compute from booking_datetime when available
    bdt = doc.get("booking_datetime")
    if not bdt:
        return
    dtv = _parse_dt_any(bdt)
    if not dtv:
        return
    now = datetime.now(timezone.utc)
    delta = now - dtv
    hours = delta.total_seconds() / 3600.0
    days = delta.total_seconds() / 86400.0
    if hours < 24:
        bucket = "0_24h"
    elif hours < 48:
        bucket = "24_48h"
    elif hours < 72:
        bucket = "48_72h"
    elif hours < 24*7:
        bucket = "3d_7d"
    elif days < 30:
        bucket = "7d_30d"
    elif days < 60:
        bucket = "30d_60d"
    else:
        bucket = "60d_plus"
    doc["time_bucket_v2"] = bucket
    if debug:
        print(f"[DERIVE] time_bucket_v2={bucket} (hours={hours:.1f})")


# ------------------ Main ------------------

def run_for_county(client: MongoClient, dbname: str, county: str, batch_size: int, max_docs: Optional[int], debug: bool, bulk_size: int = 500, progress_every: int = 1000):
    mapping_file = mapping_path_for_county(county)
    mapping = load_mapping(mapping_file)

    mongo_db = client[dbname]

    # raw collection from mapping (prefer 'collection', fallback 'source')
    raw_coll_name = mapping.get("collection") or mapping.get("source")
    if not raw_coll_name:
        raise ValueError("Mapping must include 'collection' or 'source' to name the raw collection.")
    raw_coll = mongo_db[raw_coll_name]

    simple_coll_name = ensure_simple_collection_name(mapping)
    simple_coll = mongo_db[simple_coll_name]

    logging.info(
        "normalize start | county=%s raw=%s simple=%s mapping=%s batch_size=%s bulk_size=%s progress_every=%s",
        county, raw_coll_name, simple_coll_name, mapping_file, batch_size, bulk_size, progress_every,
    )

    upserted = 0
    skipped = 0
    total = 0
    modified = 0
    pending_ops: list[UpdateOne] = []
    started_at = time.time()

    for raw in iter_raw(raw_coll, batch_size=batch_size, max_docs=max_docs):
        total += 1
        try:
            normalized = apply_mapping(raw, mapping)

            # Build _upsert_key robustly
            upsert_key = build_upsert_key_or_none(normalized, mapping.get("primary_key", []), raw)
            if not upsert_key:
                skipped += 1
                continue

            # Attach upsert key
            normalized["_upsert_key"] = upsert_key

            # Cross-county safety net: normalize anchors, bond fields, time buckets, and defaults
            postprocess_simple_doc(normalized, county, debug)

            # Optional mapping debug line (bonds, etc.)
            debug_map_line(raw, normalized)

            if debug:
                print(f"[DBG] upserting _upsert_key={upsert_key}")

            # Drop None values to avoid cluttering the simple collection
            simple_doc = {k: v for k, v in normalized.items() if v is not None}

            # Upsert by _upsert_key using nested-field filter for better index compatibility
            # Build a filter like {"_upsert_key.county": ..., "_upsert_key.category": ..., "_upsert_key.anchor": ...}
            upsert_filter = {f"_upsert_key.{k}": v for k, v in upsert_key.items()}
            # Queue for bulk upsert (unordered for throughput)
            pending_ops.append(UpdateOne(upsert_filter, {"$set": simple_doc}, upsert=True))

            # Flush when we reach bulk_size to reduce round-trips
            if len(pending_ops) >= bulk_size:
                try:
                    res = simple_coll.bulk_write(pending_ops, ordered=False)
                    upserted += (res.upserted_count or 0) + (res.modified_count or 0)
                    modified += (res.modified_count or 0)
                finally:
                    pending_ops.clear()
                # Log progress on flush
                elapsed = max(time.time() - started_at, 1e-6)
                rate = total / elapsed
                logging.info(
                    "progress | processed=%s upserted_or_modified=%s skipped=%s rate=%.1f docs/s",
                    total, upserted, skipped, rate,
                )

            # Periodic progress log
            if progress_every and (total % progress_every == 0):
                elapsed = max(time.time() - started_at, 1e-6)
                rate = total / elapsed
                logging.info(
                    "progress | processed=%s upserted_or_modified=%s skipped=%s rate=%.1f docs/s",
                    total, upserted, skipped, rate,
                )

        except Exception as e:
            skipped += 1
            if debug:
                # Try to surface why this doc failed.
                keys = list(raw.keys())
                print(f"[WARN] skip _id={raw.get('_id')}: {e}")
                print(f"        raw keys: {keys}")
            continue

    # Flush any remaining ops
    if pending_ops:
        res = simple_coll.bulk_write(pending_ops, ordered=False)
        upserted += (res.upserted_count or 0) + (res.modified_count or 0)
        modified += (res.modified_count or 0)
        pending_ops.clear()
    elapsed = max(time.time() - started_at, 1e-6)
    rate = total / elapsed
    logging.info(
        "normalize done | county=%s total=%s upserted_or_modified=%s modified=%s skipped=%s rate=%.1f docs/s",
        county, total, upserted, modified, skipped, rate,
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Normalize raw county collections to simple_* using YAML mappings.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--county", type=str, help="Single county slug (e.g., jefferson)")
    g.add_argument("--all", action="store_true", help="Run for all counties (folders under mappings/)")

    p.add_argument("--batch-size", type=int, default=1000)
    p.add_argument("--bulk-size", type=int, default=500, help="Number of docs per unordered bulk write")
    p.add_argument("--progress-every", type=int, default=1000, help="Emit a progress log every N processed docs")
    p.add_argument("--log-file", type=str, default=None, help="Optional path to write a log file in addition to stdout")
    p.add_argument("--log-level", type=str, default="INFO", choices=["DEBUG","INFO","WARN","ERROR"], help="Log level")
    p.add_argument("--max-docs", type=int, default=None)
    p.add_argument("--debug", action="store_true")
    return p.parse_args(argv)


def main():
    args = parse_args()

    mongo_uri = os.environ.get("MONGO_URI")
    mongo_db = os.environ.get("MONGO_DB")
    if not mongo_uri or not mongo_db:
        print("ERROR: MONGO_URI and MONGO_DB env vars are required.", file=sys.stderr)
        sys.exit(2)

    # Configure logging
    level = getattr(logging, (getattr(args, "log_level", None) or "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")
    if getattr(args, "log_file", None):
        fh = logging.FileHandler(args.log_file)
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)

    # Add conservative timeouts to avoid indefinite stalls during network issues
    client = MongoClient(
        mongo_uri,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        socketTimeoutMS=30000,
        retryWrites=True,
        maxPoolSize=20,
    )

    if args.county:
        run_for_county(
            client,
            mongo_db,
            args.county.strip().lower(),
            args.batch_size,
            args.max_docs,
            args.debug,
            bulk_size=args.bulk_size,
            progress_every=getattr(args, "progress_every", 1000),
        )
    else:
        # --all: run for each subfolder under mappings/
        mappings_dir = "mappings"
        if not os.path.isdir(mappings_dir):
            print("ERROR: 'mappings/' directory not found.", file=sys.stderr)
            sys.exit(2)
        for county in sorted(os.listdir(mappings_dir)):
            path = os.path.join(mappings_dir, county)
            if not os.path.isdir(path):
                continue
            try:
                run_for_county(
                    client,
                    mongo_db,
                    county,
                    args.batch_size,
                    args.max_docs,
                    args.debug,
                    bulk_size=args.bulk_size,
                    progress_every=getattr(args, "progress_every", 1000),
                )
            except Exception as e:
                print(f"[WARN] county '{county}' failed: {e}", file=sys.stderr)
                continue


if __name__ == "__main__":
    main()