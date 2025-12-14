#!/usr/bin/env python3
"""
TDCJ IVSS Counties scraper: recent intakes (last N hours)

Overview
- Uses Selenium (headless Chrome) to submit a broad search by first-name initial (A..Z)
- Collects offender detail URLs, then fetches each detail page via HTTPX using Selenium session cookies
- Parses details with BeautifulSoup to extract:
  - name, tdcj_id, state_id, custody_status (+ date)
  - location (facility, city, type)
  - subgrid Intake Date (savin_intakedate) rows; chooses latest and filters within window
- Emits JSONL to stdout; optional --mongo-upsert will upsert into a dynamic collection per location

Collection naming
- By default uses simple_{slug} where slug is the lowercased city if available, else facility name
- You can override with --collection-prefix (default: simple_)

Note
- This site is heavily JS-driven. We use Selenium for the search results and direct HTTPX for details (faster).
- If direct HTTPX fails due to auth/state, we fallback to parsing with Selenium.

"""
from __future__ import annotations
import argparse
import sys
import time
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Iterable, List, Optional, Dict, Any, Tuple

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

# Local storage client
try:
    from storage.mongo_client import get_db
except Exception:
    get_db = None  # Mongo optional unless --mongo-upsert is set

BASE_URL = "https://ivss-counties.tdcj.texas.gov"
HOME_URL = f"{BASE_URL}/home"

# Heuristic locators (best-effort; site may change)
SEARCH_BUTTON_TEXTS = ["Search", "SEARCH"]  # button label on home
FIRST_NAME_LABELS = ["First Name", "FIRST NAME", "First name"]
RESULT_CARD_SELECTOR = "a[href*='iicid=']"  # links to details include iicid param

# Subgrid column markers on detail page
INTAKE_DATE_HEADER = "Intake Date"
RELEASE_DATE_HEADER = "Release Date"
FACILITY_HEADER = "Facility"

# Slugify helper
_slug_non_alnum = re.compile(r"[^a-z0-9]+")

def slugify(s: str) -> str:
    s = s.strip().lower()
    s = _slug_non_alnum.sub("_", s)
    s = s.strip("_")
    return s or "unknown"

@dataclass
class OffenderRecord:
    source: str
    offender_id: Optional[str]
    name: Optional[str]
    tdcj_id: Optional[str]
    state_id: Optional[str]
    custody_status: Optional[str]
    custody_status_date: Optional[str]
    latest_intake_date: Optional[str]
    release_date: Optional[str]
    facility: Optional[str]
    location_name: Optional[str]
    location_type: Optional[str]
    city: Optional[str]
    state: Optional[str]
    detail_url: str
    fetched_at: str

    def collection_key(self, prefix: str = "simple_") -> str:
        # Prefer city, then facility, finally fallback to 'tx'
        basis = self.city or self.facility or "tx"
        return f"{prefix}{slugify(basis)}"

