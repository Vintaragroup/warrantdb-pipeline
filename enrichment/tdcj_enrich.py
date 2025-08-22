# enrichment/tdcj_enrich.py
from __future__ import annotations

import os
import time
import argparse
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


USER_AGENT = "Mozilla/5.0 (compatible; WarrantDB/0.2)"

# IMPORTANT: use the InmateSearch app (the OffenderSearch site often errors)
TDCJ_BASE  = "https://inmate.tdcj.texas.gov/InmateSearch/"
TDCJ_START = TDCJ_BASE  # start here to establish proper base/session


# -----------------------
# HTML parsing utilities
# -----------------------
def _parse_label_table(html: str) -> Dict[str, str]:
    """
    Generic label/value extractor that works for many gov 'detail' pages.
    It looks for:
      1) tables with <th>/<td> or 2-column <td>/<td>
      2) <dl>/<dt>/<dd> pairs
    Returns a dict {Label -> Value}.
    """
    out: Dict[str, str] = {}
    soup = BeautifulSoup(html or "", "lxml")

    # Tables
    for tbl in soup.select("table"):
        rows = tbl.select("tr")
        for tr in rows:
            ths = [th.get_text(strip=True) for th in tr.select("th")]
            tds = [td.get_text(strip=True) for td in tr.select("td")]
            if ths and tds:
                out[ths[0]] = tds[0] if tds else ""
            elif len(tds) == 2:
                out[tds[0]] = tds[1]

    # Definition lists
    for dl in soup.select("dl"):
        dts = dl.select("dt")
        dds = dl.select("dd")
        for dt, dd in zip(dts, dds):
            out[dt.get_text(strip=True)] = dd.get_text(strip=True)

    return out


def _split_name(full_name: str) -> tuple[str, str]:
    """Split 'LAST, FIRST ...' or 'FIRST LAST' into (last, first)."""
    first = last = ""
    s = (full_name or "").strip()
    if not s:
        return "", ""
    if "," in s:
        last, first = [x.strip() for x in s.split(",", 1)]
    else:
        parts = s.split()
        if len(parts) >= 2:
            last = parts[-1].strip()
            first = " ".join(parts[:-1]).strip()
        else:
            last = s
    return last, first


# -----------------------
# Core lookup (Playwright)
# -----------------------
def tdcj_lookup(full_name: str, *, headful: bool | None = None, throttle_ms: int = 600, timeout_ms: int = 20000) -> Optional[Dict[str, Any]]:
    """
    Perform a single lookup on TDCJ InmateSearch and return extracted fields:
        {'dob','tdcj','sid','race','sex','source_url'} or None if not found.

    - Starts at TDCJ_START (required to set proper base/session)
    - Fills lastName + optional firstName
    - Submits, finds a detail link that stays under /InmateSearch/, navigates, parses
    """
    last, first = _split_name(full_name)
    if not last:
        return None

    if headful is None:
        headful = os.getenv("TDCJ_HEADFUL", "0").lower() in ("1", "true", "yes")

    # soft throttling defaults (can tweak via env)
    try:
        throttle_ms = int(os.getenv("TDCJ_THROTTLE_MS", str(throttle_ms)))
    except Exception:
        pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            ignore_https_errors=True,
        )
        page = ctx.new_page()

        try:
            # 1) establish app session
            page.goto(TDCJ_START, wait_until="domcontentloaded", timeout=timeout_ms)

            # 2) fill the search form
            # lastName (required)
            for sel in ("#lastName", "input[name='lastName']"):
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible():
                        loc.fill(last)
                        break
                except Exception:
                    continue

            # firstName (optional)
            if first:
                for sel in ("#firstName", "input[name='firstName']"):
                    try:
                        loc = page.locator(sel).first
                        if loc.is_visible():
                            loc.fill(first)
                            break
                    except Exception:
                        continue

            # 3) submit
            try:
                if page.locator("#search_btnSearch").first.is_visible():
                    page.click("#search_btnSearch")
                else:
                    page.keyboard.press("Enter")
            except Exception:
                page.keyboard.press("Enter")

            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            time.sleep(throttle_ms / 1000)

            # 4) locate an internal detail link (stays in /InmateSearch/)
            detail_href = None
            anchors = page.locator("a")
            for i in range(anchors.count()):
                a = anchors.nth(i)
                try:
                    href = a.get_attribute("href") or ""
                    if not href:
                        continue
                    hl = href.lower()
                    # prefer something that looks like a detail View inside the app
                    if ("inmatesearch/" in hl) and any(k in hl for k in ("view", "detail", "info", "offender")):
                        detail_href = href
                        break
                except Exception:
                    continue

            # Some result pages may already be a detail if the search is very specific
            if not detail_href:
                html = page.content()
                kv = _parse_label_table(html)
                if any(x in kv for x in ("Date of Birth", "DOB", "TDCJ Number", "SID Number", "SID")):
                    res = {
                        "dob":  kv.get("Date of Birth") or kv.get("DOB"),
                        "tdcj": kv.get("TDCJ Number") or kv.get("TDCJ #"),
                        "sid":  kv.get("SID Number")  or kv.get("SID"),
                        "race": kv.get("Race"),
                        "sex":  kv.get("Gender") or kv.get("Sex"),
                        "source_url": page.url,
                    }
                    ctx.close(); browser.close()
                    return res
                ctx.close(); browser.close()
                return None

            # normalize absolute
            if detail_href.startswith("/"):
                detail_href = "https://inmate.tdcj.texas.gov" + detail_href
            elif not detail_href.startswith("http"):
                detail_href = TDCJ_BASE + detail_href.lstrip("/")

            # 5) navigate to detail and parse
            page.goto(detail_href, wait_until="domcontentloaded", timeout=timeout_ms)
            time.sleep(throttle_ms / 1000)

            html = page.content()
            kv = _parse_label_table(html)

            res = {
                "dob":  kv.get("Date of Birth") or kv.get("DOB"),
                "tdcj": kv.get("TDCJ Number")   or kv.get("TDCJ #"),
                "sid":  kv.get("SID Number")    or kv.get("SID"),
                "race": kv.get("Race"),
                "sex":  kv.get("Gender")        or kv.get("Sex"),
                "source_url": detail_href,
            }
            ctx.close(); browser.close()
            return res

        except Exception:
            try:
                ctx.close(); browser.close()
            except Exception:
                pass
            return None


