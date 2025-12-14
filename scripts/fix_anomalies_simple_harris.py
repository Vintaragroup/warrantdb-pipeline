"""
Safe fixer for anomalies in simple_harris.

Modes:
- mark (default): For invalid SPNs, set spn_flagged=true and stash original in spn_bad; do not change spn.
- unset_spn: Same as mark, plus unset the spn field.
- clean_address: For addresses starting with directive phrases (e.g., "REFER TO MAGISTRATE "), strip the leading phrase.
  If result becomes empty, unset address.

Dry-run by default: prints counts and samples; use --apply to execute updates.

Usage examples:
  python -m scripts.fix_anomalies_simple_harris --window 24h --limit 50000
  python -m scripts.fix_anomalies_simple_harris --window 30d --fix unset_spn --apply --limit 10000
  python -m scripts.fix_anomalies_simple_harris --window 7d --fix clean_address --apply
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, UTC
from typing import Any, Dict, List, Tuple

from pymongo import UpdateOne

from storage.mongo_client import get_db

DIRECTIVE_PREFIXES = [
    "REFER TO MAGISTRATE",
    "SEE MAGISTRATE",
    "SEE JUDGE",
    "NO BOND",
    "BOND DENIED",
    "DENIED",
    "CONTACT COURT",
]

SPN_DIGITS_MIN = 5
SPN_DIGITS_MAX = 10


def is_digits_only(s: str) -> bool:
    return bool(re.fullmatch(r"\d+", s or ""))


def invalid_spn(spn: str) -> Tuple[bool, str]:
    s = (spn or "").strip()
    if not s:
        return False, "empty"
    if not is_digits_only(s):
        return True, "non_digits"
    if len(s) < SPN_DIGITS_MIN or len(s) > SPN_DIGITS_MAX:
        return True, "bad_length"
    return False, "ok"


def strip_directive_prefix(line1: str) -> str:
    s = (line1 or "").strip()
    up = s.upper()
    for p in DIRECTIVE_PREFIXES:
        if up.startswith(p):
            # remove prefix and any adjacent punctuation/whitespace
            s = s[len(p):].lstrip(" :;-.,")
            break
    # collapse repeated spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description="Safe fixer for anomalies in simple_harris")
    ap.add_argument("--window", choices=["24h","48h","72h","7d","30d","60d"], default=None)
    ap.add_argument("--limit", type=int, default=100000)
    ap.add_argument("--fix", choices=["mark","unset_spn","clean_address"], default="mark")
    ap.add_argument("--apply", action="store_true", help="Apply updates (otherwise dry-run)")
    ap.add_argument("--samples", type=int, default=10)
    args = ap.parse_args()

    db = get_db()
    coll = db["simple_harris"]

    window_map = {
        "24h": ["0_24h"],
        "48h": ["24_48h"],
        "72h": ["48_72h"],
        "7d": ["0_24h","24_48h","48_72h","3d_7d"],
        "30d": ["0_24h","24_48h","48_72h","3d_7d","7d_30d"],
        "60d": ["0_24h","24_48h","48_72h","3d_7d","7d_30d","30d_60d"],
    }

    q: Dict[str, Any] = {"county": "harris"}
    if args.window:
        q["time_bucket_v2"] = {"$in": window_map[args.window]}

    proj = {
        "_id": 1,
        "full_name": 1,
        "spn": 1,
        "bond_label": 1,
        "address": 1,
    }

    cur = coll.find(q, proj).limit(args.limit)

    updates: List[UpdateOne] = []
    samples: List[Dict[str, Any]] = []

    c_spn_mark = 0
    c_spn_unset = 0
    c_addr_clean = 0

    now = datetime.now(UTC)

    for d in cur:
        _id = d["_id"]
        spn = (d.get("spn") or "").strip()
        addr = d.get("address") or {}
        line1 = (addr.get("line1") or "").strip()

        invalid, reason = invalid_spn(spn)
        op_set: Dict[str, Any] = {}
        op_unset: Dict[str, Any] = {}
        touched = False

        if args.fix in ("mark", "unset_spn") and invalid:
            # mark fields; preserve original
            op_set.update({
                "spn_flagged": True,
                "spn_bad": spn,
                "spn_flag_reason": reason,
                "anomalies": list(set((d.get("anomalies") or []) + ["spn_invalid"])),
                "updated_at": now,
            })
            c_spn_mark += 1
            if args.fix == "unset_spn":
                op_unset["spn"] = ""
                c_spn_unset += 1
            touched = True

        if args.fix == "clean_address" and isinstance(addr, dict) and line1:
            cleaned = strip_directive_prefix(line1)
            if cleaned != line1:
                # If cleaned becomes empty, remove address entirely; else write back line1 only
                if cleaned:
                    op_set["address.line1"] = cleaned
                else:
                    op_unset["address"] = ""
                op_set["updated_at"] = now
                anomalies = set(d.get("anomalies") or [])
                anomalies.add("address_cleaned")
                op_set["anomalies"] = list(anomalies)
                c_addr_clean += 1
                touched = True

        if touched:
            update_doc: Dict[str, Any] = {}
            if op_set:
                update_doc["$set"] = op_set
            if op_unset:
                update_doc["$unset"] = op_unset
            updates.append(UpdateOne({"_id": _id}, update_doc))
            if len(samples) < args.samples:
                samples.append({
                    "_id": str(_id),
                    "full_name": d.get("full_name"),
                    "spn_before": spn,
                    "line1_before": line1,
                    "update": update_doc,
                })

    print(json.dumps({
        "window": args.window,
        "fix": args.fix,
        "apply": bool(args.apply),
        "counts": {
            "spn_mark": c_spn_mark,
            "spn_unset": c_spn_unset,
            "address_clean": c_addr_clean,
            "bulk_ops": len(updates),
        },
        "samples": samples,
    }, default=str, indent=2))

    if args.apply and updates:
        res = coll.bulk_write(updates, ordered=False)
        print("apply_result:", res.bulk_api_result)

    return 0


if __name__ == "__main__":
    sys.exit(main())
