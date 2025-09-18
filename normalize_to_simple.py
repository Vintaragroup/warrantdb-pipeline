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

import yaml
from pymongo import MongoClient

# mapping engine (your existing modules)
from pipeline.mapping.apply import apply_mapping
from pipeline.mapping.transforms import get_path  # for debug only

import re
from datetime import datetime, timezone


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
    If time_bucket missing, compute from booking_date using our standard buckets.
    """
    if doc.get("time_bucket"):
        return False
    days = _days_since(doc.get("booking_date"))
    if days is None:
        return False
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
    doc["time_bucket"] = tb
    return True


def postprocess_simple_doc(doc: Dict[str, Any], county: str, debug: bool = False) -> None:
    """
    Safe, schema-wide cleanup that runs after mapping but before upsert.
    Does not mutate required keys other than normalizing anchor/bond/time_bucket and ensuring tags.
    """
    anchor_changed = _normalize_anchor_with_case_number(doc, county)
    bond_changed, label = _normalize_bond_fields(doc)
    bucket_changed = _ensure_time_bucket(doc)

    # Ensure tags array exists
    if "tags" not in doc or doc["tags"] is None:
        doc["tags"] = []

    if debug and os.environ.get("DEBUG_MAP"):
        print(f"[POST-CLEAN] anchor_changed={anchor_changed} bond_changed={bond_changed} bucket_changed={bucket_changed} label={label}")


# ------------------ Main ------------------

def run_for_county(client: MongoClient, dbname: str, county: str, batch_size: int, max_docs: Optional[int], debug: bool):
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

    if debug:
        print(f"=== {county.upper()} ===")
        print(f"Using raw={raw_coll_name} -> simple={simple_coll_name} | mapping={mapping_file}")

    upserted = 0
    skipped = 0
    total = 0
    modified = 0

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

            # Upsert by _upsert_key using $set so only changed fields are written
            res = simple_coll.update_one(
                {"_upsert_key": upsert_key},
                {"$set": simple_doc},
                upsert=True,
            )

            # Track results
            if res.upserted_id is not None:
                upserted += 1  # newly inserted
            else:
                if res.modified_count:
                    modified += 1
                    upserted += 1  # count modified as successful upsert for summary

        except Exception as e:
            skipped += 1
            if debug:
                # Try to surface why this doc failed.
                keys = list(raw.keys())
                print(f"[WARN] skip _id={raw.get('_id')}: {e}")
                print(f"        raw keys: {keys}")
            continue

    if debug:
        print(f"Done {county}: total={total} inserted_or_updated={upserted} modified={modified} skipped={skipped}")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Normalize raw county collections to simple_* using YAML mappings.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--county", type=str, help="Single county slug (e.g., jefferson)")
    g.add_argument("--all", action="store_true", help="Run for all counties (folders under mappings/)")

    p.add_argument("--batch-size", type=int, default=1000)
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

    client = MongoClient(mongo_uri)

    if args.county:
        run_for_county(client, mongo_db, args.county.strip().lower(), args.batch_size, args.max_docs, args.debug)
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
                run_for_county(client, mongo_db, county, args.batch_size, args.max_docs, args.debug)
            except Exception as e:
                print(f"[WARN] county '{county}' failed: {e}", file=sys.stderr)
                continue


if __name__ == "__main__":
    main()