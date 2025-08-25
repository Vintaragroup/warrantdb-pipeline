# ingestion/galveston_p2c_fast.py
from __future__ import annotations

import asyncio
import base64
import json
import os
import re

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import (
    urlparse, urlsplit, urlunparse, urlunsplit,
    parse_qs, parse_qsl, urlencode
)
from hashlib import sha1 as _sha1
from pathlib import Path

import certifi
import httpx
from bs4 import BeautifulSoup
from ingestion.audited_scraper import AuditedScraper

try:
    from gridfs import GridFS  # only used if MUGSHOT_SAVE=gridfs
except Exception:  # pragma: no cover
    GridFS = None  # type: ignore

BASE = "https://p2c.galvestoncountytx.gov"
ROSTER_HTML = f"{BASE}/jailinmates.aspx"
BASE_DOMAIN = urlparse(BASE).hostname or "p2c.galvestoncountytx.gov"
UA = {"User-Agent": "Mozilla/5.0 (compatible; WarrantDB/0.2)"}
TIMEOUT = 30.0

# ---------- Env helpers ----------
def _verify() -> Any:
    v = os.getenv("SCRAPER_VERIFY_SSL", "false").strip().lower() in ("1", "true", "yes")
    return certifi.where() if v else False

def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name, "true" if default else "false").strip().lower()
    return val in ("1", "true", "yes", "y")

def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

# Updated configuration for better performance and control
TEST_MAX_LINKS = _int("TEST_MAX_LINKS", 0)  # 0 means no limit
SKIP_MUGSHOTS = _bool("SKIP_MUGSHOTS", True)
MUGSHOT_SAVE_MODE = os.getenv("MUGSHOT_SAVE", "link").strip().lower()  # link|bytes|gridfs
CONCURRENCY = _int("GALV_CONCURRENCY", 10)  # Reduced from 20
ROWS_MAX = _int("ROWS_MAX", 5000)
ROW_DELAY = float(os.getenv("GALV_ROW_DELAY_SEC", "0.5"))  # Add delay between requests

# Controlled snapshot system (default OFF)
SNAPSHOT_ENABLE = os.getenv("GALV_SNAPSHOT", "false").strip().lower() in ("1","true","yes")
SNAPSHOT_DIR = os.getenv("GALV_SNAPSHOT_DIR", "debug/galveston")
SNAPSHOT_MAX_TOTAL = int(os.getenv("GALV_MAX_SNAPSHOTS_TOTAL", "50"))

_BAD_NAME_RE = re.compile(
    r"(HOME|DAILY\s+BULLETIN|INMATE\s+INQUIRY|ARRESTS|CRASH\s+REPORTS|WANTED)",
    re.I,
)

# ---------- Helper functions ----------
def _money_to_float(s: str) -> Optional[float]:
    s = (s or "").replace(",", "")
    m = re.search(r"\$?\s*([0-9]+(?:\.\d{2})?)", s)
    return float(m.group(1)) if m else None

def _rid_from_url(u: str) -> Optional[str]:
    """Extract a stable row-id from a detail URL if present."""
    try:
        qs = parse_qs(urlparse(u).query, keep_blank_values=True)
        qs = {k.lower(): v for k, v in qs.items()}
        for k in ("rid", "rowid", "row_id", "id", "navid"):
            vals = qs.get(k)
            if vals and vals[0]:
                return vals[0].strip()
    except Exception:
        pass
    return None

def _normalize_detail_url(u: str) -> str:
    """Normalize inmate detail URLs so we don't treat different navids as different pages."""
    try:
        s = urlsplit(u)
        host = (s.netloc or "").lower()
        path = (s.path or "").lower()
        q = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True) if k.lower() != "navid"]
        return urlunsplit((s.scheme, host, path, urlencode(q), ""))
    except Exception:
        return u

def _iso_date_guess(s: str | None) -> Optional[str]:
    """Parse various date formats to ISO date string."""
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

def _write_controlled_snapshot(kind: str, name: str, html: str) -> None:
    """Controlled snapshot writing."""
    if not SNAPSHOT_ENABLE:
        return
    
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        
        # Sanitize filename
        name = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)[:50]
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        
        filename = Path(SNAPSHOT_DIR) / f"{kind}_{stamp}_{name}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        
        print(f"[galv] snapshot → {filename}")
        
        # Prune old snapshots
        _prune_snapshots()
    except Exception:
        pass  # Never crash on snapshots

