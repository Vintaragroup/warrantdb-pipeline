"""
Create unique indexes to avoid duplicate custody events.
Safe to run multiple times.
"""
from storage.mongo_client import get_db
from pymongo import ASCENDING


def _db():
    return get_db()


db = _db()

# Avoid dupes: same person_id + source_url (preferred)
db.custody_events.create_index([("person_id", ASCENDING), ("source_url", ASCENDING)], unique=True, sparse=True)

# Fallback: same person_id + booking_number
db.custody_events.create_index([("person_id", ASCENDING), ("booking_number", ASCENDING)], unique=True, sparse=True)

print("custody_events unique indexes ensured (person_id+source_url, person_id+booking_number)")