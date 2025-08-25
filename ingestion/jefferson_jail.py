# ingestion/jefferson_jail.py
from __future__ import annotations

import os, time, re, uuid
import requests
from typing import Any, Dict, Iterable, List, Optional, Tuple
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from pathlib import Path

from .base_scraper import BaseScraper

BASE = "https://jeffersoncountytx.gov/InmateSearch"
SEARCH_URL = f"{BASE}/Search/List"

# Updated User Agent
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# --- env knobs ---
ROW_DELAY = float(os.getenv("JEFF_ROW_DELAY_SEC", "0.6"))
REQ_TIMEOUT = int(os.getenv("JEFF_REQ_TIMEOUT", "30"))
APPEND_WILDCARD_DEFAULT = False  # Wildcards don't work
MAX_RESULTS_PER_PREFIX = int(os.getenv("JEFF_MAX_RESULTS_PER_PREFIX", "2000"))
AUDIT_ENABLE = os.getenv("SCRAPER_AUDIT", "true").strip().lower() in ("1","true","yes")
SNAPSHOT_ENABLE = os.getenv("JEFF_SNAPSHOT", "true").strip().lower() in ("1","true","yes")
SNAPSHOT_DIR = os.getenv("JEFF_SNAPSHOT_DIR", "debug/jefferson")
SNAPSHOT_OVERWRITE = os.getenv("JEFF_SNAPSHOT_OVERWRITE", "false").strip().lower() in ("1","true","yes")
SNAPSHOT_KEEP_PER_KIND = int(os.getenv("JEFF_MAX_SNAPSHOTS_PER_KIND", "20"))
SNAPSHOT_MAX_TOTAL = int(os.getenv("JEFF_MAX_SNAPSHOTS_TOTAL", "200"))
SEARCH_DELAY = float(os.getenv("JEFF_SEARCH_DELAY_SEC", "1"))

# ------- helpers -------
def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass

