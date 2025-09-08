"""
Derive Jefferson roster prefixes from a PDF and print shell-ready env lines.

Usage:
  python -m scripts.derive_jefferson_prefixes rosters/jefferson-county/currentinmates_roster.pdf

Outputs to stdout:
  JEFF_LETTERS=AD,AL,AN,...
  JEFF_FIRST_LETTERS=AL,AN,BE,...

Options:
  --min-last  N   (default 2) number of letters to use for last-name prefixes
  --min-first N   (default 2) number of letters to use for first-name prefixes

Dependencies: pdfminer.six
  pip install pdfminer.six
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path
from typing import Set, Tuple

try:
    from pdfminer.high_level import extract_text
except Exception:  # pragma: no cover
    print("pdfminer.six not installed. Run: pip install pdfminer.six", file=sys.stderr)
    raise

NAME_LINE_RE = re.compile(r"^([A-Za-z' .-]+),\s+([A-Za-z][A-Za-z .-]*)")


def norm_letters(s: str) -> str:
    return re.sub(r"[^A-Za-z]", "", s).upper()


def derive_prefixes(pdf_path: Path, min_last: int, min_first: int) -> Tuple[Set[str], Set[str]]:
    text = extract_text(str(pdf_path)) or ""
    last_prefixes: Set[str] = set()
    first_prefixes: Set[str] = set()

    for raw in text.splitlines():
        line = raw.strip()
        m = NAME_LINE_RE.match(line)
        if not m:
            continue
        last = norm_letters(m.group(1))
        first = norm_letters(m.group(2))
        if last:
            last_prefixes.add(last[: max(1, min_last)])
        if first:
            first_prefixes.add(first[: max(1, min_first)])

    return last_prefixes, first_prefixes


def main() -> int:
    ap = argparse.ArgumentParser("Derive Jefferson roster prefixes from PDF")
    ap.add_argument("pdf", help="Path to roster PDF")
    ap.add_argument("--min-last", type=int, default=2, help="Letters for last-name prefixes (default: 2)")
    ap.add_argument("--min-first", type=int, default=2, help="Letters for first-name prefixes (default: 2)")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    last_pfx, first_pfx = derive_prefixes(pdf_path, args.min_last, args.min_first)

    if not last_pfx:
        print("No names detected in PDF; cannot derive prefixes.", file=sys.stderr)
        return 1

    last_list = ",".join(sorted(last_pfx))
    first_list = ",".join(sorted(first_pfx)) if first_pfx else ""

    print(f"JEFF_LETTERS={last_list}")
    print(f"JEFF_FIRST_LETTERS={first_list}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())