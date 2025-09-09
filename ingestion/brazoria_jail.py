# ingestion/brazoria_jail.py
import os
import re
import sys
import time
import datetime as dt
import json
from typing import List, Dict, Any, Optional, Callable
from pathlib import Path
import glob
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from ingestion.base_scraper import BaseScraper

BASE = os.getenv("BRAZORIA_BASE_URL", "https://pubweb.brazoriacountytx.gov/PublicAccess/")
SEARCH_PATH = "JailingSearch.aspx"  # expects ?ID=400 plus name params

# Debug dumps
DUMP_DIR = Path(os.getenv("BRAZORIA_DUMP_DIR", "debug_dumps/brazoria"))
DUMP_DIR.mkdir(parents=True, exist_ok=True)
MAX_DEBUG = int(os.getenv("BRAZORIA_MAX_DEBUG", "20"))


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _utcnow_iso() -> str:
    return _utcnow().isoformat()

# JSON datetime serializer for CLI/JSONL output
def _dt_default(o):
    if isinstance(o, dt.datetime):
        return o.replace(tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return str(o)

def _to_int_money(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    m = re.search(r'(\$?\s*[0-9][0-9,]*)', s.replace('\xa0', ' '))
    if not m:
        return None
    digits = re.sub(r'[^0-9]', '', m.group(1))
    return int(digits) if digits.isdigit() else None

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/127.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
        "Referer": urljoin(BASE, SEARCH_PATH) + "?ID=400",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s

def _dump_html(content: str, prefix: str, sub: str = "") -> str:
    """Write HTML debug snapshot and prune older ones to keep the directory small.

    We keep at most `MAX_DEBUG` files in the chosen directory (per subfolder),
    preferring to retain the most recent files. The prefix is included in the
    file name and used to help group similar snapshots together.
    """
    base = (DUMP_DIR / sub) if sub else DUMP_DIR
    base.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = base / f"{prefix}_{stamp}.html"
    path.write_text(content, encoding="utf-8")

    # Prune older snapshots: keep only the most recent MAX_DEBUG files in this subdir
    try:
        # Gather only .html files within the same subdir
        files = [Path(p) for p in glob.glob(str(base / "*.html"))]
        if len(files) > MAX_DEBUG:
            # Sort by modification time (newest first)
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[MAX_DEBUG:]:
                try:
                    old.unlink(missing_ok=True)
                except Exception:
                    # Best-effort cleanup; ignore errors
                    pass
    except Exception:
        # Never let pruning errors affect the main flow
        pass

    return str(path)

def _abs(base: str, href: Optional[str]) -> Optional[str]:
    return urljoin(base, href) if href else None

def _pick_results_table(soup: BeautifulSoup):
    """Find the results table that has Booking Number / Defendant Name headers."""
    for tbl in soup.select("table"):
        headers = [th.get_text(strip=True).lower() for th in tbl.select("th")]
        if not headers:
            continue
        if ("booking number" in headers) and ("defendant name" in headers):
            return tbl
    return None

def _parse_mmddyyyy(s: Optional[str]) -> Optional[dt.date]:
    if not s:
        return None
    s = s.strip()
    # common Tyler formats
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    # ISO (if we ever see it)
    try:
        return dt.date.fromisoformat(s)
    except Exception:
        return None

def _iso_or_none(d: Optional[dt.date]) -> Optional[str]:
    return d.isoformat() if d else None

def _calculate_booking_age_category(booked_date_str: str) -> str:
    """Calculate how long ago someone was booked and return a category."""
    if not booked_date_str:
        return "unknown"
    
    try:
        booked_date = dt.datetime.fromisoformat(booked_date_str.replace("Z", "")).date()
        current_date = dt.datetime.utcnow().date()
        days_diff = (current_date - booked_date).days
        
        if days_diff < 0:
            return "future_date"
        elif days_diff <= 1:
            return "24_hours_or_less"
        elif days_diff <= 30:
            return "0_to_30_days"
        elif days_diff <= 60:
            return "30_to_60_days"
        elif days_diff <= 180:
            return "60_to_180_days"
        elif days_diff <= 365:
            return "180_to_365_days"
        else:
            return "365_days_or_older"
    except Exception as e:
        print(f"[brazoria] Error calculating booking age: {e}")
        return "unknown"

def _get_booking_priority(booking_age_category: str) -> int:
    """Get priority ranking based on booking age (1 = highest priority)."""
    priority_map = {
        "24_hours_or_less": 1,
        "0_to_30_days": 2,
        "30_to_60_days": 3,
        "60_to_180_days": 4,
        "180_to_365_days": 5,
        "365_days_or_older": 6,
        "unknown": 7,
        "future_date": 8
    }
    return priority_map.get(booking_age_category, 7)

def fetch_brazoria_detail(detail_url: str, sess: Optional[requests.Session] = None) -> Dict[str, Any]:
    """
    Parse a Tyler 'Jailing Detail' page:
      - Attempt to extract per-charge rows
      - Sum bond/bail amounts when possible
    """
    if not detail_url:
        return {}
    s = sess or _session()
    r = s.get(detail_url, timeout=30, allow_redirects=True)
    r.raise_for_status()
    html = r.text
    _dump_html(html, "detail", sub="detail")

    soup = BeautifulSoup(html, "html.parser")
    charges: List[Dict[str, Any]] = []
    bond_values: List[int] = []

    # Strategy: any table with a header containing "charge" or "offense"
    for tbl in soup.select("table"):
        heads = [th.get_text(strip=True) for th in tbl.select("thead th")]
        if not heads:
            first = tbl.select_one("tr")
            if first:
                heads = [c.get_text(strip=True) for c in first.select("th,td")]
        norm_heads = [h.strip().lower() for h in heads]
        if not norm_heads:
            continue
        if not any("charge" in h or "offense" in h for h in norm_heads):
            continue

        rows = tbl.select("tbody tr") or tbl.select("tr")[1:]
        for tr in rows:
            cells = [td.get_text(strip=True) for td in tr.select("td")]
            if not cells:
                continue
            row: Dict[str, Any] = {}
            for i, h in enumerate(norm_heads):
                row[h] = cells[i] if i < len(cells) else None
            charges.append(row)

            for key in ("bond amount", "bond", "bail amount", "amount", "set bond"):
                if key in row:
                    v = _to_int_money(row.get(key))
                    if v:
                        bond_values.append(v)

    if not bond_values:
        txt = soup.get_text(" ", strip=True)
        m2 = re.search(r'total\s+bond[^$0-9]*\$?\s*([0-9][0-9,]*)', txt, flags=re.I)
        if m2:
            v = _to_int_money(m2.group(1))
            if v:
                bond_values.append(v)

    bond_total = sum(bond_values) if len(bond_values) > 1 else (bond_values[0] if bond_values else None)
    return {
        "charges": charges,
        "bond_total": bond_total,
        "detail_fetched_at": dt.datetime.now(dt.timezone.utc)
    }

def _collect_hidden_fields(soup: BeautifulSoup) -> Dict[str, str]:
    data = {}
    for inp in soup.select("input[type=hidden]"):
        name = inp.get("name")
        if not name:
            continue
        data[name] = inp.get("value", "")
    return data

def _is_public_access_error(html: str) -> bool:
    return ("Public Access Error" in html
            or "An error occurred while processing your request." in html)

def _is_search_form(html: str) -> bool:
    """
    True when we were bounced back to the search form instead of results.
    We check for the form and common markers, including an action that echoes FirstName/LastName.
    """
    if 'id="SearchParameters"' not in html:
        return False
    if ("Jail Records" in html) or ("+ First Name" in html) or ("+ Last Name" in html):
        return True
    if re.search(r'action="[^"]*JailingSearch\.aspx\?[^"]*(FirstName|LastName)=', html, flags=re.I):
        return True
    return False

def search_brazoria(
    last: str = "",
    first: str = "",
    include_details: bool = False,
    progress_cb: Optional[Callable[[int, int, str, str], None]] = None,
    since_days: Optional[int] = 60,  # NEW: limit window (None disables)
) -> List[Dict[str, Any]]:
    """
    Perform a Brazoria 'Defendant' search. If since_days is provided:
      - Try to pass booking date lower bound to the POST fallback (Tyler respects these).
      - Regardless, apply a client-side guard to drop rows older than the window.
    """
    sess = _session()

    # Enforce Brazoria requirement: must supply BOTH names for Defendant search
    if (last and not first) or (first and not last):
        _dump_html(f"<!-- missing counterpart name -->", "guard_missing_name", sub="logs")
        return []

    # Time window boundaries
    lower_date: Optional[dt.date] = None
    if since_days is not None:
        lower_date = (_utcnow() - dt.timedelta(days=since_days)).date()
    lower_str = f"{lower_date.month:02d}/{lower_date.day:02d}/{lower_date.year}" if lower_date else ""

    # 1) Warm up
    start_url = urljoin(BASE, SEARCH_PATH) + "?ID=400"
    sess.get(start_url, timeout=30, allow_redirects=True, headers={"Referer": start_url})

    # 2) Preferred: GET with params (Tyler usually ignores date filters in GET)
    q = {"ID": "400", "SearchBy": "Defendant"}
    if last:
        q["LastName"] = last
    if first:
        q["FirstName"] = first

    get_url = urljoin(BASE, SEARCH_PATH)
    r = sess.get(get_url, params=q, timeout=30, allow_redirects=True,
                 headers={"Referer": start_url, "Accept": "text/html"})
    r.raise_for_status()
    html = r.text
    _dump_html(html, f"search_last_{(last or '_')}_first_{(first or '_')}")

    need_post_fallback = _is_public_access_error(html) or _is_search_form(html)

    if need_post_fallback and last and first:
        # 3) Fallback: POST form with hidden fields + name fields and date window
        blank = sess.get(start_url, timeout=30, allow_redirects=True, headers={"Referer": start_url})
        blank.raise_for_status()
        soup_blank = BeautifulSoup(blank.text, "html.parser")
        form = soup_blank.select_one("form#SearchParameters")
        action = form.get("action") if form else f"{SEARCH_PATH}?ID=400&SearchBy=Defendant"
        post_url = urljoin(BASE, action)

        data = _collect_hidden_fields(soup_blank)
        data.update({
            "RadioSearchType": "1",
            "SearchType": "PARTYNAME",
            "NameTypeKy": "ALIAS",
            "BaseConnKy": "",
            "ShowInactive": "",
            "StatusType": "",
            "AllStatusTypes": "",
            "BondCompany": "",
            "ProductType": "",
            "SearchParams": "",
            "LastName": last or "",
            "FirstName": first or "",
            "MiddleName": "",
            "DateOfBirth": "",
            # Server-side filter attempts:
            "DateBookingOnAfter": lower_str if lower_date else "",
            "DateBookingOnBefore": "",
            "DateReleasedOnAfter": "",
            "DateReleasedOnBefore": "",
            "DatePostedOnAfter": "",
            "DatePostedOnBefore": "",
            "SearchSubmit": "Search",
        })

        headers = {
            "Referer": start_url,
            "Origin": BASE.rstrip("/"),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html",
        }
        r = sess.post(post_url, data=data, timeout=30, headers=headers, allow_redirects=True)
        r.raise_for_status()
        html = r.text
        _dump_html(html, f"search_fallback_post_last_{(last or '_')}_first_{(first or '_')}")

    # 4) Parse results page
    if _is_public_access_error(html) or _is_search_form(html):
        return []

    soup = BeautifulSoup(html, "html.parser")
    table = _pick_results_table(soup)
    if not table:
        return []

    rows = [tr for tr in table.select("tr") if tr.select("td")]
    total = len(rows)

    out: List[Dict[str, Any]] = []
    for i, tr in enumerate(rows, 1):
        tds = [td.get_text(" ", strip=True) for td in tr.select("td")]
        if len(tds) < 6:
            if progress_cb:
                try: progress_cb(i, total, last, first)
                except Exception: pass
            continue

        name_link = tr.select_one("a[href]")
        detail_href = name_link["href"] if name_link else None
        detail_url = _abs(get_url, detail_href)

        booking_date_raw = tds[2] or None
        release_date_raw = tds[3] or None
        booking_date = _parse_mmddyyyy(booking_date_raw)
        release_date = _parse_mmddyyyy(release_date_raw)

        # Client-side guard: drop if older than since_days or if booking date is missing (strict)
        if since_days is not None:
            if not booking_date:
                # Strict: skip rows without a booking date when a window is requested
                if progress_cb:
                    try: progress_cb(i, total, last, first)
                    except Exception: pass
                continue
            if lower_date and booking_date < lower_date:
                if progress_cb:
                    try: progress_cb(i, total, last, first)
                    except Exception: pass
                continue

        booking_date_iso = _iso_or_none(booking_date)
        
        # Add booking age categorization
        booking_age_category = _calculate_booking_age_category(booking_date_iso) if booking_date_iso else "unknown"
        booking_priority = _get_booking_priority(booking_age_category)

        row = {
            "booking_number": tds[0] or None,
            "name": tds[1] or None,
            "booking_date": booking_date_raw,
            "booking_date_iso": booking_date_iso,
            "booking_age_category": booking_age_category,  # NEW
            "booking_priority": booking_priority,           # NEW
            "release_date": release_date_raw,
            "release_date_iso": _iso_or_none(release_date),
            "arresting_agency": tds[4] or None,
            "charges_summary": tds[5] or None,
            "detail_url": detail_url,
            "source": "brazoria_inquiry",
            "fetched_at": dt.datetime.now(dt.timezone.utc),
            "scraped_at": dt.datetime.now(dt.timezone.utc),  # store as MongoDB Date
        }
        out.append(row)

        if progress_cb:
            try:
                progress_cb(i, total, last, first)
            except Exception:
                pass

        if include_details and detail_url:
            try:
                detail = fetch_brazoria_detail(detail_url, sess=sess)
                row.update(detail)
                time.sleep(0.4)
            except Exception as e:
                row["detail_error"] = str(e)

    return out

def _write_jsonl(rows, out_fp):
    for r in rows:
        out_fp.write(json.dumps(r, ensure_ascii=False, default=_dt_default) + "\n")

# --- Pipeline wrapper so SCRAPER_SPECS can import BrazoriaJailScraper ---
class BrazoriaJailScraper(BaseScraper):
    """
    Thin wrapper so the class-based pipeline can run Brazoria.
    Delegates to the existing ingestion logic in ingestion.brazoria_ingest.
    """
    source_name = "brazoria_jail"

    def run(self):
        # Import here to avoid circular imports
        try:
            from ingestion.brazoria_ingest import ingest_all_letters
        except Exception as e:
            raise RuntimeError(f"Failed to import brazoria_ingest.ingest_all_letters: {e}") from e

        # Read optional knobs from environment (with sensible defaults)
        letters = os.getenv("BRAZORIA_LETTERS", "A-Z")
        first_letters = os.getenv("BRAZORIA_FIRST_LETTERS", "")
        append_wildcard = os.getenv("BRAZORIA_APPEND_WILDCARD", "false").lower() == "true"
        since_days = int(os.getenv("BRAZORIA_SINCE_DAYS", "365"))
        tick_every = int(os.getenv("BRAZORIA_TICK_EVERY", "50"))
        include_details = os.getenv("BRAZORIA_INCLUDE_DETAILS", "true").lower() != "false"

        # Delegate to the working ingestion function that writes to Mongo
        result = ingest_all_letters(
            include_details=include_details,
            letters=letters,
            first_letters=first_letters or None,
            verbose=True,
            tick_every=tick_every,
            append_wildcard=append_wildcard,
            since_days=since_days,
        )

        # If BaseScraper expects counters, return whatever ingest_all_letters returns
        return result

# -----------------------------------------------

if __name__ == "__main__":
    import argparse, json, sys
    ap = argparse.ArgumentParser("Brazoria Jail (Tyler Public Access) search tester")
    ap.add_argument("--last", default="", help="Last name (full or partial)")
    ap.add_argument("--first", default="", help="First name (full or partial)")
    ap.add_argument("--details", action="store_true", help="Fetch details for each row")
    ap.add_argument("--since-days", type=int, default=60, help="Only include bookings within the last N days (default 60)")
    ap.add_argument("--jsonl", action="store_true", help="Emit newline-delimited JSON (one record per line)")
    ap.add_argument("--out", default="-", help="Output path (default stdout). Used only with --jsonl")
    args = ap.parse_args()

    # Friendly CLI guard for the Brazoria rule
    if (args.last and not args.first) or (args.first and not args.last):
        print(json.dumps({
            "error": "Brazoria Defendant search requires BOTH --last and --first.",
            "hint": "Try: --last SMITH --first JOHN"
        }, indent=2))
        sys.exit(2)

    rows = search_brazoria(last=args.last, first=args.first, include_details=args.details, since_days=args.since_days)

    if args.jsonl:
        if args.out == "-" or args.out == "":
            _write_jsonl(rows, sys.stdout)
        else:
            with open(args.out, "w", encoding="utf-8") as f:
                _write_jsonl(rows, f)
    else:
        print(json.dumps({"count": len(rows), "results": rows[:10]}, indent=2, default=_dt_default))