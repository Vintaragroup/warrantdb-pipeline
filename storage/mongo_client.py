from pymongo import MongoClient
from dotenv import load_dotenv
from pathlib import Path
import os

# Load .env from repo root reliably, regardless of CWD
_ROOT_DOTENV = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=_ROOT_DOTENV)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "warrantdb")

_client = None

def get_client():
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI)
    return _client

def get_db():
    return get_client()[MONGO_DB]
