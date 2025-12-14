from fastapi import FastAPI, Query, Request
from typing import Optional, List, Dict, Any
from storage.mongo_client import get_db
from pydantic import BaseModel
from datetime import datetime
from fastapi import BackgroundTasks
from pathlib import Path
from dotenv import load_dotenv
from bson import ObjectId
import traceback, os, logging
# make sure .env is loaded even when uvicorn runs from elsewhere
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

# Harris ingestion entrypoint
from ingestion.harris_inmate import run_harris_ingest

app = FastAPI(title="WarrantDB API", version="0.1.0")

# Minimal request logging to trace FE calls (method, path, querystring)
logger = logging.getLogger("api")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

@app.middleware("http")
async def log_requests(request: Request, call_next):
    try:
        qs = request.url.query
        logger.info("%s %s?%s", request.method, request.url.path, qs)
    except Exception:
        pass
    response = await call_next(request)
    return response

class PersonQuery(BaseModel):
    name: Optional[str] = None
    dob: Optional[str] = None  # YYYY-MM-DD
    spn: Optional[str] = None
    county: Optional[str] = None

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat() + "Z"}

@app.get("/person")
def get_person(
    name: Optional[str] = Query(default=None, description="Exact full_name match"),
    dob: Optional[str] = Query(default=None),
    spn: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None, description="Substring match on full_name"),
    limit: int = Query(default=25, ge=1, le=100)
):
    db = get_db()
    query = {}
    # Exact match (legacy)
    if name:
        query["full_name"] = {"$regex": f"^{name.strip()}$", "$options": "i"}
    # Substring match (new)
    if q:
        query["full_name"] = {"$regex": q.strip(), "$options": "i"}
    if dob:
        query["dob"] = dob
    if spn:
        query["identifiers.spn"] = spn

    persons = list(db.persons.find(query).limit(limit))
    for p in persons:
        p["_id"] = str(p["_id"])
    return {"results": persons}

def _summarize_alerts(alerts: dict) -> dict:
    # alerts = {"bond":{"Civil":[...],"Criminal":[...]}, "misfel": {...}, "nafiling": {...}}
    return {k: {g: len(v) for g, v in alerts.get(k, {}).items()} for k in ["bond","misfel","nafiling"]}

@app.post("/ingest/harris-now")
def ingest_harris_now():
    """
    Run Harris (Civil+Criminal × bond/misfel/nafiling) synchronously and return counts.
    """
    alerts = run_harris_ingest()
    return {"ok": True, "alerts": _summarize_alerts(alerts)}

@app.post("/ingest/harris")
def ingest_harris(background: BackgroundTasks):
    """
    Kick off Harris ingestion in the background and return immediately.
    """
    def _task():
        try:
            alerts = run_harris_ingest()
            print("[harris] alerts:", _summarize_alerts(alerts))
        except Exception as e:
            import traceback; traceback.print_exc()
    background.add_task(_task)
    return {"started": True, "pipeline": "harris"}

@app.post("/ingest/harris-now")
def ingest_harris_now():
    try:
        alerts = run_harris_ingest()
        return {"ok": True, "alerts": _summarize_alerts(alerts)}
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
            "env_check": {
                "MONGO_URI": bool(os.getenv("MONGO_URI")),
                "MONGO_DB": os.getenv("MONGO_DB"),
                "HARRIS_BASE_FILES_URL": os.getenv("HARRIS_BASE_FILES_URL"),
                "HARRIS_DATASETS_PAGE": os.getenv("HARRIS_DATASETS_PAGE"),
            },
        }
    
@app.get("/harris/summary")
def harris_summary():
    """
    Read-only dashboard data: counts per collection, today's counts, and needs_bond_help counts.
    """
    db = get_db()
    today = datetime.utcnow().date().isoformat()

    out = {}
    for coll in ["harris_bond", "harris_misfel", "harris_nafiling"]:
        total = db[coll].count_documents({})
        today_count = db[coll].count_documents({"last_seen_file_date": today})
        needs_help = db[coll].count_documents({"last_seen_file_date": today, "needs_bond_help": True})
        out[coll] = {
            "total": total,
            "today": today_count,
            "needs_bond_help": needs_help,
        }
    return {"date": today, "collections": out}


