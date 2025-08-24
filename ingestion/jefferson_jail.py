# ingestion/jefferson_jail.py
from __future__ import annotations

import os, time, re, itertools, uuid
import requests
from typing import Any, Dict, Iterable, List, Optional, Tuple
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from pathlib import Path

from .base_scraper import BaseScraper

BASE = "https://jeffersoncountytx.gov/InmateSearch"
SEARCH_URL = f"{BASE}/Search/List"

UA = {
    "User-Agent": "Mozilla/5.0 (compatible; WarrantDB/0.3)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# --- env knobs ---
ROW_DELAY = float(os.getenv("JEFF_ROW_DELAY_SEC", "0.6"))
REQ_TIMEOUT = int(os.getenv("JEFF_REQ_TIMEOUT", "30"))
APPEND_WILDCARD_DEFAULT = (os.getenv("JEFF_APPEND_WILDCARD", "false").strip().lower() in ("1","true","yes"))
MAX_RESULTS_PER_PREFIX = int(os.getenv("JEFF_MAX_RESULTS_PER_PREFIX", "2000"))
AUDIT_ENABLE = os.getenv("SCRAPER_AUDIT", "true").strip().lower() in ("1","true","yes")
SNAPSHOT_ENABLE = os.getenv("JEFF_SNAPSHOT", "true").strip().lower() in ("1","true","yes")
SNAPSHOT_DIR = os.getenv("JEFF_SNAPSHOT_DIR", "debug/jefferson")
SNAPSHOT_OVERWRITE = os.getenv("JEFF_SNAPSHOT_OVERWRITE", "false").strip().lower() in ("1","true","yes")
SNAPSHOT_KEEP_PER_KIND = int(os.getenv("JEFF_MAX_SNAPSHOTS_PER_KIND", "20"))
SNAPSHOT_MAX_TOTAL     = int(os.getenv("JEFF_MAX_SNAPSHOTS_TOTAL", "200"))
SEARCH_DELAY = float(os.getenv("JEFF_SEARCH_DELAY_SEC", "0"))

# ------- helpers -------
def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass

def _sanitize_name(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", (s or "")).strip("_")[:160]

def _prune_kind(kind: str) -> None:
    """Keep only the most recent SNAPSHOT_KEEP_PER_KIND files for a kind."""
    if not SNAPSHOT_ENABLE:
        return
    p = Path(SNAPSHOT_DIR)
    files = sorted((f for f in p.glob(f"{kind}_*.html") if f.is_file()),
                   key=lambda f: f.stat().st_mtime)
    excess = max(0, len(files) - max(0, SNAPSHOT_KEEP_PER_KIND))
    for f in files[:excess]:
        try:
            f.unlink()
        except Exception:
            pass

def _prune_global() -> None:
    """Keep overall snapshot count under SNAPSHOT_MAX_TOTAL by deleting oldest first."""
    if not SNAPSHOT_ENABLE:
        return
    p = Path(SNAPSHOT_DIR)
    files = sorted((f for f in p.glob("*.html") if f.is_file()),
                   key=lambda f: f.stat().st_mtime)
    excess = max(0, len(files) - max(0, SNAPSHOT_MAX_TOTAL))
    for f in files[:excess]:
        try:
            f.unlink()
        except Exception:
            pass

def _write_snapshot(kind: str, name: str, html: str) -> None:
    """
    Write a snapshot with either:
      - overwrite mode: {kind}_latest.html (single rotating file per kind), or
      - rolling mode:   {kind}_{timestamp}_{name}.html and prune extras.
    """
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
        pass  # never crash on snapshots

def _money_to_float(s: str | None) -> Optional[float]:
    if not s:
        return None
    s = s.replace(",", "")
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
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
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

def _kv_from_label_blocks(soup: BeautifulSoup) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for dl in soup.select("dl"):
        dts = dl.select("dt")
        dds = dl.select("dd")
        for dt, dd in itertools.zip_longest(dts, dds, fillvalue=None):
            lab = _clean_txt(dt.get_text()) if dt else ""
            val = _clean_txt(dd.get_text()) if dd else ""
            if lab:
                out[lab] = val
    for tbl in soup.select("table"):
        for tr in tbl.select("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if th and td:
                lab = _clean_txt(th.get_text())
                val = _clean_txt(td.get_text())
                if lab:
                    out[lab] = val
    for r in soup.select(".row"):
        cols = r.find_all(recursive=False)
        if len(cols) >= 2:
            lab = _clean_txt(cols[0].get_text())
            val = _clean_txt(cols[1].get_text())
            if lab:
                out[lab] = val
    return out

def _extract_detail_links(list_html: str) -> List[str]:
    """
    Extract ONLY inmate detail links from the search results.
      Accept: /InmateSearch/Search/Details/... or /InmateSearch/Details...
      Ignore: site chrome (e.g., /Sheriff, /InmateSearch/).
    """
    soup = BeautifulSoup(list_html or "", "lxml")
    links: List[str] = []

    def _abs(u: str) -> str:
        return urljoin(BASE + "/", u)

    def _is_detail(href: str) -> bool:
        if not href:
            return False
        h = href.lower()
        return bool(re.search(r"/inmatesearch/(search/)?detail(s)?(?:/|\?|$)", h))

    def _is_obvious_non_detail(href: str) -> bool:
        if not href:
            return True
        h = href.lower()
        if h.endswith("/sheriff") or "/sheriff/" in h:
            return True
        if h.rstrip("/") in ("/inmatesearch", "/"):
            return True
        return False

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if _is_detail(href):
            links.append(_abs(href))

    for row in soup.select(".clickable-row[data-href]"):
        dh = (row.get("data-href") or "").strip()
        if _is_detail(dh):
            links.append(_abs(dh))

    for el in soup.select("[onclick]"):
        oc = el.get("onclick", "")
        m = re.search(r"location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]", oc, re.I)
        if m:
            cand = m.group(1).strip()
            if _is_detail(cand):
                links.append(_abs(cand))

    out: List[str] = []
    seen = set()
    for u in links:
        if _is_obvious_non_detail(u):
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def _extract_charges(soup: BeautifulSoup) -> List[Dict[str, str]]:
    charges: List[Dict[str, str]] = []
    for tbl in soup.select("table"):
        heads = [th.get_text(strip=True).lower() for th in tbl.select("thead th")] or \
                [th.get_text(strip=True).lower() for th in tbl.find_all("th")]
        if not heads or not any("charge" in h for h in heads):
            continue
        idx = {
            "charge": next((i for i,h in enumerate(heads) if "charge" in h), None),
            "status": next((i for i,h in enumerate(heads) if "status" in h), None),
            "docket": next((i for i,h in enumerate(heads) if "docket" in h or "case" in h), None),
            "bond":   next((i for i,h in enumerate(heads) if "bond" in h), None),
        }
        for tr in tbl.select("tbody tr") or tbl.select("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            def cell(i): return tds[i].get_text(strip=True) if (i is not None and i < len(tds)) else ""
            charges.append({
                "charge": cell(idx["charge"]),
                "status": cell(idx["status"]),
                "docket": cell(idx["docket"]),
                "bond":   cell(idx["bond"]),
            })
        if charges:
            return charges
    for li in soup.select("li"):
        t = li.get_text(" ", strip=True)
        if re.search(r"charge", t, re.I):
            charges.append({"charge": t, "status": "", "docket": "", "bond": ""})
    return charges

def _looks_like_inmate_detail(html: str) -> bool:
    """Heuristic: a real detail page should have a person-ish name or common labels."""
    soup = BeautifulSoup(html or "", "lxml")
    nameish = soup.select_one("h1, h2, .inmate-name, #inmate-name, [aria-level='1']")
    labels  = soup.find(string=re.compile(r"(DOB|Date of Birth|Booking|Booked|Arrest)", re.I))
    charges = soup.find(string=re.compile(r"charge", re.I))
    return bool(nameish or labels or charges)

# ------- main scraper -------
class JeffersonJailScraper(BaseScraper):
    name = "jefferson_jail"

    def __init__(self, db):
        super().__init__(db)
        self._sess = requests.Session()
        self._sess.headers.update(UA)

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

    # ---------- HTTP ----------
    def _search(self, last_prefix: str, first_prefix: Optional[str], append_wildcard: bool) -> str:
        ln = last_prefix
        fn = first_prefix or ""
        if append_wildcard:
            ln = f"{ln}*"
            if fn:
                fn = f"{fn}*"
        params = {"lastName": ln}
        if fn:
            params["firstName"] = fn
        r = self._sess.get(SEARCH_URL, params=params, timeout=REQ_TIMEOUT)
        r.raise_for_status()
        return r.text

    # ---------- parsing ----------
    def _parse_detail(self, html: str, url: str) -> Optional[Dict[str, Any]]:
        soup = BeautifulSoup(html or "", "lxml")
        name = ""
        cand = soup.select_one("h1, h2, .inmate-name, #inmate-name, [aria-level='1']")
        if cand:
            name = cand.get_text(strip=True)
        if not name:
            header = soup.find(["h1","h2"])
            name = header.get_text(strip=True) if header else ""
        kv = _kv_from_label_blocks(soup)
        full_name = _pick(name, kv.get("Name"), kv.get("Inmate Name"))
        if not full_name:
            return None

        dob = _pick(kv.get("DOB"), kv.get("Date of Birth"), kv.get("Birth Date"))
        booking_no = _pick(kv.get("Booking #"), kv.get("Booking Number"), kv.get("Book #"), kv.get("Book Number"))
        booked = _pick(kv.get("Booking Date"), kv.get("Booked"), kv.get("Arrest Date"), kv.get("Arrested"))
        total_bond = _pick(kv.get("Total Bond"), kv.get("Bond Total"), kv.get("Bond Amount"))
        mug = None
        img = soup.select_one("img[src*='mug'], img#imgMugshot, img[alt*='mug'], .mugshot img")
        if img and img.get("src"):
            src = img["src"].strip()
            mug = src if src.startswith("http") else urljoin(BASE + "/", src.lstrip("/"))

        charges = _extract_charges(soup)

        first, last = _split_name(full_name)
        person = {
            "_ext_id": _pick(booking_no, f"jefferson:{(full_name or '').upper()}|{_iso_date_guess(dob) or ''}|{_iso_date_guess(booked) or ''}"),
            "full_name": (full_name or "").upper(),
            "aka": [],
            "dob": _iso_date_guess(dob),
            "identifiers": {"booking": ([booking_no] if booking_no else [])},
            "contact": {},
            "media": ([{"rel": "mugshot", "url": mug}] if mug else []),
            "links": [{"rel": "jefferson_detail", "url": url}],
        }

        event = {
            "_collection": "custody_events",
            "person_id": None,
            "county": "Jefferson",
            "facility": "Jefferson County Jail",
            "booking_number": booking_no or None,
            "status": "In Custody",
            "booked_at": _iso_date_guess(booked),
            "released_at": None,
            "source_url": url,
            "scraped_at": datetime.utcnow().isoformat() + "Z",
            "charges": charges,
            "bonds": [],
            "total_bond": _money_to_float(total_bond),
            "agency": _pick(kv.get("Arresting Agency"), kv.get("Agency")),
            "arrest_date": _iso_date_guess(_pick(kv.get("Arrest Date"), kv.get("Arrested"))),
            "race": kv.get("Race"),
            "sex": kv.get("Sex"),
            "age": kv.get("Age"),
        }
        return {"person": person, "event": event}

    # ---------- main entry ----------
    def fetch(self, *, letters: str = "SMI-SMZ", first_letters: str = "", append_wildcard: Optional[bool] = None) -> Iterable[Dict[str, Any]]:
        """
        Sweep last-name prefixes (and optional first-initials).
        Env overrides: JEFF_LETTERS, JEFF_FIRST_LETTERS, JEFF_APPEND_WILDCARD
        """
        letters = os.getenv("JEFF_LETTERS", letters)
        first_letters = os.getenv("JEFF_FIRST_LETTERS", first_letters)
        if append_wildcard is None:
            append_wildcard = APPEND_WILDCARD_DEFAULT
        if os.getenv("JEFF_APPEND_WILDCARD"):
            append_wildcard = os.getenv("JEFF_APPEND_WILDCARD","false").strip().lower() in ("1","true","yes")

        print(f"[jeff] START: letters={letters} first_letters={first_letters or '(none)'} (append_wildcard={append_wildcard})")
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
        for lp in last_prefixes:
            print(f"[jeff] last-prefix = {lp}  (first-initials={len(first_prefixes)})")
            for fp in first_prefixes:
                self._audit_inc("prefixes_scanned", 1)
                try:
                    html = self._search(lp, fp or None, append_wildcard)
                except Exception as e:
                    print(f"[jeff] WARN search error for last={lp} first={fp or ''}: {e}")
                    self._audit_inc("errors", 1)
                    self._audit_emit("warn", {"prefix": {"last": lp, "first": fp or ""}, "msg": f"search error: {e}"})
                    if SEARCH_DELAY > 0:
                        time.sleep(SEARCH_DELAY)
                    continue

                _write_snapshot("search", f"{lp}_{fp or 'NONE'}", html)

                if re.search(r"too many|narrow your search|exceeded", html, re.I):
                    msg = f"too broad; skipping (last={lp}, first={fp or ''})"
                    print(f"[jeff] NOTE: {msg}")
                    self._audit_note(msg)
                    if SEARCH_DELAY > 0:
                        time.sleep(SEARCH_DELAY)
                    continue

                links = _extract_detail_links(html)
                self._audit_inc("detail_links_found", len(links))

                if len(links) > MAX_RESULTS_PER_PREFIX:
                    msg = f"{len(links)} links > cap {MAX_RESULTS_PER_PREFIX}; skipping prefix last={lp}, first={fp or ''}"
                    print(f"[jeff] NOTE: {msg}")
                    self._audit_note(msg)
                    if SEARCH_DELAY > 0:
                        time.sleep(SEARCH_DELAY)
                    continue

                print(f"[jeff] last={lp} first={fp or ''} → {len(links)} detail link(s)")
                total_seen += len(links)
                self._audit_emit("prefix", {"prefix": {"last": lp, "first": fp or ""}, "links": len(links)})

                for url in links:
                    if "/InmateSearch/" not in url:
                        print(f"[jeff] skip non-inmate link → {url}")
                        self._audit_note(f"skip non-inmate link: {url}")
                        continue
                    try:
                        r = self._sess.get(url, timeout=REQ_TIMEOUT)
                        r.raise_for_status()
                        detail_html = r.text

                        if "/Sheriff" in r.url and "/InmateSearch/" not in r.url:
                            _write_snapshot("detail_fail", re.sub(r"[^a-zA-Z0-9]+", "_", r.url[-80:]), detail_html)
                            print(f"[jeff] redirected to non-detail → {r.url} (skipping)")
                            self._audit_inc("errors", 1)
                            continue

                        if not _looks_like_inmate_detail(detail_html):
                            _write_snapshot("detail_fail", re.sub(r"[^a-zA-Z0-9]+", "_", r.url[-80:]), detail_html)
                            print(f"[jeff] not a detail page → {r.url} (snapshot saved, skipping)")
                            self._audit_inc("errors", 1)
                            continue

                        rec = self._parse_detail(detail_html, r.url)
                        if not rec:
                            _write_snapshot("detail_fail", re.sub(r"[^a-zA-Z0-9]+", "_", r.url[-80:]), detail_html)
                            self._audit_inc("errors", 1)
                            continue
                    except Exception as e:
                        print(f"[jeff] WARN detail fetch error: {e}")
                        self._audit_inc("errors", 1)
                        continue

                    person = rec["person"]
                    event  = rec["event"]

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

                    if ROW_DELAY > 0:
                        time.sleep(ROW_DELAY)

                # throttle between prefix searches
                if SEARCH_DELAY > 0:
                    time.sleep(SEARCH_DELAY)

        print(f"[jeff] DONE. total detail pages discovered = {total_seen}")
        self._audit_emit("done", {
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "summary_seen_links": total_seen
        })