def _prune_snapshots() -> None:
    """Keep snapshot count under control."""
    try:
        p = Path(SNAPSHOT_DIR)
        if not p.exists():
            return
            
        files = sorted(p.glob("*.html"), key=lambda f: f.stat().st_mtime)
        excess = max(0, len(files) - SNAPSHOT_MAX_TOTAL)
        
        for f in files[:excess]:
            f.unlink()
    except Exception:
        pass

@dataclass
class SniffedRoster:
    url: str
    method: str
    headers: Dict[str, str]
    post_data: Optional[str]
    cookies: List[Dict[str, Any]]

# ---------- Playwright sniff (short, one-shot) ----------
def _playwright_sniff_roster() -> Optional[SniffedRoster]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

    observed: Dict[str, Any] = {}
    cookies: List[Dict[str, Any]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            ignore_https_errors=(_verify() is False),
            user_agent=UA["User-Agent"],  # type: ignore
        )
        page = context.new_page()

        def on_response(resp):
            try:
                url = resp.url
                if "jqHandler.ashx" in url:
                    req = resp.request
                    observed["url"] = url
                    observed["method"] = req.method
                    try:
                        observed["post_data"] = req.post_data
                    except Exception:
                        observed["post_data"] = None
                    observed["headers"] = dict(req.headers)
            except Exception:
                pass

        context.on("response", on_response)

        try:
            page.goto(f"{BASE}/main.aspx", wait_until="domcontentloaded", timeout=25_000)
        except Exception:
            pass

        if "jailinmates.aspx" not in (page.url or "").lower():
            for sel in [
                "a[href*='jailinmates.aspx']",
                "text=/jail inmates/i",
                "text=/inmate roster/i",
                "text=/inmates/i",
            ]:
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible():
                        loc.click(timeout=5_000, force=True, no_wait_after=True)
                        page.wait_for_load_state("domcontentloaded", timeout=10_000)
                        break
                except Exception:
                    pass

        try:
            page.goto(ROSTER_HTML, wait_until="domcontentloaded", timeout=25_000)
        except Exception:
            pass

        _set_show_all(page)

        try:
            resp = page.wait_for_response(
                lambda r: ("jqHandler.ashx" in r.url),
                timeout=10_000,
            )
            if resp:
                req = resp.request
                observed["url"] = resp.url
                observed["method"] = req.method
                try:
                    observed["post_data"] = req.post_data
                except Exception:
                    observed["post_data"] = None
                observed["headers"] = dict(req.headers)
        except Exception:
            pass

        try:
            cookies = context.cookies()
        except Exception:
            cookies = []

        context.close()
        browser.close()

    if not observed.get("url"):
        return None

    return SniffedRoster(
        url=observed["url"],
        method=observed.get("method", "GET"),
        headers=observed.get("headers", {}),
        post_data=observed.get("post_data"),
        cookies=cookies,
    )

def _cookies_for_httpx(cookies: List[Dict[str, Any]]) -> httpx.Cookies:
    jar = httpx.Cookies()
    for c in cookies or []:
        try:
            jar.set(c.get("name"), c.get("value"), domain=c.get("domain"), path=c.get("path", "/"))
        except Exception:
            continue
    return jar

def _bump_rows_in_payload(method: str, url: str, post_data: Optional[str]) -> Tuple[str, Optional[str]]:
    if method.upper() == "POST" and post_data:
        parts = post_data.split("&")
        new_parts = []
        had_rows = False
        for p in parts:
            if p.startswith("rows="):
                new_parts.append(f"rows={ROWS_MAX}")
                had_rows = True
            else:
                new_parts.append(p)
        if not had_rows:
            new_parts.append(f"rows={ROWS_MAX}")
        return url, "&".join(new_parts)
    else:
        if "rows=" in url:
            url = re.sub(r"rows=\d+", f"rows={ROWS_MAX}", url)
        if "rows=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}rows={ROWS_MAX}"
        return url, None