@app.get("/simple/harris/summary")
def simple_harris_summary():
    """
    Summary for simple_harris using canonical v2 buckets.

    Returns:
      - by_bucket_v2: array of { _id: bucket, count }
      - windows: 24h/48h/72h/7d/30d rollups using v2 buckets
      - coverage: total docs and fraction with booking_datetime and time_bucket_v2
    """
    db = get_db()
    coll = db["simple_harris"]

    # Coverage
    total = coll.count_documents({})
    with_bdt = coll.count_documents({"booking_datetime": {"$exists": True}})
    with_tbv2 = coll.count_documents({"time_bucket_v2": {"$exists": True}})

    # Counts by bucket v2
    buckets = list(coll.aggregate([
        {"$group": {"_id": {"$ifNull": ["$time_bucket_v2", "missing"]}, "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]))

    # Map to rollup windows
    def get(bname: str) -> int:
        for b in buckets:
            if b.get("_id") == bname:
                return int(b.get("count", 0))
        return 0

    b_0_24 = get("0_24h")
    b_24_48 = get("24_48h")
    b_48_72 = get("48_72h")
    b_3_7 = get("3d_7d")
    b_7_30 = get("7d_30d")
    b_30_60 = get("30d_60d")
    b_60_plus = get("60d_plus")

    windows = {
        "24h": b_0_24,
        "48h": b_24_48,
        "72h": b_48_72,
        "7d": b_0_24 + b_24_48 + b_48_72 + b_3_7,
        "30d": b_0_24 + b_24_48 + b_48_72 + b_3_7 + b_7_30,
    }

    coverage = {
        "total": total,
        "pct_booking_datetime": (with_bdt / total) if total else None,
        "pct_time_bucket_v2": (with_tbv2 / total) if total else None,
    }

    return {
        "date": datetime.utcnow().isoformat() + "Z",
        "by_bucket_v2": buckets,
        "windows": windows,
        "coverage": coverage,
    }


@app.get("/simple/harris/inmates")
def simple_harris_inmates(
    bucket_v2: Optional[str] = Query(default=None, description="Filter by time_bucket_v2 (e.g., 0_24h, 7d_30d)"),
    bucket: Optional[str] = Query(default=None, description="FE-friendly alias (e.g., 24h, 48h, 72h) -> mapped to bucket_v2"),
    window: Optional[str] = Query(default=None, description="Rollup window (24h, 48h, 72h, 7d, 30d, 60d)"),
    limit: int = Query(default=200, ge=1, le=1000),
    skip: int = Query(default=0, ge=0),
    sort: Optional[str] = Query(default="-booking_datetime", description="Sort field; prefix '-' for desc"),
):
    """
    Return a list of normalized Harris inmate rows from simple_harris for frontend consumption.
    Includes the address field carried from the raw feeds.
    """
    db = get_db()
    coll = db["simple_harris"]

    q: Dict[str, Any] = {"county": "harris"}

    # FE-friendly aliases
    alias_bucket = {
        "24h": "0_24h",
        "48h": "24_48h",
        "72h": "48_72h",
    }
    window_map = {
        "24h": ["0_24h"],
        "48h": ["24_48h"],
        "72h": ["48_72h"],
        "7d": ["0_24h", "24_48h", "48_72h", "3d_7d"],
        "30d": ["0_24h", "24_48h", "48_72h", "3d_7d", "7d_30d"],
        "60d": ["0_24h", "24_48h", "48_72h", "3d_7d", "7d_30d", "30d_60d"],
    }

    # Priority: explicit bucket_v2 > window > bucket alias
    if bucket_v2:
        q["time_bucket_v2"] = bucket_v2
    elif window and window.lower() in window_map:
        q["time_bucket_v2"] = {"$in": window_map[window.lower()]}
    elif bucket and bucket.lower() in alias_bucket:
        q["time_bucket_v2"] = alias_bucket[bucket.lower()]

    # Determine sort
    sort_field = "booking_datetime"
    direction = -1
    if sort:
        if sort.startswith("-"):
            sort_field = sort[1:]
            direction = -1
        elif sort.startswith("+"):
            sort_field = sort[1:]
            direction = 1
        else:
            sort_field = sort
            direction = 1

    projection = {
        "_id": 0,
        # identity
        "county": 1,
        "category": 1,
        "case_number": 1,
        "anchor": 1,
        # expose SPN for FE linkage/debugging
        "spn": 1,
        # display
        "full_name": 1,
        "dob": 1,
        "charge": 1,
        "status": 1,
        # bond
        "bond_amount": 1,
        "bond_label": 1,
        # booking & buckets
        "booking_datetime": 1,
        "booking_date_v2": 1,
        "time_bucket_v2": 1,
        # address passthrough
        "address": 1,
        # phones from roster enrichment (if set)
        "phone_nbr1": 1,
        "phone_nbr2": 1,
        "phone_nbr3": 1,
        # spn anomaly context (if present)
        "spn_flagged": 1,
        "spn_bad": 1,
        "spn_flag_reason": 1,
        # optional helpful fields
        "tags": 1,
        "normalized_at": 1,
    }

    docs: List[Dict[str, Any]] = list(
        coll.find(q, projection).sort([(sort_field, direction)]).skip(skip).limit(limit)
    )

    return {
        "count": len(docs),
        "skip": skip,
        "limit": limit,
        "items": docs,
    }


@app.get("/simple/harris/buckets")
def simple_harris_buckets():
    """
    Bucket breakdown for simple_harris using canonical v2 buckets.

    Returns:
      - by_bucket_v2: list in canonical order (0_24h → 60d_plus)
      - total: total simple_harris docs
    """
    db = get_db()
    coll = db["simple_harris"]

    # Aggregate counts by time_bucket_v2
    res = list(coll.aggregate([
        {"$group": {"_id": {"$ifNull": ["$time_bucket_v2", "missing"]}, "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]))

    order = ["0_24h", "24_48h", "48_72h", "3d_7d", "7d_30d", "30d_60d", "60d_plus", "missing"]
    by = {r["_id"]: int(r["count"]) for r in res}
    buckets = [{"bucket": b, "count": by.get(b, 0)} for b in order]

    total = sum(x["count"] for x in buckets if x["bucket"] != "missing")
    return {"by_bucket_v2": buckets, "total": total}