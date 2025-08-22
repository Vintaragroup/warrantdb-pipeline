# ingestion/base_scraper.py
from typing import Any, Dict
from pymongo.collection import Collection

class BaseScraper:
    def __init__(self, db):
        self.db = db

    def upsert_person(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Upsert a person using a stable key:
        1) _ext_id if present (preferred)
        2) booking number
        3) (full_name, dob)
        """
        doc = dict(doc)
        doc.pop("updated_at", None)

        key: Dict[str, Any] = {}
        if doc.get("_ext_id"):
            key["_ext_id"] = doc["_ext_id"]
        else:
            booking_list = (doc.get("identifiers") or {}).get("booking") or []
            if booking_list:
                key["identifiers.booking"] = booking_list[0]
            else:
                key["full_name"] = (doc.get("full_name") or "").strip()
                key["dob"] = doc.get("dob")

        if not key:
            return {"skipped": True, "reason": "missing upsert key"}

        res = self.db.persons.update_one(
            key,
            {"$set": doc, "$currentDate": {"updated_at": True}},
            upsert=True,
        )
        return {
            "inserted": bool(res.upserted_id),
            "matched": res.matched_count,
            "modified": res.modified_count,
            "_id": str(res.upserted_id) if res.upserted_id else None,
            "key": key,
        }