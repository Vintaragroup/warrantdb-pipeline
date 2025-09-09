# ingestion/fortbend_jail.py
import os
import glob
import re
import time
import datetime as dt
from typing import List, Dict, Any, Optional, Callable
from pathlib import Path
from urllib.parse import urljoin
import inspect

import requests
from bs4 import BeautifulSoup
from ingestion.base_scraper import BaseScraper

BASE = os.getenv("FORTBEND_BASE_URL", "https://jailinq.fortbendcountytx.gov/")

def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def _to_int_money(s: str | None) -> int | None:
    if not s:
        return None
    # pull the first "$1,234" or "1234" looking number
    m = re.search(r'(\$?\s*[0-9][0-9,]*)', s.replace('\xa0', ' '))
    if not m:
        return None
    digits = re.sub(r'[^0-9]', '', m.group(1))
    return int(digits) if digits.isdigit() else None

def _parse_date(s: str | None) -> Optional[str]:
    """Parse various date formats to ISO date string."""
    if not s:
        return None
    s = s.strip()
    
    # Handle MM/DD/YYYY format
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mm, dd, yy = map(int, m.groups())
        try:
            return dt.datetime(yy, mm, dd).date().isoformat()
        except Exception:
            pass
    
    # Handle datetime strings like "9/13/2021 1:17:00 PM"
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+\d{1,2}:\d{2}:\d{2}\s+[AP]M", s)
    if m:
        mm, dd, yy = map(int, m.groups())
        try:
            return dt.datetime(yy, mm, dd).date().isoformat()
        except Exception:
            pass
    
    try:
        return dt.datetime.fromisoformat(s.replace("Z","")).date().isoformat()
    except Exception:
        return None

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
        print(f"[fortbend] Error calculating booking age: {e}")
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

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/127.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
        "Referer": BASE,
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s

def _abs(base: str, href: str | None) -> str | None:
    return urljoin(base, href) if href else None

# Where debug HTML files go (override with env FORTBEND_DUMP_DIR)
DUMP_DIR = Path(os.getenv("FORTBEND_DUMP_DIR", "debug_dumps/fortbend"))
DUMP_DIR.mkdir(parents=True, exist_ok=True)

def _dump_html(content: str, prefix: str, *, sub: str = "") -> str:
    """Write HTML to debug folder and return the file path (as string). Keeps only N most recent files."""
    base = (DUMP_DIR / sub) if sub else DUMP_DIR
    base.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = base / f"{prefix}_{stamp}.html"
    path.write_text(content, encoding="utf-8")

    # Prune older debug files, keep only the most recent N
    max_debug = int(os.getenv("FORTBEND_MAX_DEBUG", "20"))
    # glob for files matching this prefix in this folder
    pattern = str(base / f"{prefix}_*.html")
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    for oldfile in files[max_debug:]:
        try:
            os.remove(oldfile)
        except Exception:
            pass

    return str(path)

