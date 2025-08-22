"""
Simple entity resolution placeholder.
"""
from typing import Dict, Any
from hashlib import sha1

def person_key(full_name: str, dob: str | None) -> str:
    key = f"{(full_name or '').upper()}|{dob or ''}"
    return "person:" + sha1(key.encode()).hexdigest()[:16]

def match_or_create(db, person_doc: Dict[str, Any]) -> str:
    q = {"full_name": person_doc["full_name"]}
    if person_doc.get("dob"):
        q["dob"] = person_doc["dob"]
    found = db.persons.find_one(q, {"_id": 1})
    if found:
        return str(found["_id"])
    res = db.persons.insert_one(person_doc)
    return str(res.inserted_id)
