#!/usr/bin/env python3
"""
Back-fill mugshots for Galveston P2C persons already in Mongo.

Modes (choose via env):
- SKIP_MUGSHOTS=true     -> no-op (default false)
- MUGSHOT_SAVE=url       -> store URL only (DEFAULT)
- MUGSHOT_SAVE=bytes     -> store base64 inline on the person doc
- MUGSHOT_SAVE=gridfs    -> store file in GridFS and reference _id

Other env:
- CONCURRENCY=30         -> parallel HTTP fetches
- SCRAPER_VERIFY_SSL=false|true (false recommended locally)
"""

from __future__ import annotations
import asyncio
import base64
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import certifi
import httpx
from bs4 import BeautifulSoup
from bson import ObjectId
from pymongo import UpdateOne
from gridfs import GridFS

# Reuse your existing DB helper
from storage.mongo_client import get_db

BASE = "https://p2c.galvestoncountytx.gov"
UA = {"User-Agent": "Mozilla/5.0 (compatible; WarrantDB/0.2)"}
TIMEOUT = 30.0

def _verify() -> Any:
    v = os.getenv("SCRAPER_VERIFY_SSL", "false").strip().lower() in ("1", "true", "yes")
    return certifi.where() if v else False

MODE = os.getenv("MUGSHOT_SAVE", "url").strip().lower()  # 'url' | 'bytes' | 'gridfs'
CONCURRENCY = int(os.getenv("CONCURRENCY", "30"))
SKIP = os.getenv("SKIP_MUGSHOTS", "false").strip().lower() in ("1", "true", "yes")

def _abs_url(src: str) -> str:
    src = (src or "").strip()
    if not src:
        return ""
    return src if src.startswith("http") else f"{BASE}/{src.lstrip('/')}"

def _parse_detail_for_mugshot(html: str) -> Optional[str]:
    soup = BeautifulSoup(html or "", "lxml")
    img = soup.select_one("#mainContent_CenterColumnContent_imgPhoto")
    if not img or not img.get("src"):
        return None
    return _abs_url(img["src"])

async def _fetch_detail_and_img(
    client: httpx.AsyncClient,
    detail_url: str,
    want_bytes: bool
) -> Tuple[Optional[str], Optional[bytes]]:
    # 1) get detail page
    try:
        r = await client.get(detail_url, timeout=TIMEOUT)
        r.raise_for_status()
        mug_url = _parse_detail_for_mugshot(r.text)
    except Exception:
        return None, None

    if not mug_url:
        return None, None

    if not want_bytes:
        return mug_url, None

    # 2) fetch image bytes
    try:
        img = await client.get(mug_url, timeout=TIMEOUT)
        img.raise_for_status()
        return mug_url, img.content
    except Exception:
        return mug_url, None  # keep URL even if bytes failed

def _already_has_mugshot(person: Dict[str, Any]) -> bool:
    media = person.get("media") or []
    return any((m.get("rel") == "mugshot") for m in media)

def _detail_link(person: Dict[str, Any]) -> Optional[str]:
    for lk in person.get("links") or []:
        if lk.get("rel") == "p2c_detail" and lk.get("url"):
            return lk["url"]
    return None

async def main():
    if SKIP:
        print("SKIP_MUGSHOTS=true -> nothing to do.")
        return

    db = get_db()
    persons = db["persons"]
    events = db["custody_events"]
    fs = GridFS(db) if MODE == "gridfs" else None

    # Find targets: Galveston persons with a P2C detail link but no mugshot yet
    cursor = persons.find(
        {
            "links.rel": "p2c_detail",
            "media": {"$not": {"$elemMatch": {"rel": "mugshot"}}},
        },
        {"_id": 1, "links": 1}
    )

    targets: List[Tuple[ObjectId, str]] = []
    async for _ in _aiter_cursor(cursor):
        # _aiter_cursor wraps a sync cursor; see helper below
        pass  # placeholder to satisfy linter

    # Build list synchronously (PyMongo cursor is synchronous)
    targets = []
    for p in cursor:
        url = _detail_link(p)
        if url:
            targets.append((p["_id"], url))

    if not targets:
        print("No persons need mugshots. Done.")
        return

    print(f"Backfilling mugshots for {len(targets)} persons in mode={MODE}...")

    want_bytes = MODE in ("bytes", "gridfs")
    updates: List[UpdateOne] = []

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(headers=UA, verify=_verify(), timeout=TIMEOUT) as client:
        async def worker(pid: ObjectId, detail_url: str):
            async with sem:
                mug_url, img_bytes = await _fetch_detail_and_img(client, detail_url, want_bytes)
                if not mug_url:
                    return

                media_doc: Dict[str, Any] = {"rel": "mugshot"}

                if MODE == "url":
                    media_doc["url"] = mug_url

                elif MODE == "bytes":
                    if img_bytes:
                        media_doc["data_b64"] = base64.b64encode(img_bytes).decode("ascii")
                        media_doc["content_type"] = "image/jpeg"
                        media_doc["source_url"] = mug_url
                    else:
                        media_doc["url"] = mug_url  # fallback

                elif MODE == "gridfs":
                    if img_bytes and fs is not None:
                        grid_id = fs.put(img_bytes, filename=f"mugshot_{pid}.jpg", content_type="image/jpeg")
                        media_doc["gridfs_id"] = grid_id
                        media_doc["source_url"] = mug_url
                    else:
                        media_doc["url"] = mug_url  # fallback

                updates.append(
                    UpdateOne(
                        {"_id": pid},
                        {"$addToSet": {"media": media_doc}}
                    )
                )

                # Optional: also surface on latest custody_event for convenience
                latest_evt = events.find_one(
                    {"source_url": detail_url},
                    sort=[("_id", -1)],
                    projection={"_id": 1}
                )
                if latest_evt:
                    events.update_one(
                        {"_id": latest_evt["_id"]},
                        {"$set": {"mugshot_url": mug_url, "mugshot_mode": MODE}}
                    )

        await asyncio.gather(*(worker(pid, url) for pid, url in targets))

    # Bulk write in batches to avoid too-large ops
    BATCH = 500
    total = 0
    for i in range(0, len(updates), BATCH):
        res = persons.bulk_write(updates[i:i+BATCH], ordered=False)
        total += (res.modified_count or 0) + (res.upserted_count or 0)

    print(f"Backfill complete. Persons updated: ~{total}")

# Small helper so code above can await if we ever switch to Motor;
# for now we just keep it to satisfy the `async for` structure.
async def _aiter_cursor(_cursor):
    return
    yield  # never reached

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError:
        # If something else already has a running loop, fallback
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())