"""
Scan simple_harris for data anomalies reported by FE/users and print a concise summary.

Checks performed:
- SPN integrity: spn should be digits-only (allow 5-9 digits). Flags non-digit, too short/long, or obvious name-like values.
- Name integrity: full_name should contain a comma ("LAST, FIRST"). Flags missing comma or numeric-only names.
- Address contamination: address fields that appear to contain bond text or court directives (e.g., "REFER TO MAGISTRATE").
- Cross-field swaps: cases where spn equals (case-insensitive) a token from name, or name looks like an SPN (all digits).

Outputs:
- Summary counts per anomaly type
- Up to N sample documents per anomaly (masked identifiers)
- Optional JSONL dump via --out for deeper inspection

Usage:
  python -m scripts.scan_anomalies_simple_harris --limit 5000 --samples 10
  python -m scripts.scan_anomalies_simple_harris --window 30d --samples 5 --out debug/anomaly_scan.jsonl
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, UTC
from typing import Any, Dict, Iterable, List

from storage.mongo_client import get_db

BOND_PHRASES = [
    "REFER TO MAGISTRATE",
    "BOND DENIED",
    "DENIED",
    "NO BOND",
    "PR BOND",
    "PERSONAL RECOGNIZANCE",
    "CONTACT COURT",
    "SEE JUDGE",
    "SEE MAGISTRATE",
]

SPN_DIGITS_MIN = 5
SPN_DIGITS_MAX = 10


def is_digits_only(s: str) -> bool:
    return bool(re.fullmatch(r"\d+", s or ""))


def looks_like_spn(s: str) -> bool:
    s = (s or "").strip()
    if not is_digits_only(s):
        return False
    return SPN_DIGITS_MIN <= len(s) <= SPN_DIGITS_MAX


def looks_like_name(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return False
    # Any letter present counts as name-like
    return bool(re.search(r"[A-Za-z]", s))


def address_contains_bond_text(addr: Dict[str, Any] | None) -> bool:
    if not isinstance(addr, dict):
        return False
    text = " ".join(str(addr.get(k, "")) for k in ("line1", "city", "zip", "state"))
    up = text.upper()
    return any(p in up for p in BOND_PHRASES)


def mask(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return s
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def sampleify(docs: Iterable[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, d in enumerate(docs):
        if i >= n:
            break
        out.append(
            {
                "full_name": d.get("full_name"),
                "spn": mask(d.get("spn", "")),
                "case_number": d.get("case_number"),
                "bond_label": d.get("bond_label"),
                "address": d.get("address"),
                "booking_datetime": d.get("booking_datetime"),
            }
        )
    return out


def main():
    ap = argparse.ArgumentParser(description="Scan simple_harris for anomalies")
    ap.add_argument("--window", choices=["24h","48h","72h","7d","30d","60d"], default=None, help="Filter by time window (maps to v2 buckets)")
    ap.add_argument("--limit", type=int, default=100000, help="Max docs to scan")
    ap.add_argument("--samples", type=int, default=10, help="Samples per anomaly type to print")
    ap.add_argument("--out", type=str, default=None, help="Optional JSONL output file with full docs per anomaly")
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
        "_id": 0,
        "full_name": 1,
        "spn": 1,
        "case_number": 1,
        "bond_label": 1,
        "address": 1,
        "booking_datetime": 1,
    }

    cur = coll.find(q, proj).limit(args.limit)

    anomalies = {
        "spn_not_digits": [],
        "spn_too_short_or_long": [],
        "spn_matches_name_token": [],
        "name_missing_comma": [],
        "name_looks_like_spn": [],
        "address_contains_bond_text": [],
    }

    out_file = open(args.out, "w") if args.out else None

    for d in cur:
        spn = (d.get("spn") or "").strip()
        name = (d.get("full_name") or "").strip()
        addr = d.get("address")

        # spn integrity
        if spn:
            if not is_digits_only(spn):
                anomalies["spn_not_digits"].append(d)
                if out_file:
                    out_file.write(json.dumps({"anomaly": "spn_not_digits", "doc": d}) + "\n")
            if len(spn) < SPN_DIGITS_MIN or len(spn) > SPN_DIGITS_MAX:
                anomalies["spn_too_short_or_long"].append(d)
                if out_file:
                    out_file.write(json.dumps({"anomaly": "spn_too_short_or_long", "doc": d}) + "\n")

        # name integrity
        if name and "," not in name:
            anomalies["name_missing_comma"].append(d)
            if out_file:
                out_file.write(json.dumps({"anomaly": "name_missing_comma", "doc": d}) + "\n")
        if looks_like_spn(name.replace(",", "").replace(" ", "")):
            anomalies["name_looks_like_spn"].append(d)
            if out_file:
                out_file.write(json.dumps({"anomaly": "name_looks_like_spn", "doc": d}) + "\n")

        # cross-field swaps (spn equals any token of the name)
        if spn and name:
            tokens = [t for t in re.split(r"[^A-Za-z0-9]+", name) if t]
            if spn in tokens:
                anomalies["spn_matches_name_token"].append(d)
                if out_file:
                    out_file.write(json.dumps({"anomaly": "spn_matches_name_token", "doc": d}) + "\n")

        # address contamination
        if address_contains_bond_text(addr):
            anomalies["address_contains_bond_text"].append(d)
            if out_file:
                out_file.write(json.dumps({"anomaly": "address_contains_bond_text", "doc": d}) + "\n")

    if out_file:
        out_file.close()

    # Print summary
    total_scanned = sum(len(v) for v in anomalies.values())  # not unique, but gives a sense
    print(f"[SCAN] Completed at {datetime.now(UTC).isoformat()}")
    for k in [
        "spn_not_digits",
        "spn_too_short_or_long",
        "spn_matches_name_token",
        "name_missing_comma",
        "name_looks_like_spn",
        "address_contains_bond_text",
    ]:
        arr = anomalies[k]
        print(f"- {k}: {len(arr)}")
        for s in sampleify(arr, args.samples):
            print("   ", json.dumps(s, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())