def fetch_fort_bend_detail(detail_url: str, sess: requests.Session | None = None) -> Dict[str, Any]:
    """
    GET the inmate detail page and parse charges / bond amounts.
    Returns:
      {
        "charges": [ {<column>: <value>, ...}, ... ],
        "bond_total": <int>|None,
        "detail_fetched_at": "<iso>",
      }
    """
    if not detail_url:
        return {}

    s = sess or _session()
    r = s.get(detail_url, timeout=30, allow_redirects=True)
    r.raise_for_status()
    html = r.text

    # dump detail HTML for debugging (in subfolder)
    jail_id = None
    m = re.search(r'VarJailID=([A-Za-z0-9]+)', detail_url)
    if m:
        jail_id = m.group(1)
    _dump_html(html, f"detail_{jail_id or 'unknown'}")

    soup = BeautifulSoup(html, "html.parser")

    def headers_for(table) -> List[str]:
        # pull header cells; if none, try first row as header
        heads = [th.get_text(strip=True) for th in table.select("thead th")]
        if not heads:
            first = table.select_one("tr")
            if first:
                heads = [th.get_text(strip=True) for th in first.select("th, td")]
        # normalize to lowercase for matching/keys
        return [h.strip().lower() for h in heads if h.strip()]

    charges: List[Dict[str, Any]] = []
    bond_values: List[int] = []

    candidate_tables = soup.select("table")
    target = None
    for t in candidate_tables:
        heads = headers_for(t)
        if any(x in heads for x in ("charge", "charge description", "offense", "description")):
            target = t
            break
    if target is None and candidate_tables:
        # fallback: widest table by columns
        target = max(candidate_tables, key=lambda t: len(headers_for(t)))

    if target:
        heads = headers_for(target)
        # data rows
        body_rows = target.select("tbody tr") or target.select("tr")[1:]
        for tr in body_rows:
            cells = [td.get_text(strip=True) for td in tr.select("td")]
            if not cells:
                continue
            row: Dict[str, Any] = {}
            for i, h in enumerate(heads):
                row[h] = cells[i] if i < len(cells) else None
            charges.append(row)

            # normalize a few known columns and add parsed ints
            norm = {}
            for k, v in row.items():
                kk = k.strip().lower().replace(" ", "_")  # simple snake_case
                norm[kk] = v

            # keep original, but add normalized view + parsed int for bail amount
            row.update(norm)
            if "bail_amount" in norm:
                row["bail_amount_int"] = _to_int_money(norm["bail_amount"])

            # look for bond-ish columns among normalized keys
            for key in ("bond", "bond amount", "bond amt", "bond ($)", "amount", "set bond", "bail amount"):
                if key in row:
                    v = _to_int_money(row.get(key))
                    if v:
                        bond_values.append(v)

    # sometimes "Total Bond" appears outside tables
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
        "detail_fetched_at": _utcnow_iso(),
    }

def search_fort_bend(
    last: str = "",
    first: str = "",
    include_details: bool = False,
    progress_cb: Optional[Callable[..., None]] = None,  # accepts (i,total,...) or (i,total)
) -> List[Dict[str, Any]]:
    """Submit GET /?LastName=...&FirstName=...&SearchButton=Search and parse the results table."""
    sess = _session()

    # warm up (cookies, anti-forgery)
    sess.get(BASE, timeout=30, allow_redirects=True)

    params: Dict[str, str] = {}
    if last:
        params["LastName"] = last
    if first:
        params["FirstName"] = first
    params["SearchButton"] = "Search"  # REQUIRED

    r = sess.get(BASE, params=params, timeout=30, allow_redirects=True)
    r.raise_for_status()
    html = r.text

    # save search HTML for debugging (subfolder)
    _dump_html(html, f"search_last_{(last or '_')}_first_{(first or '_')}")

    soup = BeautifulSoup(html, "html.parser")

    table = soup.select_one("#InmatesTable")
    if not table:
        candidates = soup.select("table")
        if candidates:
            table = max(
                candidates,
                key=lambda t: len(t.select("thead th, tr:first-child th, tr:first-child td"))
            )

    if not table:
        return []

    body_rows = table.select("tbody tr") or table.select("tr")[1:]
    total = len(body_rows)
    rows_out: List[Dict[str, Any]] = []

    for i, tr in enumerate(body_rows, start=1):
        # progress callback (safe/no-op if not provided)
        if progress_cb:
            try:
                # try the richer signature first
                progress_cb(i, total, last, first)
            except TypeError:
                # fall back to simple (i,total)
                try:
                    progress_cb(i, total)
                except Exception:
                    pass
            except Exception:
                pass

        tds = [td.get_text(strip=True) for td in tr.select("td")]
        if not tds:
            continue

        name_link = tr.select_one("td a[href]")
        detail_href = name_link.get("href") if name_link else None
        detail_url = _abs(BASE, detail_href)

        row: Dict[str, Any] = {
            "name": tds[0] if len(tds) > 0 else None,         # often booking number on this site
            "id":   tds[1] if len(tds) > 1 else None,         # often LAST,FIRST here
            "dob":  tds[2] if len(tds) > 2 else None,         # often VarJailID (P00…)
            "booking_date": tds[3] if len(tds) > 3 else None, # sometimes this is race/booking date
            "detail_url": detail_url,
            "source": "fortbend_inquiry",
            "fetched_at": dt.datetime.now(dt.timezone.utc),
            "scraped_at": dt.datetime.now(dt.timezone.utc),  # store as MongoDB Date
        }

        # Fort Bend columns appear shifted:
        if isinstance(row["name"], str) and row["name"].isdigit() and isinstance(row["id"], str) and "," in row["id"]:
            row["booking_number"] = row["name"]
            row["name"], row["id"] = row["id"], row["dob"]  # id <= previous tds[2] (VarJailID)
            row["dob"] = None

        # Parse booking date and add categorization
        booking_date_iso = _parse_date(row.get("booking_date"))
        if booking_date_iso:
            row["booking_date_iso"] = booking_date_iso
            row["booking_age_category"] = _calculate_booking_age_category(booking_date_iso)
            row["booking_priority"] = _get_booking_priority(row["booking_age_category"])
        else:
            row["booking_date_iso"] = None
            row["booking_age_category"] = "unknown"
            row["booking_priority"] = 7

        if include_details and detail_url:
            try:
                detail = fetch_fort_bend_detail(detail_url, sess=sess)
                row.update(detail)
                time.sleep(0.5)  # be polite
            except Exception as e:
                row["detail_error"] = str(e)

        rows_out.append(row)

    return rows_out

 # --- Pipeline wrapper so SCRAPER_SPECS can import FortBendJailScraper ---