def _extract_detail_hrefs_from_roster_json(payload_text: str) -> List[str]:
    """Extract detail URLs from jqGrid JSON response."""
    try:
        data = json.loads(payload_text)
    except Exception:
        return []

    rows = data.get("rows") or []
    detail_urls: List[str] = []

    href_dq = re.compile(r'href="([^"]+)"', re.I)
    href_sq = re.compile(r"href='([^']+)'", re.I)
    onclick_href = re.compile(r"onclick\s*=\s*['\"][^(]*\(\s*['\"]([^'\"]+)['\"]\s*\)", re.I)

    def _abs(u: str) -> str:
        u = (u or "").strip()
        return u if u.startswith("http") else f"{BASE}/{u.lstrip('/')}"

    def _looks_like_detail(u: str) -> bool:
        u2 = u.lower()
        return ("inmate" in u2 and "detail" in u2) or ("detail" in u2 and u2.endswith(".aspx")) or ("inmates.aspx" in u2)

    for row in rows:
        fields: List[str] = []

        for c in (row.get("cell") or []):
            if isinstance(c, str):
                fields.append(c)

        for k, v in row.items():
            if isinstance(v, str):
                fields.append(v)

        for s in fields:
            if not s or not isinstance(s, str):
                continue
            for rgx in (href_dq, href_sq):
                m = rgx.search(s)
                if m:
                    href = _abs(m.group(1))
                    if _looks_like_detail(href) and href not in detail_urls:
                        detail_urls.append(href)
            m = onclick_href.search(s)
            if m:
                token = m.group(1).strip()
                if _looks_like_detail(token):
                    href = _abs(token)
                    if href not in detail_urls:
                        detail_urls.append(href)

    return detail_urls

