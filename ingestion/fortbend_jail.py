# ingestion/fortbend_jail.py
import os
import re
import time
import datetime as dt
from typing import List, Dict, Any, Optional, Callable
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = os.getenv("FORTBEND_BASE_URL", "https://jailinq.fortbendcountytx.gov/")

def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def _to_int_money(s: str | None) -> int | None:
    if not s:
        return None
    # pull the first “$1,234” or “1234” looking number
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
    """Write HTML to debug folder and return the file path (as string)."""
    base = (DUMP_DIR / sub) if sub else DUMP_DIR
    base.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = base / f"{prefix}_{stamp}.html"
    path.write_text(content, encoding="utf-8")
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

    # sometimes “Total Bond” appears outside tables
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
            "fetched_at": _utcnow_iso(),
        }

        # Fort Bend columns appear shifted:
        if isinstance(row["name"], str) and row["name"].isdigit() and isinstance(row["id"], str) and "," in row["id"]:
            row["booking_number"] = row["name"]
            row["name"], row["id"] = row["id"], row["dob"]  # id <= previous tds[2] (VarJailID)
            row["dob"] = None

        if include_details and detail_url:
            try:
                detail = fetch_fort_bend_detail(detail_url, sess=sess)
                row.update(detail)
                time.sleep(0.5)  # be polite
            except Exception as e:
                row["detail_error"] = str(e)

        rows_out.append(row)

    return rows_out

if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser("Fort Bend Jail search tester")
    ap.add_argument("--last", default="", help="Last name (full or partial)")
    ap.add_argument("--first", default="", help="First name (full or partial)")
    ap.add_argument("--details", action="store_true", help="Fetch each inmate's detail page for charges/bond")
    args = ap.parse_args()

    rows = search_fort_bend(last=args.last, first=args.first, include_details=args.details)
    print(json.dumps({"count": len(rows), "results": rows[:10]}, indent=2))