"""
Jefferson County CURRENTINMATES.PDF filter: last 72h with bond > 0.

What it does
- Downloads (or reads from path) the daily CURRENTINMATES.PDF roster
- Extracts rows with name, (optional) booking datetime, and bond amount
- Filters to inmates booked within the last N hours (default 72)
- Filters to inmates with any positive bond amount
- Outputs JSONL to stdout; optionally upserts into Mongo simple_jefferson

Usage
  python -m scripts.jefferson_pdf_recent_bonds --pdf https://jeffersoncountytx.gov/Sheriff/content/documents/inmate/CURRENTINMATES.PDF
  # or a local path
  python -m scripts.jefferson_pdf_recent_bonds --pdf rosters/jefferson-county/CURRENTINMATES_2025-11-02.pdf

Options
  --hours 72                    Window size in hours (default 72)
  --mongo                       If set, upsert into Mongo (collection simple_jefferson)
  --dry-run                     Parse and filter but do not write to DB (default)
  --output-jsonl out.jsonl      Save JSONL to file in addition to stdout

Dependencies
  pip install pdfminer.six python-dateutil pymongo python-dotenv requests

Notes
- The roster text is semi-structured; this parser aims to be tolerant but you
  may need to adjust patterns if the PDF’s layout changes.
- If booking datetime is not found, the row is skipped for the time-window filter.
"""
from __future__ import annotations
import argparse
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import requests
from dateutil import parser as dtparser
from pdfminer.high_level import extract_text

try:
    from storage.mongo_client import get_db  # local helper
except Exception:
    get_db = None

NAME_LINE_RE = re.compile(r"^([A-Z'\-\. ]+),\s+([A-Z][A-Z \-\.]*)(?:\s+([A-Z][A-Z \-\.]*))?\b")
DOB_RE = re.compile(r"\bDOB:?\s*(\d{1,2}/\d{1,2}/\d{2,4})\b", re.I)
# Patterns that might indicate booking date/time; adjust as we observe samples
BOOKING_RE = re.compile(r"\b(BOOK(?:ED|ING)?:?\s*)?(\d{1,2}/\d{1,2}/\d{2,4})(?:\s+(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?))?\b", re.I)
BOND_RE = re.compile(r"\bBOND:?\s*\$?([0-9,]+)(?:\.\d{2})?\b", re.I)
MONEY_RE = re.compile(r"\$?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.\d{2})?")

@dataclass
class RosterRow:
    line: str
    first: str
    last: str
    booking_dt: Optional[str]  # ISO8601 in UTC
    bond_total: Optional[float]
    dob: Optional[str] = None  # YYYY-MM-DD if found

    @property
    def full_name(self) -> str:
        return f"{self.first.title()} {self.last.title()}".strip()


