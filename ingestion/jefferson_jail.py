
from __future__ import annotations


import os, time, re
import requests
from typing import Any, Dict, Iterable, List, Optional, Tuple
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from pathlib import Path
import threading

def load_surnames(path="configs/jefferson_lastnames.txt") -> List[str]:
    """Load non-empty, stripped lines from a surname file if it exists."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        surnames = [line.strip().upper() for line in f if line.strip()]
    return surnames



from ingestion.audited_scraper import AuditedScraper

BASE = "https://jeffersoncountytx.gov/InmateSearch"
# The page that contains the search form
SEARCH_FORM_URL = f"{BASE}/Search"
# The endpoint that shows the results listing
SEARCH_LIST_URL = f"{BASE}/Search/List"

# Updated User Agent
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# --- Surname file constant ---
SURNAME_FILE = os.getenv("JEFF_SURNAME_FILE", "configs/jefferson_lastnames.txt")

# --- helper to fetch anti-forgery token (robust) ---
def _discover_antiforgery(sess: requests.Session) -> Dict[str, Optional[str]]:
    """
    Return {'form': token_from_hidden, 'header': token_for_header}.
    Some ASP.NET sites only emit a cookie (.AspNetCore.Antiforgery*). In that case,
    they expect the matching token in a header named 'RequestVerificationToken'.
    Also snapshot the form HTML for inspection.
    """
    out: Dict[str, Optional[str]] = {"form": None, "header": None}
    try:
        r = sess.get(SEARCH_FORM_URL, timeout=REQ_TIMEOUT)
        r.raise_for_status()
    except Exception:
        return out
    # snapshot the form for debugging
    _write_snapshot("search_form", "initial", r.text)
    soup = BeautifulSoup(r.text, "html.parser")
    hid = soup.find("input", {"name": "__RequestVerificationToken"})
    if hid and hid.has_attr("value"):
        out["form"] = hid["value"]
        out["header"] = hid["value"]
    # alternate meta placement
    if not out["form"]:
        meta = soup.find("meta", {"name": "__RequestVerificationToken"})
        if meta and meta.has_attr("content"):
            out["form"] = meta["content"]
            out["header"] = meta["content"]
    # antiforgery cookie fallback
    if not out["header"]:
        for c in sess.cookies:
            if ("Antiforgery" in c.name) or ("RequestVerificationToken" in c.name) or c.name.startswith(".AspNetCore.Antiforgery"):
                out["header"] = c.value
                break
    return out

# --- env knobs ---
ROW_DELAY = float(os.getenv("JEFF_ROW_DELAY_SEC", "0.6"))
REQ_TIMEOUT = int(os.getenv("JEFF_REQ_TIMEOUT", "30"))
APPEND_WILDCARD_DEFAULT = False  # Wildcards don't work
MAX_RESULTS_PER_PREFIX = int(os.getenv("JEFF_MAX_RESULTS_PER_PREFIX", "2000"))
SNAPSHOT_ENABLE = os.getenv("JEFF_SNAPSHOT", "true").strip().lower() in ("1","true","yes")
SNAPSHOT_DIR = os.getenv("JEFF_SNAPSHOT_DIR", "debug/jefferson")
SNAPSHOT_OVERWRITE = os.getenv("JEFF_SNAPSHOT_OVERWRITE", "false").strip().lower() in ("1","true","yes")
SNAPSHOT_KEEP_PER_KIND = int(os.getenv("JEFF_MAX_SNAPSHOTS_PER_KIND", "20"))
SNAPSHOT_MAX_TOTAL = int(os.getenv("JEFF_MAX_SNAPSHOTS_TOTAL", "200"))

SEARCH_DELAY = float(os.getenv("JEFF_SEARCH_DELAY_SEC", "1"))
JEFF_FORCE_RUN = os.getenv("JEFF_FORCE_RUN", "").strip().lower() in ("1", "true", "yes")
JEFF_NEW_ID_WINDOW = int(os.getenv("JEFF_NEW_ID_WINDOW", "200"))
JEFF_NEW_ID_MISS_LIMIT = int(os.getenv("JEFF_NEW_ID_MISS_LIMIT", "10"))
STATE_DOC_ID = "jefferson_scrape_state"

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


def _load_surnames_from_mongo(db) -> List[str]:
    if db is None:
        return []
    try:
        coll = db["jefferson_events"]
    except Exception:
        return []
    try:
        names = coll.distinct("last_name")
    except Exception:
        return []
    cleaned = {str(n).strip().upper() for n in names if isinstance(n, str) and n.strip()}
    return sorted(cleaned)


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
    s = s.strip().upper()
    if s in {"NO BOND", "N/A", "NA", "NONE", "NO", "N\u00A0A"}:
        return 0.0
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


def _parse_detail_id(url: str | None) -> Optional[int]:
    if not url:
        return None
    m = re.search(r"/(?:Detail|Details)/(\d+)", url)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _extract_last_updated(html: str) -> Optional[str]:
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    marker = soup.find(string=re.compile(r"Last updated", re.I))
    if not marker:
        return None
    parent = marker.parent if hasattr(marker, "parent") else None
    if parent:
        bold = parent.find("b") if hasattr(parent, "find") else None
        if bold and bold.get_text(strip=True):
            return bold.get_text(strip=True)
    if hasattr(marker, "next_element"):
        nxt = marker.next_element
        if nxt and hasattr(nxt, "strip"):
            text = nxt.strip()
            if text:
                return text
    return None


def _snapshot_detail_failure(detail_id: str, body: str) -> None:
    if not body:
        return
    try:
        _write_snapshot("detail_error", detail_id, body)
    except Exception:
        pass


def _is_no_results_page(html: str) -> bool:
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    header = soup.find("h3", class_=re.compile(r"red-text", re.I))
    if header and "No results found" in header.get_text(strip=True):
        return True
    title = soup.find("title")
    if title and "No results" in title.get_text(strip=True):
        return True
    return False

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
    """Extract key-value pairs from detail page with multiple fallbacks."""
    out: Dict[str, str] = {}

    # Primary: paired divs
    title_divs = soup.select("div.detail-property-title")
    for title_div in title_divs:
        title = _clean_txt(title_div.get_text()).rstrip(":").strip()
        if not title:
            continue
        value_div = title_div.find_next_sibling("div", class_="detail-property-value")
        if value_div:
            value = _clean_txt(value_div.get_text())
            out[title] = value

    # Secondary: definition lists (dl/dt/dd)
    if not out:
        for dt_tag in soup.select("dl dt"):
            title = _clean_txt(dt_tag.get_text()).rstrip(":")
            dd = dt_tag.find_next_sibling("dd")
            if dd:
                out[title] = _clean_txt(dd.get_text())

    # Normalize keys to canonical map (case-insensitive)
    norm: Dict[str, str] = {}
    keymap = {
        "jail entry time": "Jail Entry Time",
        "arrest date": "Arrest Date",
        "arresting agency": "Arresting Agency",
        "age at arrest": "Age at Arrest",
        "race": "Race",
        "gender": "Gender",
        "sex": "Gender",
    }
    for k, v in out.items():
        lk = k.lower()
        if lk in keymap:
            norm[keymap[lk]] = v
        else:
            norm[k] = v
    return norm

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
    has_property_divs = len(soup.select("div.detail-property-title")) > 0
    has_h1_after_form = False
    search_form = soup.select_one("div#inmate-search-form")
    if search_form:
        for h1 in soup.select("h1"):
            if search_form in h1.parents:
                continue
            text = h1.get_text(strip=True)
            if text and text != "Inmate Search":
                has_h1_after_form = True
                break
    # Loosen: accept if either property divs or results-table, and h1 after form
    return (has_property_divs or soup.select_one("table#results-table")) and has_h1_after_form

# ------- main scraper -------
class JeffersonJailScraper(AuditedScraper):
    name = "jefferson_jail"

    def __init__(self, db):
        super().__init__(db, "Jefferson")
        self._sess = requests.Session()
        self._sess.headers.update(UA)
        self._sess.headers["Referer"] = SEARCH_FORM_URL
        
        # Initialize session
        try:
            init_response = self._sess.get(SEARCH_FORM_URL, timeout=REQ_TIMEOUT)
            print(f"[jeff] Session initialized, status: {init_response.status_code}")
        except Exception as e:
            print(f"[jeff] Warning: Could not initialize session: {e}")
        self._antiforgery = _discover_antiforgery(self._sess)
        if not (self._antiforgery.get("form") or self._antiforgery.get("header")):
            print("[jeff] Warning: could not locate any antiforgery token; searches may fail")

    def _search(self, last_prefix: str, first_prefix: Optional[str], append_wildcard: bool) -> str:
        """
        Submit the Jefferson search using GET with the real form fields.
        The form snapshot shows METHOD=GET, ACTION=/Search/List, and fields lastName/firstName.
        We allow last-only queries (first is optional).
        """
        ln = (last_prefix or "").strip()
        fn = (first_prefix or "").strip() if first_prefix else ""

        if not ln:
            raise ValueError("Last-name prefix is required for Jefferson search")

        # Build query params exactly as the live form expects
        params: Dict[str, str] = {"lastName": ln}
        if fn:
            params["firstName"] = fn

        headers = {
            "Referer": SEARCH_FORM_URL,
            "Origin": "https://jeffersoncountytx.gov",
        }

        # GET to the list endpoint (per snapshot: METHOD=GET)
        r = self._sess.get(SEARCH_LIST_URL, params=params, headers=headers, timeout=REQ_TIMEOUT)
        print(f"[jeff] Search GET {SEARCH_LIST_URL} | last={ln} first={fn} | status={r.status_code} len={len(r.text)}")
        r.raise_for_status()
        return r.text

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
        url_match = re.search(r"/Detail[s]?/(\d+)", url, re.I)
        if url_match:
            ext_id = f"jefferson:{url_match.group(1)}"

        # Calculate booking age category using parent class method
        booking_date_iso = _iso_date_guess(jail_entry_time) or _iso_date_guess(properties.get("Arrest Date"))

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
            "_collection": "jefferson_events",
            "person_id": None,
            "county": "Jefferson",
            "facility": "Jefferson County Jail",
            "full_name": name.upper(),
            "first_name": first,
            "last_name": last,
            "booking_number": None,
            "status": "In Custody",
            "booked_at": booking_date_iso,
            "released_at": None,
            "source_url": url,
            "charges": charges,
            "bonds": [],
            "total_bond": total_bond_amount if total_bond_amount > 0 else None,
            "agency": arresting_agency,
            "arrest_date": _iso_date_guess(jail_entry_time),
            "race": race,
            "sex": gender,
            "age": age_at_arrest,
        }

        # Add standardized booking age fields using parent class method
        event = self._enhance_event(event, booking_date_iso)

        return {"person": person, "event": event}

    def fetch(self, *, letters: str = "", first_letters: str = "", append_wildcard: Optional[bool] = None) -> Iterable[Dict[str, Any]]:
        """Fetch inmates from Jefferson County jail using Mongo-derived surnames."""

        letters_spec = os.getenv("JEFF_LETTERS", letters)
        first_letters_spec = os.getenv("JEFF_FIRST_LETTERS", first_letters)
        append_wildcard = False

        print(f"[jeff] START: letters={letters_spec or '(all)'} first_letters={first_letters_spec or '(auto)'}")

        self._audit_start(
            letters_spec=letters_spec,
            first_letters_spec=first_letters_spec or "",
            append_wildcard=append_wildcard
        )

        state_coll = None
        state_doc: Dict[str, Any] = {}
        last_seen_id = 0
        last_seen_stamp: Optional[str] = None
        if self.db is not None:
            try:
                state_coll = self.db["ingest_state"]
                state_doc = state_coll.find_one({"_id": STATE_DOC_ID}) or {}
                last_seen_id = int(state_doc.get("max_detail_id") or 0)
                last_seen_stamp = state_doc.get("last_updated")
            except Exception as exc:
                print(f"[jeff] Warning: could not load ingest state: {exc}")
                state_coll = None

        current_last_updated: Optional[str] = None
        try:
            form_resp = self._sess.get(SEARCH_FORM_URL, timeout=REQ_TIMEOUT)
            form_resp.raise_for_status()
            current_last_updated = _extract_last_updated(form_resp.text)
        except Exception as exc:
            print(f"[jeff] Warning: could not fetch search form for timestamp: {exc}")

        surnames = _load_surnames_from_mongo(self.db)
        if not surnames:
            surnames = load_surnames(SURNAME_FILE)
            if surnames:
                print(f"[jeff] Using {len(surnames)} surnames from {SURNAME_FILE}")
        else:
            print(f"[jeff] Loaded {len(surnames)} surnames from Mongo")

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

        allowed_prefixes = expand_range(letters_spec)
        if allowed_prefixes:
            allowed_prefixes = sorted(set(allowed_prefixes))
            surnames = [s for s in surnames if any(s.startswith(pref) for pref in allowed_prefixes)]

        if not surnames:
            print("[jeff] No surnames available; nothing to do.")
            self._audit_finish(summary_seen_links=0)
            return

        alphabet = [chr(c) for c in range(ord('A'), ord('Z') + 1)]
        split_first_prefixes = expand_range(first_letters_spec)
        if not split_first_prefixes:
            split_first_prefixes = alphabet

        max_detail_id = last_seen_id
        total_seen = 0
        events_yielded = 0

        visited_urls: set[str] = set()
        visited_lock = threading.Lock()

        def update_max_from_url(url: str):
            nonlocal max_detail_id
            detail_id = _parse_detail_id(url)
            if detail_id and detail_id > max_detail_id:
                max_detail_id = detail_id

        def handle_detail(url: str) -> Optional[Dict[str, Any]]:
            nonlocal events_yielded
            with visited_lock:
                if url in visited_urls:
                    return None
                visited_urls.add(url)
            detail_id = _parse_detail_id(url) or "unknown"
            try:
                r = self._sess.get(url, timeout=REQ_TIMEOUT)
                if r.status_code == 404:
                    _snapshot_detail_failure(str(detail_id), r.text)
                    return None
                r.raise_for_status()
                detail_html = r.text
                if _is_no_results_page(detail_html):
                    return None
                if not _looks_like_inmate_detail(detail_html):
                    self._audit_inc("errors", 1)
                    _snapshot_detail_failure(str(detail_id), detail_html)
                    return None
                rec = self._parse_detail(detail_html, r.url)
                if not rec:
                    self._audit_inc("errors", 1)
                    _snapshot_detail_failure(str(detail_id), detail_html)
                    return None

                person = rec["person"]
                event = rec["event"]
                booking_category = event.get("booking_age_category", "unknown")
                self._audit_success(person.get("full_name", "UNKNOWN"), booking_category)

                res = self.upsert_person(person)
                if res.get("inserted"):
                    self._audit_inc("upserts_person_inserted", 1)
                else:
                    self._audit_inc("upserts_person_updated", 1)

                pid = res.get("_id")
                event["person_id"] = pid
                self._audit_inc("details_parsed_ok", 1)
                self._audit_inc("events_yielded", 1)
                update_max_from_url(r.url)
                events_yielded += 1
                return event
            except Exception as exc:
                self._audit_inc("errors", 1)
                print(f"[jeff] Detail fetch error for {url}: {exc}")
                body = ""
                if hasattr(exc, "response") and getattr(exc.response, "text", None):
                    body = exc.response.text
                else:
                    try:
                        body = self._sess.get(url, timeout=REQ_TIMEOUT).text
                    except Exception:
                        body = ""
                _snapshot_detail_failure(str(detail_id), body)
                return None

        def process_links(last_name: str, first_prefix: Optional[str], links: List[str]) -> Iterable[Dict[str, Any]]:
            nonlocal total_seen
            if not links:
                return
            print(f"[jeff] {last_name} {first_prefix or ''} → {len(links)} links")
            total_seen += len(links)

            from concurrent.futures import ThreadPoolExecutor, as_completed
            max_workers = int(os.getenv("JEFF_DETAIL_CONCURRENCY", "8"))

            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = [ex.submit(handle_detail, url) for url in links]
                for fut in as_completed(futures):
                    ev = fut.result()
                    if ev is not None:
                        yield ev

            if ROW_DELAY > 0:
                time.sleep(ROW_DELAY)

        def run_query(last_name: str, first_prefix: Optional[str]) -> Optional[str]:
            try:
                return self._search(last_name, first_prefix or None, append_wildcard)
            except Exception:
                return None

        def perform_search(last_name: str, first_prefix: Optional[str]) -> Optional[str]:
            self._audit_inc("prefixes_scanned", 1)
            html = run_query(last_name, first_prefix)
            if html is None:
                print(f"[jeff] Failed search for last={last_name} first={first_prefix or ''}")
                self._audit_inc("errors", 1)
                return None
            _write_snapshot("search", f"{last_name}_{first_prefix or 'NONE'}", html)
            return html

        def scan_last_name(last_name: str) -> Iterable[Dict[str, Any]]:
            html = perform_search(last_name, None)
            if html is None or "No results found" in html:
                return

            links = _extract_detail_links(html)
            self._audit_inc("detail_links_found", len(links))
            too_many = bool(re.search(r"too many|narrow your search", html, re.I))

            if links and not too_many and len(links) <= MAX_RESULTS_PER_PREFIX:
                for ev in process_links(last_name, None, links):
                    yield ev
                return

            for fp in split_first_prefixes:
                html_fp = perform_search(last_name, fp)
                if html_fp is None or "No results found" in html_fp:
                    continue
                links_fp = _extract_detail_links(html_fp)
                self._audit_inc("detail_links_found", len(links_fp))
                if links_fp:
                    for ev in process_links(last_name, fp, links_fp):
                        yield ev

        def probe_new_ids(start_id: int) -> Iterable[Dict[str, Any]]:
            nonlocal total_seen
            misses = 0
            current = max(start_id + 1, 1)
            upper = current + JEFF_NEW_ID_WINDOW - 1
            while current <= upper and misses < JEFF_NEW_ID_MISS_LIMIT:
                url = f"{BASE}/Search/Detail/{current}"
                ev = handle_detail(url)
                total_seen += 1
                if ev is not None:
                    misses = 0
                    yield ev
                else:
                    misses += 1
                current += 1
                if SEARCH_DELAY > 0:
                    time.sleep(min(SEARCH_DELAY, 0.5))

        for ev in probe_new_ids(last_seen_id):
            yield ev

        if not JEFF_FORCE_RUN and current_last_updated and last_seen_stamp and current_last_updated == last_seen_stamp and events_yielded == 0:
            print(f"[jeff] Last updated unchanged ({current_last_updated}); skipping surname sweep.")
        else:
            for surname in surnames:
                for ev in scan_last_name(surname):
                    yield ev

        self._audit_finish(summary_seen_links=total_seen, last_updated=current_last_updated or "unknown")

        if state_coll is not None:
            try:
                state_coll.update_one(
                    {"_id": STATE_DOC_ID},
                    {
                        "$set": {
                            "max_detail_id": max_detail_id,
                            "last_updated": current_last_updated,
                            "updated_at": datetime.utcnow().isoformat() + "Z",
                        }
                    },
                    upsert=True,
                )
            except Exception as exc:
                print(f"[jeff] Warning: could not update ingest state: {exc}")

        print(f"[jeff] COMPLETED: {total_seen} detail pages processed")
