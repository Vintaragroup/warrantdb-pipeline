"""
HCSO DOB enrichment client

This module provides a lightweight HTML scraper to retrieve Date of Birth (DOB)
from the Harris County Sheriff's Office inmate lookup pages using either:
  - SPN (preferred, high confidence)
  - Name (LAST, FIRST), with optional SPN verification if present on page

Environment variables (strongly recommended to set explicitly):
  HCSO_SPN_URL_FMT  - e.g., "https://example.harriscounty.gov/inmate?spn={spn}"
  HCSO_NAME_URL_FMT - e.g., "https://example.harriscounty.gov/inmate?last={last}&first={first}"
  HCSO_USER_AGENT   - override default UA
  HCSO_THROTTLE_SEC - polite pause between requests (default 0.7)
  HCSO_TIMEOUT_SEC  - HTTP timeout per request (default 20)

Notes:
  - The exact public URLs for HCSO change periodically; make them configurable.
  - Parsing uses a generic label/value extractor that tries common patterns
    (th/td tables, 2-column td tables, dl/dt/dd definitions).
  - This client does not require Playwright; it uses requests + bs4 only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict, Any
import os
import time
import logging

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)


def _label_value_parse(html: str) -> Dict[str, str]:
    """Best-effort extraction of label/value fields from a details page."""
    out: Dict[str, str] = {}
    # Prefer lxml if available, else fall back to built-in html.parser
    try:
        soup = BeautifulSoup(html or "", "lxml")
    except Exception:
        soup = BeautifulSoup(html or "", "html.parser")

    def _clean_label(s: str) -> str:
        # Normalize common label formats like "Date of Birth:" -> "Date of Birth"
        return (s or "").strip().rstrip(":")

    # Tables with headers or 2-col layout (also handle multiple pairs in one row)
    for tbl in soup.select("table"):
        for tr in tbl.select("tr"):
            ths = [th.get_text(strip=True) for th in tr.select("th")]
            tds = [td.get_text(strip=True) for td in tr.select("td")]
            if ths and tds:
                out[_clean_label(ths[0])] = tds[0] if tds else ""
            elif len(tds) == 2:
                out[_clean_label(tds[0])] = tds[1]
            elif len(tds) >= 4 and len(tds) % 2 == 0:
                # Pair consecutive TDs into label/value entries
                for i in range(0, len(tds) - 1, 2):
                    key = _clean_label(tds[i])
                    val = tds[i + 1]
                    if key:
                        out[key] = val

    # Definition lists
    for dl in soup.select("dl"):
        dts = dl.select("dt")
        dds = dl.select("dd")
        for dt, dd in zip(dts, dds):
            out[_clean_label(dt.get_text(strip=True))] = dd.get_text(strip=True)

    return out


def _split_last_first(full_name: str) -> tuple[str, str]:
    s = (full_name or "").strip()
    if not s:
        return "", ""
    if "," in s:
        last, first = [x.strip() for x in s.split(",", 1)]
        return last, first
    parts = s.split()
    if len(parts) >= 2:
        return parts[-1].strip(), " ".join(parts[:-1]).strip()
    return s, ""


@dataclass
class HCSOResult:
    dob: Optional[str]
    spn: Optional[str]
    source_url: Optional[str]
    raw_fields: Dict[str, Any]

    @property
    def found(self) -> bool:
        return bool(self.dob)


class HCSOClient:
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": os.getenv("HCSO_USER_AGENT", "Mozilla/5.0 (compatible; WarrantDB/0.2)"),
        })
        self.url_spn = os.getenv("HCSO_SPN_URL_FMT", "").strip()
        self.url_name = os.getenv("HCSO_NAME_URL_FMT", "").strip()
        try:
            self.timeout = float(os.getenv("HCSO_TIMEOUT_SEC", "20"))
        except Exception:
            self.timeout = 20.0
        try:
            self.throttle = float(os.getenv("HCSO_THROTTLE_SEC", "0.7"))
        except Exception:
            self.throttle = 0.7

    def _get(self, url: str) -> Optional[str]:
        if not url:
            return None
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if 200 <= resp.status_code < 300:
                time.sleep(self.throttle)
                return resp.text
            logger.warning("[HCSO] GET %s -> %s", url, resp.status_code)
            return None
        except Exception as e:
            logger.warning("[HCSO] GET error %s -> %s", url, e)
            return None

    def _parse_result(self, html: str, source_url: str | None) -> HCSOResult:
        kv = _label_value_parse(html or "")
        # Common key variants
        dob = kv.get("Date of Birth") or kv.get("DOB") or kv.get("Birth Date")
        spn = kv.get("SPN") or kv.get("SID/SPN") or kv.get("SID / SPN")
        if spn:
            spn = spn.strip()
        return HCSOResult(dob=dob, spn=spn, source_url=source_url, raw_fields=kv)

    def search_by_spn(self, spn: str) -> Optional[HCSOResult]:
        spn = (spn or "").strip()
        if not spn or not self.url_spn:
            return None
        url = self.url_spn.format(spn=spn)
        html = self._get(url)
        if not html:
            return None
        res = self._parse_result(html, url)
        # Enforce that returned page matches the requested SPN if present
        if res.spn and spn not in res.spn:
            return None
        return res

    def search_by_name(self, full_name: str) -> Optional[HCSOResult]:
        if not full_name or not self.url_name:
            return None
        last, first = _split_last_first(full_name)
        if not last:
            return None
        url = self.url_name.format(last=last, first=first)
        html = self._get(url)
        if not html:
            return None
        return self._parse_result(html, url)


def best_effort_lookup(full_name: str, spn: Optional[str] = None, client: Optional[HCSOClient] = None) -> Optional[HCSOResult]:
    """
    Try SPN first (if provided), otherwise search by name.
    Returns HCSOResult or None if not found/parsable.
    """
    c = client or HCSOClient()
    # 1) SPN search
    if spn:
        res = c.search_by_spn(spn)
        if res and res.found:
            return res
    # 2) Name search
    if full_name:
        res = c.search_by_name(full_name)
        if res and res.found:
            # If caller provided SPN, prefer matches that contain it
            if spn and res.spn and spn in res.spn:
                return res
            # Otherwise accept name-based result
            return res
    return None