class FortBendJailScraper(BaseScraper):
    """
    Thin wrapper so the class-based pipeline can run Fort Bend.
    Delegates to the existing ingestion logic in ingestion.fortbend_ingest.
    """
    source_name = "fortbend_jail"

    def run(self):
        # Import here to avoid circular imports
        try:
            from ingestion.fortbend_ingest import ingest_all_letters
        except Exception as e:
            raise RuntimeError(f"Failed to import fortbend_ingest.ingest_all_letters: {e}") from e

        # Allow env knobs (mirrors Brazoria)
        letters = os.getenv("FORTBEND_LETTERS", "A-Z")
        first_letters = os.getenv("FORTBEND_FIRST_LETTERS", "")
        append_wildcard = os.getenv("FORTBEND_APPEND_WILDCARD", "false").lower() == "true"
        since_days = int(os.getenv("FORTBEND_SINCE_DAYS", "365"))
        tick_every = int(os.getenv("FORTBEND_TICK_EVERY", "50"))
        include_details = os.getenv("FORTBEND_INCLUDE_DETAILS", "true").lower() != "false"

        # Build kwargs and filter to match ingest_all_letters signature
        kwargs = {
            "include_details": include_details,
            "letters": letters,
            "first_letters": (first_letters or None),
            "verbose": True,
            "tick_every": tick_every,
            "append_wildcard": append_wildcard,
            "since_days": since_days,
        }
        sig = inspect.signature(ingest_all_letters)
        filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return ingest_all_letters(**filtered)

if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser("Fort Bend Jail search tester")
    ap.add_argument("--last", default="", help="Last name (full or partial)")
    ap.add_argument("--first", default="", help="First name (full or partial)")
    ap.add_argument("--details", action="store_true", help="Fetch each inmate's detail page for charges/bond")
    args = ap.parse_args()

    rows = search_fort_bend(last=args.last, first=args.first, include_details=args.details)
    
    # Enhanced output with booking categories
    categories = {}
    for r in rows:
        cat = r.get("booking_age_category", "unknown")
        categories[cat] = categories.get(cat, 0) + 1

    def _dt_default(o):
        if isinstance(o, dt.datetime):
            return o.replace(tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")
        return str(o)
    
    print(json.dumps({
        "count": len(rows), 
        "booking_categories": dict(sorted(categories.items())),
        "results": rows[:10]
    }, indent=2, default=_dt_default))