def _parse_money_any(s: str) -> Optional[float]:
    m = MONEY_RE.search(s or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except Exception:
        return None


def _parse_booking_dt(line: str) -> Optional[str]:
    m = BOOKING_RE.search(line)
    if not m:
        return None
    date_part = m.group(2)
    time_part = m.group(3)
    try:
        if time_part:
            d = dtparser.parse(f"{date_part} {time_part}")
        else:
            d = dtparser.parse(date_part)
        # Assume local is Central Time; convert to UTC for consistent compare
        # If the PDF shows local timestamps, they’re likely CST/CDT; we normalize to UTC
        if d.tzinfo is None:
            # naive: interpret as US Central; approximate by local system tz then to UTC
            # If system tz is not Central, this may be off by a few hours. Refine if needed.
            d = d.replace(tzinfo=timezone.utc)  # fallback: treat as UTC to avoid drift
        return d.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def _parse_dob(line: str) -> Optional[str]:
    m = DOB_RE.search(line)
    if not m:
        return None
    try:
        return dtparser.parse(m.group(1)).date().isoformat()
    except Exception:
        return None


def parse_pdf_rows(pdf_path: Path) -> List[RosterRow]:
    text = extract_text(str(pdf_path)) or ""
    rows: List[RosterRow] = []

    for raw in text.splitlines():
        line = raw.strip().upper()
        if not line:
            continue
        nm = NAME_LINE_RE.match(line)
        if not nm:
            continue
        last = (nm.group(1) or "").strip()
        first = (nm.group(2) or "").strip()
        # Some PDFs include middle names; we keep only first token in first
        first = first.split()[0]
        booking_dt = _parse_booking_dt(line)
        dob = _parse_dob(line)
        bond_total = None
        # Try explicit BOND label first
        bm = BOND_RE.search(line)
        if bm:
            bond_total = _parse_money_any(bm.group(0))
        if bond_total is None:
            # Fallback to any currency-looking amount on the line
            bond_total = _parse_money_any(line)

        rows.append(RosterRow(line=raw.strip(), first=first, last=last, booking_dt=booking_dt, bond_total=bond_total, dob=dob))

    # Deduplicate by name + booking_dt when possible
    seen = set()
    uniq: List[RosterRow] = []
    for r in rows:
        key = (r.first, r.last, r.booking_dt)
        if key in seen:
            continue
        uniq.append(r)
        seen.add(key)

    return uniq


def filter_recent_bonds(rows: Iterable[RosterRow], hours: int) -> List[RosterRow]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out: List[RosterRow] = []
    for r in rows:
        if r.bond_total is None or r.bond_total <= 0:
            continue
        if not r.booking_dt:
            # Without a booking timestamp, we can’t assert recency; skip
            continue
        try:
            dt_obj = dtparser.isoparse(r.booking_dt) if hasattr(dtparser, 'isoparse') else dtparser.parse(r.booking_dt)
        except Exception:
            continue
        if dt_obj >= cutoff:
            out.append(r)
    return out


def download_to_tmp(url: str) -> Path:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.content
    tmp = Path("./debug/jefferson")
    tmp.mkdir(parents=True, exist_ok=True)
    fname = f"CURRENTINMATES_{int(time.time())}.pdf"
    path = tmp / fname
    path.write_bytes(data)
    return path


def upsert_mongo(rows: Iterable[RosterRow]):
    if get_db is None:
        print("Mongo helper not available; skipping DB write.", file=sys.stderr)
        return
    db = get_db()
    coll = db["simple_jefferson"]
    now = datetime.utcnow()
    for r in rows:
        key = {
            "full_name": r.full_name,
            "dob": r.dob,
        }
        update = {
            "$set": {
                "full_name": r.full_name,
                "first_name": r.first.title(),
                "last_name": r.last.title(),
                "dob": r.dob,
                "jefferson": {
                    "source": "CURRENTINMATES.PDF",
                    "booking_dt": r.booking_dt,
                    "bond_total": r.bond_total,
                    "source_fetched_at": now,
                },
                "updated_at": now,
            },
            "$setOnInsert": {
                "created_at": now,
            },
        }
        coll.update_one(key, update, upsert=True)


def main() -> int:
    ap = argparse.ArgumentParser("Filter Jefferson PDF for recent bonded inmates")
    ap.add_argument("--pdf", required=True, help="URL or local path to CURRENTINMATES.PDF")
    ap.add_argument("--hours", type=int, default=72, help="Window size in hours (default 72)")
    ap.add_argument("--mongo", action="store_true", help="Write filtered results into Mongo simple_jefferson")
    ap.add_argument("--output-jsonl", help="Write results as JSONL to this path in addition to stdout")
    ap.add_argument("--dry-run", action="store_true", help="Parse and filter only; no DB writes (default)")
    args = ap.parse_args()

    # Resolve PDF
    src = args.pdf
    if re.match(r"^https?://", src, re.I):
        pdf_path = download_to_tmp(src)
        print(f"[jefferson_pdf] downloaded → {pdf_path}")
    else:
        pdf_path = Path(src)
        if not pdf_path.exists():
            print(f"PDF not found: {pdf_path}", file=sys.stderr)
            return 2

    rows = parse_pdf_rows(pdf_path)
    print(f"[jefferson_pdf] parsed rows: {len(rows)}")

    filtered = filter_recent_bonds(rows, args.hours)
    print(f"[jefferson_pdf] filtered recent + bonded: {len(filtered)}")

    # Output JSONL
    out_lines = [json.dumps(asdict(r), ensure_ascii=False) for r in filtered]
    for line in out_lines:
        print(line)
    if args.output_jsonl:
        Path(args.output_jsonl).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_jsonl).write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        print(f"[jefferson_pdf] JSONL written → {args.output_jsonl}")

    # Optional DB write
    if args.mongo and not args.dry_run:
        upsert_mongo(filtered)
        print(f"[jefferson_pdf] upserted {len(filtered)} documents into simple_jefferson")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