# -----------------------
# Batch enrichment runner
# -----------------------
def _update_person_doc(db, person_id, update: Dict[str, Any]) -> None:
    # prefer _ext_id anchor (your BaseScraper upsert uses that)
    q = {"_id": person_id}
    set_fields: Dict[str, Any] = {}
    add_to_set: Dict[str, Any] = {}

    if update.get("dob"):
        set_fields["dob"] = update["dob"]
    if update.get("tdcj"):
        set_fields["identifiers.tdcj"] = update["tdcj"]
    if update.get("sid"):
        set_fields["identifiers.sid"] = update["sid"]
    if update.get("race"):
        set_fields["race"] = update["race"]
    if update.get("sex"):
        set_fields["sex"] = update["sex"]
    if update.get("source_url"):
        add_to_set["links"] = {"rel": "tdcj_detail", "url": update["source_url"]}

    update_doc: Dict[str, Any] = {}
    if set_fields:
        update_doc["$set"] = set_fields
    if add_to_set:
        update_doc["$addToSet"] = add_to_set

    if update_doc:
        update_doc["$currentDate"] = {"updated_at": True}
        db.persons.update_one(q, update_doc)


def enrich_persons_from_tdcj(limit: int = 200, only_missing_dob: bool = True, name_prefix: Optional[str] = None, dry_run: bool = False) -> None:
    """
    Finds candidate persons and attempts TDCJ enrichment.
    - only_missing_dob=True → restrict to persons with dob == null
    - name_prefix="ADAMS"   → restrict by last-name prefix (optional)
    """
    from storage.mongo_client import get_db

    db = get_db()

    query: Dict[str, Any] = {}
    if only_missing_dob:
        query["dob"] = None
    if name_prefix:
        # naive last-name startswith: works because your names are in "LAST, FIRST" form
        query["full_name"] = {"$regex": f"^{name_prefix.upper()},", "$options": "i"}

    cur = db.persons.find(query).limit(limit)

    tried = 0
    hits = 0
    for p in cur:
        tried += 1
        name = p.get("full_name") or ""
        if not name:
            continue

        print(f"[TDCJ] Lookup: {name} (id={p.get('_id')})")
        res = tdcj_lookup(name)
        if not res:
            print("  → no match")
            continue

        print(f"  → hit: dob={res.get('dob')} tdcj={res.get('tdcj')} sid={res.get('sid')} url={res.get('source_url')}")
        hits += 1

        if not dry_run:
            _update_person_doc(db, p["_id"], res)

        # polite pause between people
        time.sleep(float(os.getenv("TDCJ_BETWEEN_PEOPLE_SEC", "0.8")))

    print(f"[TDCJ] Done. Tried={tried} | Hits={hits} | DryRun={dry_run}")


# -----------------------
# CLI
# -----------------------
def _main():
    ap = argparse.ArgumentParser(description="TDCJ enrichment runner")
    ap.add_argument("--limit", type=int, default=int(os.getenv("TDCJ_LIMIT", "200")), help="Max persons to try")
    ap.add_argument("--all", action="store_true", help="Include persons that already have a DOB")
    ap.add_argument("--prefix", type=str, default=os.getenv("TDCJ_NAME_PREFIX", ""), help="Restrict by last-name prefix (e.g. ADAMS)")
    ap.add_argument("--dry-run", action="store_true", help="Do not write to DB")
    args = ap.parse_args()

    enrich_persons_from_tdcj(
        limit=args.limit,
        only_missing_dob=(not args.all),
        name_prefix=(args.prefix or None),
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    _main()