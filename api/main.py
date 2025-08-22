from fastapi import FastAPI, Query
from typing import Optional
from storage.mongo_client import get_db
from pydantic import BaseModel
from datetime import datetime
from fastapi import BackgroundTasks
from pathlib import Path
from dotenv import load_dotenv
from bson import ObjectId
import traceback, os
# make sure .env is loaded even when uvicorn runs from elsewhere
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

# Harris ingestion entrypoint
from ingestion.harris_inmate import run_harris_ingest

app = FastAPI(title="WarrantDB API", version="0.1.0")

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