class IVSSScraper:
    def __init__(self, headless: bool = True, window_hours: int = 72, 
                 letters: str = "abcdefghijklmnopqrstuvwxyz", 
                 delay: float = 0.5, verbose: bool = False):
        self.headless = headless
        self.window_hours = window_hours
        self.letters = letters
        self.delay = delay
        self.verbose = verbose
        self._driver = None
        self._client = None
        # progress controls (set by main)
        self._progress_every = 10
        self._heartbeat_sec = 30

    # ---------- Selenium setup ----------
    def _get_driver(self):
        if self._driver is None:
            # Lazy import selenium so --help works without deps installed
            from selenium import webdriver  # type: ignore
            from selenium.webdriver.chrome.service import Service  # type: ignore
            from selenium.webdriver.chrome.options import Options  # type: ignore
            from webdriver_manager.chrome import ChromeDriverManager  # type: ignore

            chrome_options = Options()
            if self.headless:
                chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--window-size=1280,1200")
            chrome_options.add_argument("--disable-dev-shm-usage")
            service = Service(ChromeDriverManager().install())
            self._driver = webdriver.Chrome(service=service, options=chrome_options)
        return self._driver

    def _get_httpx_client(self):
        if self._client is None:
            self._client = httpx.Client(follow_redirects=True, timeout=30)
        return self._client

    def _transfer_cookies(self):
        # Copy Selenium cookies into HTTPX client
        client = self._get_httpx_client()
        driver = self._get_driver()
        for c in driver.get_cookies():
            # httpx CookieJar.set(name, value, domain=..., path=...)
            try:
                client.cookies.set(
                    c.get("name"), c.get("value"), domain=c.get("domain"), path=c.get("path")
                )
            except Exception:
                # Fallback minimal set
                client.cookies.set(c.get("name"), c.get("value"))

    # ---------- High-level scraping ----------
    def run(self) -> Iterable[OffenderRecord]:
        driver = self._get_driver()
        from selenium.webdriver.support.ui import WebDriverWait  # type: ignore
        driver.get(HOME_URL)
        WebDriverWait(driver, 20).until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(self.delay)

        # Try to reveal search inputs – depending on page structure this may be a modal or inline form.
        self._open_search_if_needed(driver)
        if self.verbose:
            try:
                print(f"[IVSS] After open_search, URL: {driver.current_url}", file=sys.stderr)
                from selenium.webdriver.common.by import By  # type: ignore
                buttons = driver.find_elements(By.TAG_NAME, "button")
                print(f"[IVSS] Buttons on page: {len(buttons)}", file=sys.stderr)
                for b in buttons[:25]:
                    txt = (b.text or "").strip()
                    print(f"[IVSS] button text={txt}", file=sys.stderr)
            except Exception:
                pass

        seen_detail_urls = set()
        start_ts = time.monotonic()
        last_log_ts = start_ts
        total_attempted = 0
        total_emitted = 0
        for ch in self.letters:
            if self.verbose:
                print(f"[IVSS] Searching first name initial: {ch}", file=sys.stderr)
            try:
                self._search_by_first_initial(driver, ch)
                urls = self._collect_detail_links(driver)
                if self.verbose:
                    print(f"[IVSS] Found {len(urls)} detail links for '{ch}'", file=sys.stderr)
                # Transfer cookies once after first results appear
                self._transfer_cookies()
                for url in urls:
                    if url in seen_detail_urls:
                        continue
                    seen_detail_urls.add(url)
                    total_attempted += 1
                    # periodic heartbeat
                    now = time.monotonic()
                    if now - last_log_ts >= self._heartbeat_sec:
                        elapsed = now - start_ts
                        rate = total_attempted / elapsed if elapsed > 0 else 0.0
                        print(f"[IVSS] Heartbeat: attempted={total_attempted}, emitted={total_emitted}, elapsed={elapsed:.1f}s, rate={rate:.2f}/s", file=sys.stderr)
                        last_log_ts = now
                    rec = self._fetch_and_parse_detail(url)
                    if rec is None:
                        continue
                    # Filter by window
                    if self._within_window(rec.latest_intake_date):
                        total_emitted += 1
                        if self.verbose and (total_attempted % max(1, self._progress_every) == 0):
                            print(f"[IVSS] Progress: {total_emitted} within-window out of {total_attempted} attempted", file=sys.stderr)
                        yield rec
                # be polite
                time.sleep(self.delay)
            except Exception as ex:
                if self.verbose:
                    print(f"[IVSS] Error on letter '{ch}': {ex}", file=sys.stderr)
                continue

    def _open_search_if_needed(self, driver):
        # The home page contains a Search section; sometimes a button is present
        from selenium.webdriver.common.by import By  # type: ignore
        from selenium.webdriver.support.ui import WebDriverWait  # type: ignore
        from selenium.webdriver.support import expected_conditions as EC  # type: ignore
        try:
            # Try to scroll into view
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight/3);")
            time.sleep(0.2)
            # Click a button or link with text 'Search' if exists (global)
            # Prefer the explicit SEARCH button if present
            try:
                btn = WebDriverWait(driver, 5).until(
                    EC.elementToBeClickable((By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'search')]")
                ))
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                time.sleep(0.2)
                try:
                    btn.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", btn)
                time.sleep(0.8)
            except Exception:
                searchables = driver.find_elements(By.XPATH, "//button|//a")
                for el in searchables:
                    label = (el.text or "").strip().lower()
                    if "search" in label:
                        try:
                            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
                            time.sleep(0.1)
                            try:
                                el.click()
                            except Exception:
                                driver.execute_script("arguments[0].click();", el)
                            time.sleep(0.8)
                            break
                        except Exception:
                            continue
            # Wait for any input to appear (modal or form)
            try:
                WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text'], input[type='search']"))
                )
            except Exception:
                pass
        except Exception:
            pass

    def _search_by_first_initial(self, driver, initial: str):
        # Find the main search widget under the "SEARCH FOR AN OFFENDER" section
        from selenium.webdriver.common.by import By  # type: ignore
        from selenium.webdriver.common.keys import Keys  # type: ignore
        from selenium.webdriver.support.ui import WebDriverWait  # type: ignore
        from selenium.webdriver.support import expected_conditions as EC  # type: ignore

        initial_upper = initial.upper()

        # Prefer targeting by section heading (case-insensitive)
        heading_xpath = "//h3[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'search for an offender')]"
        input_el = None
        try:
            heading = driver.find_element(By.XPATH, heading_xpath)
            input_el = heading.find_element(By.XPATH, "following::input[@type='text' or @type='search'][1]")
        except Exception:
            # Fallback: first visible text/search input on page
            try:
                inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='text'],input[type='search']")
                for el in inputs:
                    if el.is_displayed():
                        input_el = el
                        break
            except Exception:
                pass
        if input_el is None:
            raise RuntimeError("Could not locate the main search input on IVSS home page.")

        # Clear and type
        try:
            input_el.clear()
        except Exception:
            pass
        input_el.send_keys(initial_upper)
        time.sleep(0.2)

        # Click the nearby SEARCH button under the same section
        submitted = False
        try:
            search_btn = driver.find_element(By.XPATH, heading_xpath + "//following::button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'search')][1]")
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", search_btn)
            time.sleep(0.1)
            try:
                search_btn.click()
            except Exception:
                driver.execute_script("arguments[0].click();", search_btn)
            submitted = True
        except Exception:
            # ENTER key as fallback
            try:
                input_el.send_keys(Keys.ENTER)
                submitted = True
            except Exception:
                pass

        # Wait for the results grid to render (rows present)
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#divOffenderList tbody tr"))
            )
        except Exception:
            time.sleep(1.0)

        if self.verbose:
            try:
                print(f"[IVSS] After submit, URL: {driver.current_url}", file=sys.stderr)
                # Count rows if available
                rows_ct = len(driver.find_elements(By.CSS_SELECTOR, "#divOffenderList tbody tr"))
                print(f"[IVSS] Grid rows visible: {rows_ct}", file=sys.stderr)
            except Exception:
                pass

    def _collect_detail_links(self, driver) -> List[str]:
        """Collect detail URLs by interrogating the Kendo Grid for IDEnc values.

        Strategy
        - Prefer reading Kendo Grid dataSource.view() items via JS (fast, stable).
        - Fallback to scraping hidden cells .gridoffid within the grid rows.
        - Construct likely detail URLs from IDEnc and verify via HTTPX; if none validate,
          as a final fallback scan anchors (legacy behavior).
        """
        from selenium.webdriver.common.by import By  # type: ignore

        def _kendo_exec(js: str):
            # Helper to execute JS that depends on Kendo/jQuery safely.
            wrapper = (
                "var $k = (window.kendo && window.kendo.jQuery) ? window.kendo.jQuery : (window.jQuery || window.$);"
                "var el = document.getElementById('divOffenderList');"
                "if(!$k || !el){ return null; }"
                "var grid = $k(el).data('kendoGrid');"
                "if(!grid){ return null; }"
                + js
            )
            try:
                return driver.execute_script(wrapper)
            except Exception:
                return None

        # Try to get all page items' IDs via the grid API
        ids: List[Tuple[str, Optional[str]]] = []  # (IDEnc, OffenderId)
        # Determine total pages
        info = _kendo_exec("var ds=grid.dataSource; return {total: ds.total(), pageSize: ds.pageSize(), page: ds.page()};")
        total_pages = 1
        if isinstance(info, dict) and info.get("pageSize"):
            total = int(info.get("total") or 0)
            page_size = int(info.get("pageSize") or 10)
            total_pages = max(1, (total + page_size - 1) // page_size)
        if self.verbose:
            print(f"[IVSS] Kendo pages detected: {total_pages}", file=sys.stderr)

        # Iterate pages via Kendo dataSource to avoid clicking DOM pager
        for p in range(1, total_pages + 1):
            _kendo_exec(f"grid.dataSource.page({p});")
            # Wait briefly for async fetch
            for _ in range(20):
                time.sleep(0.15)
                cur = _kendo_exec("return grid.dataSource.page();")
                length = _kendo_exec("var v=grid.dataSource.view(); return v ? v.length : 0;")
                if cur == p and (isinstance(length, int) and length > 0):
                    break
            # Extract items
            items = _kendo_exec("var v=grid.dataSource.view() || []; return v.map(function(it){return {IDEnc: it.IDEnc || it.IdEnc || it.Id || null, ID: it.ID || it.Id || null};});") or []
            for it in items:
                idenc = None
                offid = None
                if isinstance(it, dict):
                    idenc = it.get("IDEnc")
                    offid = it.get("ID")
                if idenc and isinstance(idenc, str):
                    ids.append((idenc, offid if isinstance(offid, str) else None))
            # Also scrape hidden cells from the DOM to be safe (works even if field names differ)
            try:
                rows = driver.find_elements(By.CSS_SELECTOR, "#divOffenderList tbody tr")
                for r in rows:
                    try:
                        c1 = r.find_element(By.CSS_SELECTOR, ".gridoffid")
                        c2 = r.find_element(By.CSS_SELECTOR, ".gridoffid2")
                        id1 = (c1.text or "").strip()
                        id2 = (c2.text or "").strip()
                        if id1:
                            ids.append((id1, id2 or None))
                    except Exception:
                        continue
            except Exception:
                pass

        # Deduplicate IDs preserving order
        seen = set()
        unique_ids = []
        for tup in ids:
            if not tup:
                continue
            key = tup[0]
            if key and key not in seen:
                seen.add(key)
                unique_ids.append(tup)

        # Build candidate URLs and validate via HTTPX (lightweight HEAD/GET)
        if self.verbose:
            try:
                fn_src = driver.execute_script("return window.offenderDetails ? window.offenderDetails.toString() : null;")
                if fn_src:
                    # Print up to 2000 chars for inspection
                    print("[IVSS] offenderDetails():\n" + str(fn_src)[:2000], file=sys.stderr)
            except Exception:
                pass
            try:
                print(f"[IVSS] Collected raw IDs (first 10): {unique_ids[:10]}", file=sys.stderr)
            except Exception:
                pass
        candidates: List[str] = []
        # Derived from offenderDetails() function on the site
        def make_detail_url(idenc: str, offid: Optional[str]) -> str:
            ph = f"&ph={offid}" if offid else ""
            return f"{BASE_URL}/iic-info?iicid={idenc}{ph}"
        client = self._get_httpx_client()
        for (iid, offid) in unique_ids:
            trial = make_detail_url(iid, offid)
            try:
                resp = client.get(trial)
                if resp.status_code == 200 and ("idoc_name_1_readonly" in resp.text or "subgrid_1_element" in resp.text or "Offender Detail" in resp.text):
                    candidates.append(trial)
            except Exception:
                continue

        # If we couldn't validate any, last fallback: scan anchors on the page (legacy)
        if not candidates:
            anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
            if self.verbose:
                try:
                    print(f"[IVSS] Anchors on page: {len(anchors)}", file=sys.stderr)
                    for a in anchors[:25]:
                        href_dbg = a.get_attribute("href") or ""
                        txt_dbg = (a.text or "").strip()
                        print(f"[IVSS] href={href_dbg} text={txt_dbg}", file=sys.stderr)
                except Exception:
                    pass
            for a in anchors:
                href = a.get_attribute("href") or ""
                href_l = href.lower()
                if not href:
                    continue
                if any(k in href_l for k in ["iicid=", "offenderdetail", "offender-detail", "/offender/"]):
                    if href.startswith("/"):
                        href = BASE_URL + href
                    if any(x in href_l for x in ["/login", "/resources", "/contact-us", "/faq"]):
                        continue
                    candidates.append(href)

        # de-dupe preserve order
        return list(dict.fromkeys(candidates))

    def _fetch_and_parse_detail(self, url: str) -> Optional[OffenderRecord]:
        client = self._get_httpx_client()
        now_iso = datetime.now(timezone.utc).isoformat()
        html = None
        used_selenium = False
        # Try HTTP first for basic fields; intake grid likely needs JS, so we'll still use Selenium to read it via Kendo if missing
        try:
            resp = client.get(url)
            if resp.status_code == 200 and ("<html" in resp.text.lower() or "<div" in resp.text.lower()):
                html = resp.text
        except Exception:
            html = None
        if html is None:
            html = ""

        # Always navigate with Selenium to extract Kendo subgrid reliably
        d = self._get_driver()
        from selenium.webdriver.support.ui import WebDriverWait  # type: ignore
        from selenium.webdriver.support import expected_conditions as EC  # type: ignore
        from selenium.webdriver.common.by import By  # type: ignore
        try:
            d.get(url)
            WebDriverWait(d, 25).until(lambda dr: dr.execute_script("return document.readyState") == "complete")
            # Wait for any Kendo grid to bind; then look for one with 'Intake Date' column
            # We'll poll the page for a Kendo grid instance with a column named 'Intake Date'
            def _read_intake_rows():
                js = (
                    "var $k = (window.kendo && window.kendo.jQuery) ? window.kendo.jQuery : (window.jQuery || window.$);"
                    "if(!$k) return null;"
                    "var grids = [];"
                    "$k('.k-grid').each(function(){ var g = $k(this).data('kendoGrid'); if(g){ grids.push(g); }});"
                    "for(var i=0;i<grids.length;i++){ var g=grids[i];"
                    "  var cols=(g.columns||[]).map(function(c){return (c.title||c.field||'').toString().trim();});"
                    "  if(cols.join('|').toLowerCase().indexOf('intake date')>=0){"
                    "    var view=g.dataSource.view()||[];"
                    "    return view.map(function(r){ return r; });"
                    "  }"
                    "}"
                    "return null;"
                )
                try:
                    return d.execute_script(js)
                except Exception:
                    return None
            rows = None
            for _ in range(40):
                time.sleep(0.25)
                rows = _read_intake_rows()
                if isinstance(rows, list) and len(rows) > 0:
                    break
            used_selenium = True
            # Capture the rendered HTML to support fallback static parsing
            try:
                html = d.page_source
            except Exception:
                pass
        except Exception:
            pass

        soup = BeautifulSoup(html, "lxml")

        # Extract fields
        def text_by_id(i):
            el = soup.select_one(f"#{i}")
            return el.get_text(strip=True) if el else None

        name = text_by_id("idoc_name_1_readonly")
        tdcj_id = text_by_id("idoc_idocnumber_readonly")
        state_id = text_by_id("idoc_stateidnumber_readonly")
        custody_status = text_by_id("idoc_offenderstatus_readonly")
        custody_status_date = text_by_id("idoc_custodystatustime_readonly")
        location_name = text_by_id("idoc_name_readonly")  # labeled 'Location'
        location_type = text_by_id("idoc_facilitytypes_readonly")
        city = text_by_id("idoc_city_readonly")
        state = text_by_id("idoc_state_readonly")

        # Subgrid: prefer Selenium Kendo extraction; fallback to static HTML parse
        latest_intake_str = None
        release_date_str = None
        facility_val = None
        if used_selenium:
            try:
                # Extract rows again with fields we care about
                js2 = (
                    "var $k = (window.kendo && window.kendo.jQuery) ? window.kendo.jQuery : (window.jQuery || window.$);"
                    "if(!$k) return null;"
                    "var grids = [];"
                    "$k('.k-grid').each(function(){ var g = $k(this).data('kendoGrid'); if(g){ grids.push(g); }});"
                    "for(var i=0;i<grids.length;i++){ var g=grids[i];"
                    "  var cols=(g.columns||[]).map(function(c){return (c.title||c.field||'').toString().trim().toLowerCase();});"
                    "  var idxIntake = cols.indexOf('intake date');"
                    "  var idxRelease = cols.indexOf('release date');"
                    "  var idxFacility = cols.indexOf('facility');"
                    "  if(idxIntake>=0){"
                    "    var v=g.dataSource.view()||[];"
                    "    var out=[];"
                    "    for(var j=0;j<v.length;j++){ var r=v[j];"
                    "      var cells = [];"
                    "      if(r.cells){ cells = r.cells; }"
                    "      out.push({intake:r['Intake Date']||r['intakeDate']||r['IntakeDate']|| (cells[idxIntake]?cells[idxIntake].value:null),"
                    "                release:r['Release Date']|| (cells[idxRelease]?cells[idxRelease].value:null),"
                    "                facility:r['Facility']|| (cells[idxFacility]?cells[idxFacility].value:null)});"
                    "    }"
                    "    return out;"
                    "  }"
                    "}"
                    "return [];"
                )
                data = d.execute_script(js2) or []
                if self.verbose and isinstance(data, list):
                    try:
                        print(f"[IVSS] Detail grid rows (JS view): {len(data)}", file=sys.stderr)
                    except Exception:
                        pass
                if not data:
                    # Try reading transport endpoint for the intake grid and fetch via HTTPX
                    js3 = (
                        "var $k = (window.kendo && window.kendo.jQuery) ? window.kendo.jQuery : (window.jQuery || window.$);"
                        "if(!$k) return null;"
                        "var found=null;"
                        "$k('.k-grid').each(function(){ var g = $k(this).data('kendoGrid'); if(g){"
                        " var cols=(g.columns||[]).map(function(c){return (c.title||c.field||'').toString().trim().toLowerCase();});"
                        " if(cols.join('|').indexOf('intake date')>=0 && g.dataSource && g.dataSource.transport && g.dataSource.transport.options && g.dataSource.transport.options.read){"
                        "   var ro = g.dataSource.transport.options.read;"
                        "   var url = (typeof ro.url==='function')? ro.url() : ro.url;"
                        "   var type = ro.type || 'GET';"
                        "   var pdata = null; try{ var rd = ro.data; pdata = (typeof rd==='function')? rd() : rd; }catch(e){ pdata = null; }"
                        "   found={url:url, method:type, data:pdata};"
                        " }"
                        "}});"
                        "return found;"
                    )
                    cfg = d.execute_script(js3)
                    if cfg:
                        data = {"__transport__": cfg}

                # Choose latest intake
                best_dt = None
                raw_rows = data
                # If Selenium returned a transport config instead of rows, pull JSON directly now
                if isinstance(data, dict) and data.get('__transport__'):
                    cfg = data.get('__transport__') or {}
                    t_url = cfg.get('url')
                    t_method = (cfg.get('method') or 'GET').upper()
                    t_data = cfg.get('data') or {}
                    try:
                        if t_method == 'POST':
                            rj = client.post(t_url, data=t_data)
                        else:
                            rj = client.get(t_url, params=t_data)
                        if rj.status_code == 200:
                            j = rj.json()
                            # Common Kendo schema: { Data: [...], Total: n } or an array
                            if isinstance(j, dict):
                                raw_rows = j.get('Data') or j.get('data') or j.get('results') or []
                            elif isinstance(j, list):
                                raw_rows = j
                    except Exception:
                        raw_rows = []
                for row in raw_rows:
                    intake_txt = None
                    if isinstance(row, dict):
                        intake_txt = row.get('intake') or row.get('Intake Date')
                    if not intake_txt:
                        continue
                    try:
                        dt = dtparser.parse(str(intake_txt))
                    except Exception:
                        continue
                    if best_dt is None or dt > best_dt:
                        best_dt = dt
                        latest_intake_str = dt.isoformat()
                        r_txt = row.get('release') if isinstance(row, dict) else None
                        try:
                            release_date_str = dtparser.parse(str(r_txt)).isoformat() if r_txt else None
                        except Exception:
                            release_date_str = None
                        facility_val = row.get('facility') if isinstance(row, dict) else None
            except Exception:
                pass

        if not latest_intake_str:
            # Fallback to static parse if Selenium Kendo read didn't succeed
            li, rd, fac = self._parse_subgrid_latest(soup)
            latest_intake_str = li
            release_date_str = rd
            facility_val = fac

        if self.verbose:
            try:
                print(f"[IVSS] Parsed latest intake for detail: {latest_intake_str}", file=sys.stderr)
            except Exception:
                pass

        offender_id = None
        # Extract iicid from URL
        m = re.search(r"[?&]iicid=([0-9a-fA-F\-]{36})", url)
        if m:
            offender_id = m.group(1)

        rec = OffenderRecord(
            source="ivss-counties",
            offender_id=offender_id,
            name=name,
            tdcj_id=tdcj_id,
            state_id=state_id,
            custody_status=custody_status,
            custody_status_date=custody_status_date,
            latest_intake_date=latest_intake_str,
            release_date=release_date_str,
            facility=facility_val,
            location_name=location_name,
            location_type=location_type,
            city=city,
            state=state,
            detail_url=url,
            fetched_at=now_iso,
        )
        return rec

    def _parse_subgrid_latest(self, soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        # Locate the Kendo grid content in the subgrid container
        grid = soup.select_one("#subgrid_1_element .k-grid-content table")
        if not grid:
            # fallback: any table under subgrid container
            grid = soup.select_one("#subgrid_1_element table")
        if not grid:
            return None, None, None

        # Map header indices
        thead = grid.find_previous("div", class_="k-grid-header")
        idx_intake = idx_release = idx_facility = None
        if thead:
            ths = thead.select("th")
            for i, th in enumerate(ths):
                title = th.get_text(strip=True)
                if title == INTAKE_DATE_HEADER:
                    idx_intake = i
                elif title == RELEASE_DATE_HEADER:
                    idx_release = i
                elif title == FACILITY_HEADER:
                    idx_facility = i
        # Fallback: assume columns order if not found
        if idx_intake is None:
            idx_intake = 1
        if idx_release is None:
            idx_release = 2
        if idx_facility is None:
            idx_facility = 3

        # Iterate rows, pick the max intake date
        latest_dt: Optional[datetime] = None
        latest_intake_str: Optional[str] = None
        latest_release_str: Optional[str] = None
        latest_facility: Optional[str] = None

        tbody = grid.select_one("tbody")
        rows = tbody.select("tr") if tbody else []
        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) <= max(idx_intake, idx_release, idx_facility):
                continue
            intake_txt = tds[idx_intake].get_text(strip=True) or None
            facility_txt = tds[idx_facility].get_text(strip=True) or None
            release_txt = tds[idx_release].get_text(strip=True) or None
            if not intake_txt:
                continue
            # Examples like '7/15/1970 12:00 AM' – parse in US format
            try:
                dt = dtparser.parse(intake_txt)
            except Exception:
                continue
            if latest_dt is None or dt > latest_dt:
                latest_dt = dt
                latest_intake_str = dt.isoformat()
                latest_release_str = dtparser.parse(release_txt).isoformat() if release_txt else None
                latest_facility = facility_txt
        return latest_intake_str, latest_release_str, latest_facility

    def _within_window(self, iso_dt: Optional[str]) -> bool:
        if not iso_dt:
            return False
        try:
            dt = dtparser.parse(iso_dt)
        except Exception:
            return False
        # Normalize to aware UTC if naive
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        window_start = datetime.now(timezone.utc) - timedelta(hours=self.window_hours)
        return dt >= window_start

    def close(self):
        try:
            if self._client:
                self._client.close()
        finally:
            try:
                if self._driver:
                    self._driver.quit()
            except Exception:
                pass


def upsert_to_mongo(records: Iterable[OffenderRecord], collection_prefix: str = "simple_") -> int:
    if get_db is None:
        raise RuntimeError("Mongo not available; install dependencies and ensure storage.mongo_client works.")
    db = get_db()
    count = 0
    for rec in records:
        coll_name = rec.collection_key(collection_prefix)
        coll = db[coll_name]
        key = {
            "source": rec.source,
            "offender_id": rec.offender_id,
            "tdcj_id": rec.tdcj_id,
        }
        update = {"$set": asdict(rec)}
        coll.update_one(key, update, upsert=True)
        count += 1
    return count


def iter_with_side_effect(it: Iterable[OffenderRecord], fn):
    for x in it:
        fn(x)
        yield x


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape TDCJ IVSS Counties for recent intakes (last N hours)")
    p.add_argument("--window-hours", type=int, default=72, help="Lookback window in hours (default 72)")
    p.add_argument("--letters", type=str, default="abcdefghijklmnopqrstuvwxyz", help="First-name initials to search, e.g. 'abc' or 'a-z' (default a..z)")
    p.add_argument("--headless", action="store_true", help="Run Chrome headless (default on)")
    p.add_argument("--no-headless", dest="headless", action="store_false", help="Disable headless mode")
    p.set_defaults(headless=True)
    p.add_argument("--delay", type=float, default=0.5, help="Delay between actions (seconds)")
    p.add_argument("--verbose", action="store_true", help="Verbose logs to stderr")
    p.add_argument("--mongo-upsert", action="store_true", help="Upsert results into Mongo collections per location")
    p.add_argument("--collection-prefix", type=str, default="simple_", help="Prefix for dynamic collection names (default simple_)")
    p.add_argument("--progress-every", type=int, default=10, help="How often (every N details) to print progress to stderr (default 10)")
    p.add_argument("--heartbeat-sec", type=int, default=30, help="Emit a heartbeat line at least this often, even if no progress (default 30s)")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    # Expand letters like a-z -> abc...z
    letters = args.letters
    m = re.fullmatch(r"([a-zA-Z])-([a-zA-Z])", letters)
    if m:
        start, end = m.group(1).lower(), m.group(2).lower()
        if ord(start) <= ord(end):
            letters = "".join(chr(c) for c in range(ord(start), ord(end) + 1))
        else:
            letters = "".join(chr(c) for c in range(ord(end), ord(start) + 1))

    scraper = IVSSScraper(
        headless=args.headless,
        window_hours=args.window_hours,
        letters=letters,
        delay=args.delay,
        verbose=args.verbose,
    )
    # Attach runtime progress config
    scraper._progress_every = getattr(args, "progress_every", 10)
    scraper._heartbeat_sec = getattr(args, "heartbeat_sec", 30)

    printed = 0
    try:
        stream = scraper.run()
        # If upserting, tee the stream so we can both print and upsert
        if args.mongo_upsert:
            def _print_jsonl(rec: OffenderRecord):
                import json
                print(json.dumps(asdict(rec), ensure_ascii=False))
            count = upsert_to_mongo(iter_with_side_effect(stream, _print_jsonl), args.collection_prefix)
            if args.verbose:
                print(f"[IVSS] Upserted {count} records", file=sys.stderr)
        else:
            import json
            for rec in stream:
                print(json.dumps(asdict(rec), ensure_ascii=False))
                printed += 1
    finally:
        scraper.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