def _set_show_all(page) -> None:
    """Force the jqGrid to show all rows."""
    try:
        page.wait_for_selector("div.ui-jqgrid", timeout=10_000)
        page.wait_for_selector("select.ui-pg-selbox", timeout=5_000)
        dd = page.locator("select.ui-pg-selbox").first

        try:
            dd.select_option(label="All")
        except Exception:
            opts = dd.locator("option")
            last_val = opts.last.get_attribute("value")
            if last_val:
                dd.select_option(value=last_val)

        page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return;
                el.dispatchEvent(new Event('change', { bubbles: true }));
                if (window.$) { try { $(el).change(); } catch(e){} }
            }""",
            "select.ui-pg-selbox",
        )

        page.wait_for_timeout(1500)

    except Exception:
        try:
            page.evaluate("""() => {
                if (window.$ && $('#tblII').length) {
                    $('#tblII').jqGrid('setGridParam', { rowNum: 9999 }).trigger('reloadGrid');
                }
            }""")
            page.wait_for_timeout(1500)
        except Exception:
            pass

async def _fetch_mugshot(session: httpx.AsyncClient, url: str) -> Optional[bytes]:
    try:
        r = await session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        ct = r.headers.get("content-type", "").lower()
        if "image" not in ct:
            return None
        return r.content
    except Exception:
        return None

# ---------- Main scraper ----------
class GalvestonP2CFastScraper(AuditedScraper):
    name = "galveston_p2c_fast"

    def __init__(self, db):
        super().__init__(db, "Galveston")
        self._cookiejar = httpx.Cookies()

    def _fetch_jqgrid_json(self) -> Optional[Dict[str, Any]]:
        sniff = _playwright_sniff_roster()
        if not sniff:
            return None

        self._cookiejar = _cookies_for_httpx(sniff.cookies)

        url, body = _bump_rows_in_payload(sniff.method, sniff.url, sniff.post_data)
        cookies = _cookies_for_httpx(sniff.cookies)

        try:
            with httpx.Client(headers=UA, verify=_verify(), cookies=cookies, timeout=TIMEOUT) as client:
                if sniff.method.upper() == "POST":
                    headers = {k: v for k, v in (sniff.headers or {}).items()}
                    headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
                    resp = client.post(url, content=(body or sniff.post_data or ""), headers=headers)
                else:
                    resp = client.get(url, headers=sniff.headers or {})
                resp.raise_for_status()
                data = resp.json()
                
                print(f"[galv] jqGrid JSON: {len(data.get('rows') or [])} rows")
                return data
        except Exception:
            return None

    def _render_roster_html(self) -> Optional[str]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return None
            
        html = None
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=(_verify() is False), user_agent=UA["User-Agent"])
            page = context.new_page()
            try:
                try:
                    page.goto(f"{BASE}/main.aspx", wait_until="domcontentloaded", timeout=25_000)
                except Exception:
                    pass
                    
                for sel in ["a[href*='jailinmates.aspx']", "text=/jail inmates/i", "text=/inmate roster/i", "text=/inmates/i"]:
                    try:
                        loc = page.locator(sel).first
                        if loc.is_visible():
                            loc.click(timeout=5_000, force=True, no_wait_after=True)
                            page.wait_for_load_state("domcontentloaded", timeout=10_000)
                            break
                    except Exception:
                        continue

                page.goto(ROSTER_HTML, wait_until="domcontentloaded", timeout=25_000)

                try:
                    page.wait_for_selector("select.ui-pg-selbox", timeout=10_000)
                    page.locator("select.ui-pg-selbox").first.select_option(label="All")
                    page.wait_for_timeout(1500)
                except Exception:
                    pass

                html = page.content()
                
                _write_controlled_snapshot("roster_html", "galveston_roster", html)
                
            except Exception:
                html = None
            context.close()
            browser.close()
        return html

    def _parse_roster_table(self, html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html or "", "lxml")

        target = soup.select_one("table#tblII")
        if target is None:
            tables = soup.select("table")
            if not tables:
                return []
            target = max(tables, key=lambda t: len(t.select("tr")) if t else 0)

        headers_raw = [th.get_text(strip=True) for th in target.select("thead th")]
        if not headers_raw:
            headers_raw = [th.get_text(strip=True) for th in target.select("th")]
        rows = target.select("tr")

        if not headers_raw and rows:
            first = rows[0].find_all(["td", "th"])
            headers_raw = [c.get_text(strip=True) for c in first]

        def hkey(k: str) -> str:
            k = k.strip().lower().replace(":", "")
            if "first" in k and "name" in k: return "first_name"
            if "last"  in k and "name" in k: return "last_name"
            if k in ("name", "inmate name", "full name"): return "name"
            if "dob" in k or "date of birth" in k: return "dob"
            if "age" in k: return "age"
            if "race" in k: return "race"
            if "sex" in k or "gender" in k: return "sex"
            if "arrest" in k: return "arrest_date"
            if "agency" in k: return "agency"
            if "total bond" in k or ("bond" in k and "total" in k): return "total_bond"
            if "bond" in k and "amount" in k: return "bond_amount"
            if "book" in k: return "booking"
            return k

        headers = [hkey(h) for h in headers_raw]
        people: List[Dict[str, Any]] = []

        start_idx = 1 if (rows and rows[0].find_all("th")) else 0

        lead_num = re.compile(r"^\s*\d+\s+")
        paren_demog = re.compile(r"\(([^)]*)\)$")

        def parse_demog_from_name(name: str):
            name = name.strip()
            race = sex = age = None
            m = paren_demog.search(name)
            if m:
                tail = m.group(1)
                parts = [p.strip() for p in re.split(r"[/|]", tail) if p.strip()]
                if len(parts) >= 2:
                    race = parts[0] or None
                    sex  = parts[1] or None
                if len(parts) >= 3 and parts[2].isdigit():
                    age = parts[2]
                name = name[:m.start()].strip()
            name = lead_num.sub("", name).strip()
            return name, race, sex, age

        for tr in rows[start_idx:]:
            tds = tr.find_all("td")
            if not tds:
                continue
            vals = [td.get_text(strip=True) for td in tds]
            cols = max(len(headers), len(vals))
            row = {(headers[i] if i < len(headers) else f"col{i}"): (vals[i] if i < len(vals) else "") for i in range(cols)}

            first = (row.get("first_name") or "").strip()
            last  = (row.get("last_name")  or "").strip()
            name  = (row.get("name") or "").strip()
            if not name and (first or last):
                name = f"{last}, {first}".strip(", ")

            if name:
                name, r2, s2, a2 = parse_demog_from_name(name)
            else:
                name, r2, s2, a2 = "", None, None, None

            if not name or name.upper().startswith("HOME") or "INMATE INQUIRY" in name.upper():
                continue

            dob         = row.get("dob") or None
            age         = row.get("age") or a2
            race        = row.get("race") or r2
            sex         = row.get("sex") or s2
            arrest_date = row.get("arrest_date") or None
            agency      = row.get("agency") or None
            total_bond  = row.get("total_bond") or row.get("bond_amount") or None
            booking_no  = row.get("booking") or None

            if isinstance(total_bond, str):
                amt = _money_to_float(total_bond)
                total_bond = amt if amt is not None else total_bond

            people.append({
                "full_name": name.upper(),
                "dob": dob,
                "age": age,
                "race": race,
                "sex": sex,
                "arrest_date": arrest_date,
                "agency": agency,
                "booking_number": booking_no,
                "bond_amount": total_bond,
            })
        return people

    def _harvest_detail_links_via_playwright(self) -> List[Dict[str, Any]]:
        """Harvest inmate detail links + jqGrid row ids (rid)."""
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            print("[galv] Playwright not available for DOM harvest")
            return []

        import time
        max_rows = int(os.getenv("HARVEST_MAX_ROWS", "1200"))  # Updated default
        max_secs = int(os.getenv("HARVEST_MAX_SECONDS", "3600"))  # Updated default

        links: List[Dict[str, Any]] = []
        start = time.time()

        def _abs(u: str) -> str:
            u = (u or "").strip()
            return u if u.startswith("http") else f"{BASE}/{u.lstrip('/')}"

        def _accept(u: str) -> bool:
            lu = (u or "").lower()
            return ("inmatedetail.aspx" in lu) and ("navid=" in lu)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=(_verify() is False),
                                        user_agent=UA["User-Agent"])
            
            try:
                cj = getattr(self, "_cookiejar", None)
                if isinstance(cj, httpx.Cookies):
                    preload = [{"name": k, "value": v, "domain": f".{BASE_DOMAIN}", "path": "/"} for k, v in cj.items()]
                    if preload:
                        context.add_cookies(preload)
                        print(f"[galv] Preloaded {len(preload)} cookies")
            except Exception:
                pass

            page = context.new_page()
            context.set_default_timeout(4000)

            try:
                try:
                    page.goto(f"{BASE}/main.aspx", wait_until="domcontentloaded")
                except Exception:
                    pass
                try:
                    loc = page.locator("a[href*='jailinmates.aspx']").first
                    if loc.is_visible():
                        loc.click(force=True, no_wait_after=True)
                        page.wait_for_load_state("domcontentloaded")
                except Exception:
                    pass
                    
                page.goto(ROSTER_HTML, wait_until="domcontentloaded")
                
                try:
                    page.locator("select.ui-pg-selbox").first.select_option(label="All")
                    page.wait_for_timeout(900)
                except Exception:
                    try:
                        page.evaluate("""() => {
                            if (window.$ && $('#tblII').length) {
                                $('#tblII').jqGrid('setGridParam', { rowNum: 9999 }).trigger('reloadGrid');
                            }
                        }""")
                        page.wait_for_timeout(1200)
                    except Exception:
                        pass

                row_loc = page.locator("#tblII tr.jqgrow[id]")
                if row_loc.count() == 0:
                    row_loc = page.locator("#tblII tr[role='row'][id]")

                total = min(max_rows, row_loc.count())
                print(f"[galv] Clicking up to {total} rows, time cap = {max_secs}s")

                start_row = int(os.getenv("HARVEST_START_ROW", "0"))
                if start_row >= total:
                    print(f"[galv] HARVEST_START_ROW {start_row} >= total {total}, nothing to do")
                    return []

                seen_rids = set()
                for i in range(start_row, total):
                    if time.time() - start > max_secs:
                        print("[galv] Harvest time cap reached")
                        break
                    try:
                        if i % 10 == 0:
                            print(f"[galv] Processing row {i}/{total}")

                        row = row_loc.nth(i)
                        rid = (row.get_attribute("id") or "").strip()
                        if not rid or rid in seen_rids:
                            continue
                        seen_rids.add(rid)

                        if not row.is_visible():
                            row.scroll_into_view_if_needed()

                        before_url = page.url
                        row.click(force=True)
                        page.wait_for_timeout(300)

                        cur = page.url
                        if cur != before_url and _accept(cur):
                            html = page.content()
                            links.append({"url": _abs(cur), "rid": rid, "html": html})
                            
                            _write_controlled_snapshot("detail_page", f"inmate_{rid}", html)

                            try:
                                page.go_back(wait_until="domcontentloaded")
                                page.wait_for_timeout(int(ROW_DELAY * 1000))
                                try:
                                    page.locator("select.ui-pg-selbox").first.select_option(label="All")
                                    page.wait_for_timeout(150)
                                except Exception:
                                    pass
                            except Exception:
                                page.goto(ROSTER_HTML, wait_until="domcontentloaded")

                    except Exception:
                        continue
            finally:
                try: 
                    context.close()
                except Exception: 
                    pass
                try: 
                    browser.close()
                except Exception: 
                    pass

        # De-dup by rid
        out: List[Dict[str, Any]] = []
        seen_rids = set()
        for item in links:
            rid = str(item.get("rid") or "")
            if not rid or rid in seen_rids:
                continue
            seen_rids.add(rid)
            out.append(item)

        print(f"[galv] DOM harvest collected {len(out)} items")
        return out

    async def _fetch_detail(self, session: httpx.AsyncClient, url: str, want_mug: bool, rid: str) -> Optional[Dict[str, Any]]:
        """Fetch one inmate detail page."""
        try:
            r = await session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
        except Exception:
            return None

        html = r.text
        return self._parse_detail_html(html, url, rid)

    def _parse_detail_html(self, html: str, url: str, rid: Optional[str]) -> Optional[Dict[str, Any]]:
        soup = BeautifulSoup(html or "", "lxml")

        def txt(sel: str) -> str:
            el = soup.select_one(sel)
            return (el.get_text(strip=True) if el else "").strip()

        name = txt("#mainContent_CenterColumnContent_lblName")
        if not name or _BAD_NAME_RE.search(name):
            return None

        first = last = ""
        if "," in name:
            last, first = [x.strip() for x in name.split(",", 1)]

        age       = txt("#mainContent_CenterColumnContent_lblAge")
        race      = txt("#mainContent_CenterColumnContent_lblRace")
        sex       = txt("#mainContent_CenterColumnContent_lblSex")
        arrest_dt = txt("#mainContent_CenterColumnContent_lblArrestDate")
        agency    = txt("#mainContent_CenterColumnContent_lblAgency")
        total_bond = (
            txt("#mainContent_CenterColumnContent_lblTotalBondAmount")
            or txt("#mainContent_CenterColumnContent_lblTotalBoundAmount")
            or None
        )

        # Extract charges
        charges = []
        t = soup.select_one("#mainContent_CenterColumnContent_dgMainResults")
        tables = [t] if t else []
        for tbl in soup.select("table"):
            if tbl is t: 
                continue
            heads = [th.get_text(strip=True).lower() for th in tbl.select("thead th")] or \
                    [th.get_text(strip=True).lower() for th in tbl.find_all("th")]
            head_str = " ".join(heads)
            if any(k in head_str for k in ["charge","offense"]) and any(k in head_str for k in ["bond","docket","status"]):
                tables.append(tbl)

        for tbl in tables:
            rows = tbl.select("tbody tr") or [tr for tr in tbl.select("tr") if tr.find_all("td")]
            for idx, tr in enumerate(rows):
                tds = tr.find_all("td")
                if idx == 0:
                    maybe_head = " ".join(td.get_text(strip=True).lower() for td in tds)
                    if all(k in maybe_head for k in ["charge","status","docket","bond"]):
                        continue
                if len(tds) >= 4:
                    charges.append({
                        "charge": tds[0].get_text(strip=True),
                        "status": tds[1].get_text(strip=True),
                        "docket": tds[2].get_text(strip=True),
                        "bond":   tds[3].get_text(strip=True),
                    })
                elif len(tds) >= 2:
                    charges.append({
                        "charge": tds[0].get_text(strip=True),
                        "status": (tds[1].get_text(strip=True) if len(tds) > 1 else ""),
                        "docket": (tds[2].get_text(strip=True) if len(tds) > 2 else ""),
                        "bond":   (tds[3].get_text(strip=True) if len(tds) > 3 else ""),
                    })
            if charges:
                break

        # Booking number
        booking_no = ""
        bn = soup.select_one("#mainContent_CenterColumnContent_lblBookingNumber")
        if bn:
            booking_no = bn.get_text(strip=True)

        # Mugshot
        img = soup.select_one("#mainContent_CenterColumnContent_imgPhoto")
        mug_url = None
        if img and img.get("src"):
            src = img["src"].strip()
            mug_url = src if src.startswith("http") else f"{BASE}/{src.lstrip('/')}"

        full_name = f"{last}, {first}".upper().strip(", ") if (last or first) else name.upper()

        # Calculate booking date ISO using helper function
        booking_date_iso = _iso_date_guess(arrest_dt)

        # External ID: prefer booking number, else row id, else stable hash
        if booking_no:
            ext_id = f"bk:{booking_no}"
        elif rid:
            ext_id = f"p2c:rid:{rid}"
        else:
            basis = "|".join([(full_name or ""), (arrest_dt or ""), (race or ""), (sex or "")]).upper()
            ext_id = "p2c:" + _sha1(basis.encode("utf-8")).hexdigest()[:32]

        person = {
            "_ext_id": ext_id,
            "full_name": full_name,
            "dob": None,
            "aka": [],
            "identifiers": {"booking": ([booking_no] if booking_no else [])},
            "contact": {},
            "media": ([{"rel": "mugshot", "url": mug_url}] if mug_url else []),
            "links": [{"rel": "p2c_detail", "url": url}],
        }

        event = {
            "_collection": "galveston_events",
            "person_id": None,
            "county": "Galveston",
            "facility": "Galveston County Jail (P2C)",
            "booking_number": (booking_no or None),
            "status": "In Custody",
            "booked_at": booking_date_iso,
            "released_at": None,
            "source_url": url,
            "charges": charges,
            "bonds": [],
            "total_bond": total_bond,
            "agency": agency,
            "arrest_date": booking_date_iso,
            "race": race,
            "sex": sex,
            "age": age,
        }
        
        # Add standardized booking age fields using parent class method
        event = self._enhance_event(event, booking_date_iso)
        
        return {"person": person, "event": event}

    def fetch(self) -> Iterable[Dict[str, Any]]:
        print("[galv] Starting Galveston P2C scraper")
        
        # Start audit tracking
        self._audit_start()

        # 1) jqGrid sniff for JSON data
        grid = self._fetch_jqgrid_json()
        rows = (grid or {}).get("rows") or []

        # 2) Harvest detail links via Playwright DOM
        detail_items = self._harvest_detail_links_via_playwright() or []
        self._audit_inc("detail_links_found", len(detail_items))

        if TEST_MAX_LINKS > 0 and len(detail_items) > TEST_MAX_LINKS:
            detail_items = detail_items[:TEST_MAX_LINKS]
            print(f"[galv] Limited to {len(detail_items)} items for testing")

        # Coerce to uniform format
        coerced: List[Tuple[str, str, Optional[str]]] = []
        for it in detail_items:
            if isinstance(it, dict):
                url = str(it.get("url") or "")
                rid = str(it.get("rid") or "")
                html = it.get("html")
            elif isinstance(it, (tuple, list)) and len(it) == 2:
                url, rid = str(it[0] or ""), str(it[1] or "")
                html = None
            else:
                url = str(it or "")
                rid = _rid_from_url(url) or ""
                html = None
            if url and rid:
                coerced.append((url, rid, html))

        # De-duplicate by rid
        seen_rids: set[str] = set()
        items: List[Tuple[str, str, Optional[str]]] = []
        for url, rid, html in coerced:
            if rid in seen_rids:
                continue
            seen_rids.add(rid)
            items.append((url, rid, html))

        print(f"[galv] Processing {len(items)} detail pages")
        self._audit_inc("prefixes_scanned", 1)

        # Fallback to roster table if no items harvested
        if not items:
            print("[galv] No items harvested, using HTML roster fallback")
            html = self._render_roster_html()
            if not html:
                print("[galv] Failed to fetch HTML roster")
                return
                
            people = self._parse_roster_table(html)
            print(f"[galv] Parsed {len(people)} people from roster table")

            # Minimal person + event records for roster-only data
            for p in people:
                booking_date_iso = _iso_date_guess(p.get("arrest_date"))
                
                yield {
                    "full_name": p["full_name"],
                    "dob": p.get("dob"),
                    "aka": [],
                    "identifiers": {"booking": ([p["booking_number"]] if p.get("booking_number") else [])},
                    "contact": {},
                    "media": [],
                    "links": [{"rel": "p2c_roster_html", "url": ROSTER_HTML}],
                }
                
                event = {
                    "_collection": "galveston_events",
                    "person_id": None,
                    "county": "Galveston",
                    "facility": "Galveston County Jail (P2C)",
                    "booking_number": p.get("booking_number"),
                    "status": "In Custody",
                    "booked_at": booking_date_iso,
                    "released_at": None,
                    "source_url": ROSTER_HTML,
                    "charges": [],
                    "bonds": ([{"amount": p["bond_amount"]}] if p.get("bond_amount") else []),
                    "total_bond": p.get("bond_amount"),
                    "agency": p.get("agency"),
                    "arrest_date": booking_date_iso,
                    "race": p.get("race"),
                    "sex": p.get("sex"),
                    "age": p.get("age"),
                }
                
                # Add standardized booking age fields
                event = self._enhance_event(event, booking_date_iso)
                self._audit_inc("events_yielded", 1)
                yield event
                
            print("[galv] Finished HTML roster fallback")
            self._audit_finish()
            return

        # 3) Process detail pages
        results: List[Dict[str, Any]] = []

        async def run_details():
            sem = asyncio.Semaphore(CONCURRENCY)
            headers = dict(UA)
            headers["Referer"] = ROSTER_HTML
            async with httpx.AsyncClient(
                headers=headers,
                verify=_verify(),
                timeout=TIMEOUT,
                cookies=getattr(self, "_cookiejar", httpx.Cookies()),
                follow_redirects=True,
            ) as session:

                async def worker(url: str, rid: str, html_snapshot: Optional[str]):
                    async with sem:
                        html = html_snapshot
                        if not html:
                            try:
                                resp = await session.get(url, timeout=TIMEOUT)
                                resp.raise_for_status()
                                html = resp.text
                            except Exception:
                                self._audit_inc("errors", 1)
                                return

                        rec = self._parse_detail_html(html or "", url, rid)
                        if rec:
                            results.append(rec)
                            self._audit_inc("details_parsed_ok", 1)
                        else:
                            self._audit_inc("errors", 1)

                await asyncio.gather(*(worker(u, r, h) for (u, r, h) in items))

        try:
            asyncio.run(run_details())
        except RuntimeError:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(run_details())

        print(f"[galv] Detail fetch complete: {len(results)} results")

        # 4) Yield results with audit tracking
        for rec in results:
            person = rec["person"]
            event = rec["event"]
            
            booking_category = event.get("booking_age_category", "unknown")
            self._audit_success(person.get("full_name", "UNKNOWN"), booking_category)

            # Handle person upsert tracking
            res = self.upsert_person(person)
            if res.get("inserted"):
                self._audit_inc("upserts_person_inserted", 1)
            else:
                self._audit_inc("upserts_person_updated", 1)
                
            pid = res.get("_id")
            event["person_id"] = pid
            self._audit_inc("events_yielded", 1)
            yield event

        print("[galv] Finished fetch()")
        self._audit_finish()

if __name__ == "__main__":
    # Smoke test
    from storage.mongo_client import get_db
    s = GalvestonP2CFastScraper(get_db())
    
    print("SMOKE: sniffing jqGrid JSON...")
    grid = s._fetch_jqgrid_json()
    rows = len((grid or {}).get("rows") or [])
    print("SMOKE: jqGrid rows =", rows)

    detail_links = []
    if rows:
        detail_links = _extract_detail_hrefs_from_roster_json(json.dumps(grid))
        print("SMOKE: links from JSON =", len(detail_links))

    if not detail_links:
        print("SMOKE: trying DOM harvest...")
        detail_links = s._harvest_detail_links_via_playwright()
        print("SMOKE: links from DOM =", len(detail_links))

    print("SMOKE: sample links:")
    for u in detail_links[:3]:
        print("  ", u)