def _sanitize_name(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", (s or "")).strip("_")[:160]

def _prune_kind(kind: str) -> None:
    if not SNAPSHOT_ENABLE:
        return
    p = Path(SNAPSHOT_DIR)
    if not p.exists():
        return
    files = sorted((f for f in p.glob(f"{kind}_*.html") if f.is_file()),
                   key=lambda f: f.stat().st_mtime)
    excess = max(0, len(files) - max(0, SNAPSHOT_KEEP_PER_KIND))
    for f in files[:excess]:
        try:
            f.unlink()
        except Exception:
            pass

def _prune_global() -> None:
    if not SNAPSHOT_ENABLE:
        return
    p = Path(SNAPSHOT_DIR)
    if not p.exists():
        return
    files = sorted((f for f in p.glob("*.html") if f.is_file()),
                   key=lambda f: f.stat().st_mtime)
    excess = max(0, len(files) - max(0, SNAPSHOT_MAX_TOTAL))
    for f in files[:excess]:
        try:
            f.unlink()
        except Exception:
            pass

def _write_snapshot(kind: str, name: str, html: str) -> None:
    if not SNAPSHOT_ENABLE:
        return
    _ensure_dir(SNAPSHOT_DIR)
    try:
        kind = _sanitize_name(kind)
        name = _sanitize_name(name)

        if SNAPSHOT_OVERWRITE:
            out = Path(SNAPSHOT_DIR) / f"{kind}_latest.html"
        else:
            stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            out = Path(SNAPSHOT_DIR) / f"{kind}_{stamp}_{name}.html"

        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[jeff] snapshot → {out}")

        if not SNAPSHOT_OVERWRITE:
            _prune_kind(kind)
            _prune_global()
    except Exception:
        pass

def _money_to_float(s: str | None) -> Optional[float]:
    if not s:
        return None
    s = s.replace(",", "").replace("$", "")
    m = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", s)
    return float(m.group(1)) if m else None

def _clean_txt(x: Any) -> str:
    return re.sub(r"\s+", " ", (x or "").strip())

def _split_name(full: str) -> Tuple[str, str]:
    full = _clean_txt(full)
    if "," in full:
        last, first = [p.strip() for p in full.split(",", 1)]
        return first, last
    parts = full.split()
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    return full, ""

def _pick(*vals):
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None

def _iso_date_guess(s: str | None) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    
    # Handle MM/DD/YYYY format
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mm, dd, yy = map(int, m.groups())
        try:
            return datetime(yy, mm, dd).date().isoformat()
        except Exception:
            pass
    
    # Handle datetime strings like "9/13/2021 1:17:00 PM"
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+\d{1,2}:\d{2}:\d{2}\s+[AP]M", s)
    if m:
        mm, dd, yy = map(int, m.groups())
        try:
            return datetime(yy, mm, dd).date().isoformat()
        except Exception:
            pass
    
    try:
        return datetime.fromisoformat(s.replace("Z","")).date().isoformat()
    except Exception:
        return None

def _extract_detail_links(list_html: str) -> List[str]:
    """Extract inmate detail links from search results."""
    soup = BeautifulSoup(list_html or "", "lxml")
    links: List[str] = []

    def _abs(u: str) -> str:
        return urljoin(BASE + "/", u)

    def _is_detail(href: str) -> bool:
        if not href:
            return False
        h = href.lower()
        return bool(re.search(r"/inmatesearch/(search/)?detail(s)?/\d+", h))

    # Extract from clickable rows (primary method)
    for row in soup.select("tr.clickable-row[data-href]"):
        dh = (row.get("data-href") or "").strip()
        if _is_detail(dh):
            abs_url = _abs(dh)
            if abs_url not in links:
                links.append(abs_url)

    # Extract from regular links as backup
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if _is_detail(href):
            abs_url = _abs(href)
            if abs_url not in links:
                links.append(abs_url)

    return links

def _extract_property_pairs(soup: BeautifulSoup) -> Dict[str, str]:
    """Extract key-value pairs from detail-property-title/detail-property-value divs."""
    out: Dict[str, str] = {}
    
    # Find all title divs
    title_divs = soup.select("div.detail-property-title")
    
    for title_div in title_divs:
        title = _clean_txt(title_div.get_text())
        if not title:
            continue
            
        # Find the corresponding value div (should be the next sibling)
        value_div = title_div.find_next_sibling("div", class_="detail-property-value")
        if value_div:
            value = _clean_txt(value_div.get_text())
            out[title] = value
    
    return out

def _extract_charges(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """Extract charges from the offenses table."""
    charges: List[Dict[str, str]] = []
    
    # Look for the results table
    table = soup.select_one("table#results-table")
    if not table:
        return charges
    
    # Get headers
    headers = []
    thead = table.find("thead")
    if thead:
        headers = [th.get_text(strip=True).lower() for th in thead.find_all("th")]
    
    if not headers:
        return charges
    
    # Map column indices - based on the HTML structure we saw
    idx = {
        "offense": next((i for i, h in enumerate(headers) if "offense" in h), None),
        "class": next((i for i, h in enumerate(headers) if "class" in h), None),
        "warrant": next((i for i, h in enumerate(headers) if "warrant" in h), None),
        "bond": next((i for i, h in enumerate(headers) if "bond" in h and "amount" in h), None),
        "condition": next((i for i, h in enumerate(headers) if "condition" in h), None),
    }
    
    # Extract data rows
    tbody = table.find("tbody")
    if tbody:
        rows = tbody.find_all("tr")
        for tr in rows:
            cells = tr.find_all("td")
            if not cells:
                continue
            
            def cell(i): 
                return cells[i].get_text(strip=True) if (i is not None and i < len(cells)) else ""
            
            charge_data = {
                "charge": cell(idx["offense"]) or "",
                "status": cell(idx["class"]) or "",
                "docket": cell(idx["warrant"]) or "",
                "bond": cell(idx["bond"]) or "",
            }
            
            # Add bond condition if available
            if idx["condition"] is not None and idx["condition"] < len(cells):
                condition_cell = cells[idx["condition"]]
                # Extract text from any nested lists
                condition_text = []
                for li in condition_cell.find_all("li"):
                    condition_text.append(li.get_text(strip=True))
                if condition_text:
                    charge_data["condition"] = "; ".join(condition_text)
                else:
                    charge_data["condition"] = condition_cell.get_text(strip=True)
            
            if charge_data["charge"]:  # Only add if we have a charge
                charges.append(charge_data)
    
    return charges

def _looks_like_inmate_detail(html: str) -> bool:
    """Check if this looks like an inmate detail page."""
    if not html:
        return False
        
    soup = BeautifulSoup(html, "lxml")
    
    # Look for the specific detail page structure
    has_property_divs = len(soup.select("div.detail-property-title")) > 0
    has_h1_after_form = False
    
    # Check for H1 tag that comes after the search form
    search_form = soup.select_one("div#inmate-search-form")
    if search_form:
        # Find H1 elements that come after the search form
        for h1 in soup.select("h1"):
            # Skip the H1 inside the search form
            if search_form in h1.parents:
                continue
            # This H1 is outside the search form, likely the inmate name
            text = h1.get_text(strip=True)
            if text and text != "Inmate Search":
                has_h1_after_form = True
                break
    
    return has_property_divs and has_h1_after_form

def _calculate_booking_age_category(booked_date_str: str) -> str:
    """Calculate how long ago someone was booked and return a category."""
    if not booked_date_str:
        return "unknown"
    
    try:
        # Parse the booking date
        booked_date = datetime.fromisoformat(booked_date_str).date()
        current_date = datetime.utcnow().date()
        days_diff = (current_date - booked_date).days
        
        if days_diff < 0:
            return "future_date"  # Shouldn't happen, but handle it
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
        print(f"[jeff] Error calculating booking age: {e}")
        return "unknown"

# ------- main scraper -------
class JeffersonJailScraper(BaseScraper):
    name = "jefferson_jail"

    def __init__(self, db):
        super().__init__(db)
        self._sess = requests.Session()
        self._sess.headers.update(UA)
        
        # Initialize session
        try:
            init_response = self._sess.get(BASE, timeout=REQ_TIMEOUT)
            print(f"[jeff] Session initialized, status: {init_response.status_code}")
        except Exception as e:
            print(f"[jeff] Warning: Could not initialize session: {e}")

        self._aud = {
            "run_id": f"jefferson:{uuid.uuid4()}",
            "county": "Jefferson",
            "source": "jefferson_jail",
            "started_at": datetime.utcnow().isoformat() + "Z",
            "letters_spec": None,
            "first_letters_spec": None,
            "append_wildcard": None,
            "prefixes_scanned": 0,
            "detail_links_found": 0,
            "details_parsed_ok": 0,
            "upserts_person_inserted": 0,
            "upserts_person_updated": 0,
            "events_yielded": 0,
            "errors": 0,
            "notes": [],
        }

    def _audit_emit(self, status: str, extra: Dict[str, Any] | None = None):
        if not AUDIT_ENABLE:
            return
        doc = {
            "kind": "scrape_audit",
            "status": status,
            **self._aud,
            "ts": datetime.utcnow().isoformat() + "Z",
        }
        if extra:
            doc.update(extra)
        try:
            self.db["scrape_audit"].insert_one(doc)
        except Exception:
            pass

    def _audit_note(self, msg: str):
        self._aud["notes"].append(msg)
        self._audit_emit("note", {"msg": msg})

    def _audit_inc(self, key: str, n: int = 1):
        self._aud[key] = int(self._aud.get(key, 0)) + n

    def _search(self, last_prefix: str, first_prefix: Optional[str], append_wildcard: bool) -> str:
        """Search for inmates - wildcards disabled."""
        ln = last_prefix
        fn = first_prefix or ""
        
        # Don't use wildcards - they don't work
        params = {"lastName": ln}
        if fn:
            params["firstName"] = fn
            
        print(f"[jeff] Searching: {params}")
        
        try:
            r = self._sess.get(SEARCH_URL, params=params, timeout=REQ_TIMEOUT)
            print(f"[jeff] Response: {r.status_code}, length: {len(r.text)}")
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"[jeff] Search error: {e}")
            raise

    def _parse_detail(self, html: str, url: str) -> Optional[Dict[str, Any]]:
        """Parse inmate detail page using the correct structure."""
        soup = BeautifulSoup(html or "", "lxml")
        
        # Extract name from H1 (outside the search form)
        name = ""
        search_form = soup.select_one("div#inmate-search-form")
        
        for h1 in soup.select("h1"):
            # Skip H1 inside the search form
            if search_form and search_form in h1.parents:
                continue
            
            text = _clean_txt(h1.get_text())
            if text and text != "Inmate Search":
                name = text
                print(f"[jeff] Found name: {name}")
                break
        
        if not name:
            print(f"[jeff] Could not extract inmate name from {url}")
            return None

        # Extract property pairs
        properties = _extract_property_pairs(soup)
        print(f"[jeff] Extracted {len(properties)} properties: {list(properties.keys())}")

        # Map the properties to our fields
        jail_entry_time = properties.get("Jail Entry Time")
        arresting_agency = properties.get("Arresting Agency")
        age_at_arrest = properties.get("Age at Arrest")
        race = properties.get("Race")
        gender = properties.get("Gender")

        # Extract charges
        charges = _extract_charges(soup)
        print(f"[jeff] Extracted {len(charges)} charges")

        # Calculate total bond from charges
        total_bond_amount = 0.0
        for charge in charges:
            bond_str = charge.get("bond", "")
            bond_val = _money_to_float(bond_str)
            if bond_val:
                total_bond_amount += bond_val

        # Create external ID from URL (contains unique inmate ID)
        ext_id = None
        url_match = re.search(r'/Detail/(\d+)', url)
        if url_match:
            ext_id = f"jefferson:{url_match.group(1)}"

        # Calculate booking age category
        booking_date_iso = _iso_date_guess(jail_entry_time)
        booking_age_category = _calculate_booking_age_category(booking_date_iso)
        
        # Determine priority based on recency (most recent = highest priority)
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
        priority = priority_map.get(booking_age_category, 7)

        first, last = _split_name(name)
        
        person = {
            "_ext_id": ext_id or f"jefferson:{name.upper()}|{jail_entry_time or ''}",
            "full_name": name.upper(),
            "aka": [],
            "dob": None,  # DOB not provided in this format
            "identifiers": {"inmate_id": [url_match.group(1)] if url_match else []},
            "contact": {},
            "media": [],  # No mugshots in current format
            "links": [{"rel": "jefferson_detail", "url": url}],
        }

        event = {
            "_collection": "jefferson_events",  # Raw Jefferson data goes here
            "person_id": None,
            "county": "Jefferson",
            "facility": "Jefferson County Jail",
            "full_name": name.upper(),  # Add full name to events
            "first_name": first,        # Add first name to events  
            "last_name": last,          # Add last name to events
            "booking_number": None,
            "status": "In Custody",
            "booked_at": booking_date_iso,
            "booking_age_category": booking_age_category,  # NEW: Time-based category
            "booking_priority": priority,                   # NEW: Priority ranking (1=most recent)
            "released_at": None,
            "source_url": url,
            "scraped_at": datetime.utcnow().isoformat() + "Z",
            "charges": charges,
            "bonds": [],
            "total_bond": total_bond_amount if total_bond_amount > 0 else None,
            "agency": arresting_agency,
            "arrest_date": _iso_date_guess(jail_entry_time),  # Using jail entry as arrest date
            "race": race,
            "sex": gender,
            "age": age_at_arrest,
        }
        
        return {"person": person, "event": event}

    def fetch(self, *, letters: str = "A-C", first_letters: str = "", append_wildcard: Optional[bool] = None) -> Iterable[Dict[str, Any]]:
        """Fetch inmates from Jefferson County jail."""
        letters = os.getenv("JEFF_LETTERS", letters)
        first_letters = os.getenv("JEFF_FIRST_LETTERS", first_letters)
        append_wildcard = False  # Always false - wildcards don't work
        
        print(f"[jeff] START: letters={letters} first_letters={first_letters or '(none)'}")
        self._aud["letters_spec"] = letters
        self._aud["first_letters_spec"] = first_letters or ""
        self._aud["append_wildcard"] = append_wildcard
        self._audit_emit("start")

        def expand_range(spec: str) -> List[str]:
            spec = (spec or "").strip()
            if not spec:
                return []
            if "-" in spec and len(spec) >= 3:
                a, b = spec.split("-", 1)
                a, b = a.strip().upper(), b.strip().upper()
                if len(a) == len(b) == 1:
                    return [chr(c) for c in range(ord(a), ord(b) + 1)]
                if len(a) == len(b) and a[:-1] == b[:-1]:
                    start, end = a[-1], b[-1]
                    return [a[:-1] + chr(c) for c in range(ord(start), ord(end) + 1)]
                return [a, b]
            if "," in spec:
                return [s.strip().upper() for s in spec.split(",") if s.strip()]
            return [spec.upper()]

        last_prefixes = expand_range(letters)
        first_prefixes = expand_range(first_letters) if first_letters else [""]

        total_seen = 0
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = 5

        for lp in last_prefixes:
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"[jeff] Stopping due to {consecutive_failures} consecutive failures")
                break
                
            print(f"[jeff] Processing last-prefix = {lp}")
            
            for fp in first_prefixes:
                self._audit_inc("prefixes_scanned", 1)
                
                try:
                    html = self._search(lp, fp or None, append_wildcard)
                    consecutive_failures = 0
                except Exception as e:
                    consecutive_failures += 1
                    print(f"[jeff] Search error #{consecutive_failures}: {e}")
                    self._audit_inc("errors", 1)
                    if SEARCH_DELAY > 0:
                        time.sleep(SEARCH_DELAY)
                    continue

                _write_snapshot("search", f"{lp}_{fp or 'NONE'}", html)

                # Check for "no results" message
                if "No results found" in html:
                    print(f"[jeff] No results for last={lp}, first={fp or ''}")
                    if SEARCH_DELAY > 0:
                        time.sleep(SEARCH_DELAY)
                    continue

                links = _extract_detail_links(html)
                self._audit_inc("detail_links_found", len(links))

                if len(links) > MAX_RESULTS_PER_PREFIX:
                    msg = f"{len(links)} links > cap {MAX_RESULTS_PER_PREFIX}; skipping"
                    print(f"[jeff] {msg}")
                    self._audit_note(msg)
                    if SEARCH_DELAY > 0:
                        time.sleep(SEARCH_DELAY)
                    continue

                print(f"[jeff] {lp} {fp or ''} → {len(links)} links")
                total_seen += len(links)

                for i, url in enumerate(links):
                    print(f"[jeff] Processing {i+1}/{len(links)}: {url}")
                    
                    try:
                        r = self._sess.get(url, timeout=REQ_TIMEOUT)
                        r.raise_for_status()
                        detail_html = r.text

                        if not _looks_like_inmate_detail(detail_html):
                            print(f"[jeff] Not a detail page: {r.url}")
                            self._audit_inc("errors", 1)
                            continue

                        rec = self._parse_detail(detail_html, r.url)
                        if not rec:
                            print(f"[jeff] Failed to parse: {r.url}")
                            self._audit_inc("errors", 1)
                            continue

                        person = rec["person"]
                        event = rec["event"]
                        
                        booking_category = event.get("booking_age_category", "unknown")
                        print(f"[jeff] SUCCESS: {person.get('full_name', 'UNKNOWN')} [{booking_category}]")

                        res = self.upsert_person(person)
                        if res.get("inserted"):
                            self._audit_inc("upserts_person_inserted", 1)
                        else:
                            self._audit_inc("upserts_person_updated", 1)

                        pid = res.get("_id")
                        event["person_id"] = pid
                        self._audit_inc("details_parsed_ok", 1)
                        self._audit_inc("events_yielded", 1)
                        yield event

                    except Exception as e:
                        print(f"[jeff] ERROR processing {url}: {e}")
                        self._audit_inc("errors", 1)
                        continue

                    if ROW_DELAY > 0:
                        time.sleep(ROW_DELAY)

                if SEARCH_DELAY > 0:
                    time.sleep(SEARCH_DELAY)

        print(f"[jeff] COMPLETED: {total_seen} detail pages processed")
        
        # Print booking age summary
        print(f"[jeff] BOOKING AGE SUMMARY:")
        self._audit_emit("done", {
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "summary_seen_links": total_seen
        })