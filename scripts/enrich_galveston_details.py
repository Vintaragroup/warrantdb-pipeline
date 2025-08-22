# scripts/enrich_galveston_details.py
import os, re, asyncio, httpx
from datetime import datetime
from bs4 import BeautifulSoup
from storage.mongo_client import get_db

BASE = "https://p2c.galvestoncountytx.gov"
UA = {"User-Agent": "Mozilla/5.0 (compatible; WarrantDB/0.2)"}
TIMEOUT = 30.0

def _abs(url: str) -> str:
    return url if (not url or url.startswith("http")) else f"{BASE}/{url.lstrip('/')}"

def _norm(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", (s or "").strip())

def _extract_detail_hrefs_from_roster_json(doc):
    detail_urls = set()
    rows = (doc or {}).get("rows") or []
    href_re = re.compile(r'href="([^"]+)"', re.I)
    for row in rows:
        for cell in row.get("cell") or []:
            if not isinstance(cell, str):
                continue
            m = href_re.search(cell)
            if m:
                detail_urls.add(_abs(m.group(1)))
    return list(detail_urls)

async def fetch_detail(session: httpx.AsyncClient, url: str):
    try:
        r = await session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "lxml")

    def txt(sel: str) -> str:
        el = soup.select_one(sel)
        return (el.get_text(strip=True) if el else "").strip()

    name = txt("#mainContent_CenterColumnContent_lblName")
    if not name:
        return None

    age = txt("#mainContent_CenterColumnContent_lblAge")
    race = txt("#mainContent_CenterColumnContent_lblRace")
    sex  = txt("#mainContent_CenterColumnContent_lblSex")
    arrest_date = txt("#mainContent_CenterColumnContent_lblArrestDate")
    agency = txt("#mainContent_CenterColumnContent_lblAgency")
    total_bond = txt("#mainContent_CenterColumnContent_lblTotalBoundAmount")

    charges = []
    for tr in soup.select("#mainContent_CenterColumnContent_dgMainResults tbody tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) >= 4:
            charges.append({
                "charge": tds[0].get_text(strip=True),
                "status": tds[1].get_text(strip=True),
                "docket": tds[2].get_text(strip=True),
                "bond":   tds[3].get_text(strip=True),
            })

    img = soup.select_one("#mainContent_CenterColumnContent_imgPhoto")
    mug_url = _abs(img["src"]) if (img and img.get("src")) else None

    return {
        "detail_url": url,
        "mugshot_url": mug_url,
        "charges": charges,
        "attrs": {
            "age": age, "race": race, "sex": sex,
            "arrest_date": arrest_date, "agency": agency,
            "total_bond": total_bond,
        }
    }

async def main():
    db = get_db()
    # Find the latest jqGrid JSON debug doc to re-use detail links
    dbg = db.custody_events.find_one(
        {"status": {"$regex": "^DEBUG_JQGRID_JSON", "$options":"i"}},
        sort=[("_id", -1)]
    )
    detail_urls = _extract_detail_hrefs_from_roster_json(dbg) if dbg else []
    if not detail_urls:
        print("No detail URLs found from DEBUG_JQGRID_JSON. Nothing to enrich.")
        return

    print(f"Enriching {len(detail_urls)} detail pages…")

    concurrency = int(os.getenv("CONCURRENCY", "30"))
    sem = asyncio.Semaphore(concurrency)
    updated = 0

    async with httpx.AsyncClient(headers=UA, verify=False, timeout=TIMEOUT) as session:
        async def worker(url):
            nonlocal updated
            async with sem:
                rec = await fetch_detail(session, url)
                if not rec:
                    return
                # Upsert against persons by link
                db.persons.update_many(
                    {"links": {"$elemMatch": {"rel":"p2c_detail", "url": url}}},
                    {"$set": {
                        "media": ([{"rel":"mugshot","url": rec["mugshot_url"]}] if rec["mugshot_url"] else []),
                        "updated_at": datetime.utcnow()
                    }}
                )
                # Update matching events by source_url
                db.custody_events.update_many(
                    {"source_url": url},
                    {"$set": {
                        "charges": rec["charges"],
                        "agency": rec["attrs"]["agency"],
                        "arrest_date": rec["attrs"]["arrest_date"],
                        "total_bond": rec["attrs"]["total_bond"],
                        "updated_at": datetime.utcnow()
                    }}
                )
                updated += 1

        await asyncio.gather(*(worker(u) for u in detail_urls))

    print(f"Done. Updated {updated} records.")

if __name__ == "__main__":
    asyncio.run(main())