"""
scripts/enrich_harris_dob.py

Iterate simple_harris documents missing dob, and attempt to enrich DOB from HCSO.
Priority order:
  1) SPN lookup (highest confidence)
  2) Name lookup (LAST, FIRST) and accept if page returns a DOB; if SPN present on page and
     we have an SPN, require it to match/contain. Otherwise accept as low confidence.

Writes back to simple_harris with provenance:
  dob, dob_source='hcso', dob_source_url, dob_confidence ('high'|'low'), dob_checked_at

Env:
  MONGO_URI / MONGO_DB               - required
  HCSO_SPN_URL_FMT / HCSO_NAME_URL_FMT - required for lookups
  HCSO_THROTTLE_SEC, HCSO_TIMEOUT_SEC  - optional tuning

Usage:
    python -m scripts.enrich_harris_dob --limit 250 --window 30d
    python -m scripts.enrich_harris_dob --all --prefix ADAMS --limit 100
    # Progress examples
    python -m scripts.enrich_harris_dob --limit 500 --window 60d --progress-every 25
    python -m scripts.enrich_harris_dob --limit 200 --window 24h --verbose
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import os
import time

from storage.mongo_client import get_db
from enrichment.harris_hcso_dob import best_effort_lookup


def _now_iso() -> str:
    # Use timezone-aware UTC timestamp
    return datetime.now(timezone.utc).isoformat()


def _window_to_buckets(window: Optional[str]) -> Optional[list[str]]:
    if not window:
        return None
    w = window.lower()
    m = {
        "24h": ["0_24h"],
        "48h": ["24_48h"],
        "72h": ["48_72h"],
        "7d":  ["0_24h", "24_48h", "48_72h", "3d_7d"],
        "30d": ["0_24h", "24_48h", "48_72h", "3d_7d", "7d_30d"],
        "60d": ["0_24h", "24_48h", "48_72h", "3d_7d", "7d_30d", "30d_60d"],
        "60d_plus": ["60d_plus"],
        "older": ["60d_plus"],
    }
    return m.get(w)


def run(limit: int, only_missing: bool, name_prefix: Optional[str], window: Optional[str], dry_run: bool,
        progress_every: int = 0, verbose: bool = False) -> Dict[str, Any]:
    # Guardrail: require at least one HCSO lookup URL to be configured
    url_spn = (os.getenv("HCSO_SPN_URL_FMT") or "").strip()
    url_name = (os.getenv("HCSO_NAME_URL_FMT") or "").strip()
    if not url_spn and not url_name:
        print("[WARN] HCSO_SPN_URL_FMT and HCSO_NAME_URL_FMT are not set. Configure .env before running.")
        return {"tried": 0, "updated": 0, "skipped": 0}

    db = get_db()
    col = db["simple_harris"]

    q: Dict[str, Any] = {"county": "harris"}
    if only_missing:
        q["$or"] = [{"dob": {"$exists": False}}, {"dob": None}, {"dob": ""}]
    if name_prefix:
        q["full_name"] = {"$regex": f"^{name_prefix.upper()},", "$options": "i"}
    if window:
        buckets = _window_to_buckets(window)
        if buckets:
            q["time_bucket_v2"] = {"$in": buckets}

    proj = {
        "_id": 1,
        "full_name": 1,
        "spn": 1,
        "dob": 1,
    }

    total_candidates = col.count_documents(q)
    if limit:
        total_candidates = min(total_candidates, limit)
    print(f"[START] window={window or 'all'} only_missing={only_missing} candidates={total_candidates}", flush=True)

    cur = col.find(q, proj).limit(limit)
    tried = 0
    updated = 0
    skipped = 0
    t0 = time.time()

    for doc in cur:
        tried += 1
        full_name = (doc.get("full_name") or "").strip()
        spn = (doc.get("spn") or "").strip()
        if not full_name and not spn:
            skipped += 1
            continue

        if verbose:
            print(f"[LOOKUP] _id={doc['_id']} name='{full_name}' spn='{spn}'", flush=True)
        res = best_effort_lookup(full_name, spn or None)
        if not res or not res.found:
            if verbose:
                print(f"[MISS] _id={doc['_id']} name='{full_name}' spn='{spn}'", flush=True)
            continue

        confidence = "high" if (spn and res.spn and spn in res.spn) or spn else "low"
        update = {
            "dob": res.dob,
            "dob_source": "hcso",
            "dob_source_url": res.source_url,
            "dob_confidence": confidence,
            "dob_checked_at": _now_iso(),
        }

        if dry_run:
            print(f"[DRY] would set _id={doc['_id']} dob={res.dob} conf={confidence} url={res.source_url}", flush=True)
        else:
            col.update_one({"_id": doc["_id"]}, {"$set": update})
            updated += 1

        # Progress heartbeat
        if progress_every and (tried % progress_every == 0):
            elapsed = max(1e-6, time.time() - t0)
            rate = tried / elapsed
            eta = None
            if limit:
                remaining = max(0, limit - tried)
                eta_sec = int(remaining / max(1e-6, rate)) if rate > 0 else 0
                mm, ss = divmod(eta_sec, 60)
                eta = f"{mm:02d}:{ss:02d}"
            print(f"[PROGRESS] tried={tried} updated={updated} skipped={skipped} rate={rate:.2f}/s" + (f" eta~{eta}" if eta else ""), flush=True)

        # Optional pause between people
        try:
            pause = float(os.getenv("HCSO_BETWEEN_PEOPLE_SEC", "0.5"))
        except Exception:
            pause = 0.5
        time.sleep(pause)

    return {"tried": tried, "updated": updated, "skipped": skipped}


def main():
    ap = argparse.ArgumentParser(description="Enrich simple_harris DOBs from HCSO")
    ap.add_argument("--limit", type=int, default=int(os.getenv("HCSO_LIMIT", "500")))
    ap.add_argument("--all", action="store_true", help="Include rows that already have dob")
    ap.add_argument("--prefix", type=str, default=os.getenv("HCSO_NAME_PREFIX", ""), help="Restrict by last-name prefix (e.g. ADAMS)")
    ap.add_argument("--window", type=str, default=os.getenv("HCSO_WINDOW", ""), help="Limit search by time window (24h,48h,72h,7d,30d,60d)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--progress-every", type=int, default=int(os.getenv("HCSO_PROGRESS_EVERY", "0")), help="Print progress every N records (0 to disable)")
    ap.add_argument("--verbose", action="store_true", help="Log per-record lookups and misses")
    args = ap.parse_args()

    out = run(
        limit=args.limit,
        only_missing=(not args.all),
        name_prefix=(args.prefix or None),
        window=(args.window or None),
        dry_run=args.dry_run,
        progress_every=args.progress_every,
        verbose=args.verbose,
    )
    print({"ok": True, **out})


if __name__ == "__main__":
    main()
