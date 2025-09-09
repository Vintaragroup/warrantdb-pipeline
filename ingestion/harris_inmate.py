#!/usr/bin/env python3
import os, re, csv, io, datetime as dt
from typing import Any, Dict, List, Tuple
import requests
from pymongo import MongoClient, UpdateOne, ASCENDING

# add these:
from pathlib import Path
from dotenv import load_dotenv          # pip install python-dotenv
from bs4 import BeautifulSoup           # pip install beautifulsoup4

# load .env from repo root
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

ASSIST_DENY_NOTES = {"BOND DENIED", "UNSECURED GOB ELIGIBLE"}
GROUPS = ["Civil", "Criminal"]
KINDS  = ["bond", "misfel", "nafiling"]

# Harris endpoints (use os.getenv here so we don't depend on _env ordering)
FILES_BASE = os.getenv("HARRIS_BASE_FILES_URL", "https://www.hcdistrictclerk.com/Common/e-services/Files").rstrip("/")
PAGE_URL   = os.getenv("HARRIS_DATASETS_PAGE", "https://www.hcdistrictclerk.com/Common/e-services/PublicDatasets.aspx")

def _env(k: str, d: str | None = None) -> str | None:
    v = os.getenv(k, d)
    return v.strip() if isinstance(v, str) and v else v

def _today_iso() -> str:
    return dt.date.today().isoformat()

def _to_int(s: str | None) -> int | None:
    if not s: return None
    s = s.replace(",", "").strip()
    return int(s) if s.isdigit() else None

def _parse_yymmdd(s: str | None) -> str | None:
    if not s or len(s) != 6 or not s.isdigit(): return None
    mm, dd, yy = s[:2], s[2:4], s[4:]
    year = int(yy) + (2000 if int(yy) < 70 else 1900)
    try:
        return dt.date(year, int(mm), int(dd)).isoformat()
    except Exception:
        return None

def _within_days(start_iso: str | None, days: int) -> bool:
    if not start_iso: return False
    y, m, d = [int(x) for x in start_iso.split("-")]
    return (dt.date.today() - dt.date(y, m, d)).days <= days

def _addr_line(parts: List[str | None]) -> str | None:
    line = " ".join([p for p in parts if p]).strip()
    return line or None

def _needs_bond_help(bond_amount: int | None, bond_note: str | None) -> bool:
    if not bond_amount or bond_amount <= 0: return False
    note = (bond_note or "").upper()
    if any(x in note for x in ASSIST_DENY_NOTES): return False
    return True

def _fetch_text(url: str) -> str:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content.decode("utf-8", errors="replace")

def _parse_rows(text: str) -> List[List[str]]:
    out = []
    reader = csv.reader(io.StringIO(text), delimiter=",")
    for row in reader:
        row = [x.strip() for x in row]
        if row and row[-1] == "":  # drop trailing field from terminal ';'
            row = row[:-1]
        if any(c for c in row):
            out.append(row)
    return out

# --- Enhanced booking categorization ---
def _calculate_booking_age_category(file_date_iso: str) -> str:
    """Calculate how long ago someone was booked based on file date."""
    if not file_date_iso:
        return "unknown"
    
    try:
        file_date = dt.datetime.fromisoformat(file_date_iso.replace("Z", "")).date()
        current_date = dt.datetime.utcnow().date()
        days_diff = (current_date - file_date).days
        
        if days_diff < 0:
            return "future_date"
        elif days_diff <= 1:
            return "24_hours_or_less"
        elif days_diff <= 30:
            return "0_to_30_days"
        elif days_diff <= 60:
            return "30_to_60_days"
        elif days_diff <= 180:
            return "60_to_180_days"
        elif days_diff <= 365:
            return "180_to_365_days"
        else:
            return "365_days_or_older"
    except Exception as e:
        print(f"[harris] Error calculating booking age: {e}")
        return "unknown"

def _get_booking_priority(booking_age_category: str) -> int:
    """Get priority ranking based on booking age (1 = highest priority)."""
    priority_map = {
        "24_hours_or_less": 1,
        "0_to_30_days": 2,
        "30_to_60_days": 3,
        "60_to_180_days": 4,
        "180_to_365_days": 5,
        "365_days_or_older": 6,
        "unknown": 7,
        "future_date": 8
    }
    return priority_map.get(booking_age_category, 7)

# ------------------------------
# NEW SECTION: HTTP session + tokens
# ------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s

def _get_webforms_tokens(html: str) -> dict:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    def val(id_):
        el = soup.find("input", {"id": id_})
        return el.get("value") if el else ""

    tokens = {
        "__VIEWSTATE": val("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": val("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": val("__EVENTVALIDATION"),
    }
    tokens["_hidden_name"] = "hiddenDownloadFile"
    tokens["_button_name"] = (
        "ctl00$ctl00$ctl00$ContentPlaceHolder1$ContentPlaceHolder2$ContentPlaceHolder2$buttonDownload"
    )
    return tokens

def _download_via_webforms(sess: requests.Session, rel_path: str) -> str:
    """
    Emulate clicking the Download link by posting back to the page with:
      hiddenDownloadFile=<rel_path with backslashes>
      buttonDownload=<clicked>
    """
    if not PAGE_URL:
        raise RuntimeError("HARRIS_DATASETS_PAGE not set")

    # 1) GET page to get cookies + tokens
    r = sess.get(PAGE_URL, timeout=60)
    r.raise_for_status()
    toks = _get_webforms_tokens(r.text)

    # 2) POST back with hidden + button
    data = {
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": toks["__VIEWSTATE"],
        "__VIEWSTATEGENERATOR": toks["__VIEWSTATEGENERATOR"],
        "__EVENTVALIDATION": toks["__EVENTVALIDATION"],
        toks["_hidden_name"]: rel_path.replace("/", "\\"),
        toks["_button_name"]: "",
    }
    r2 = sess.post(PAGE_URL, data=data, timeout=90)
    r2.raise_for_status()
    return r2.content.decode("utf-8", errors="replace")

# ---------- Enhanced Parsers ----------

def parse_bond(rows: List[List[str]], file_date: str, group: str) -> List[Dict[str, Any]]:
    docs = []
    for c in rows:
        doc = {
            "source": f"harris_{group.lower()}_bond",
            "file_date": file_date,
            "court_group": c[0] if len(c)>0 else None,
            "case_number": c[1] if len(c)>1 else None,
            "offense": c[2] if len(c)>2 else None,
            "court_no": c[3] if len(c)>3 else None,
            "last_name": c[4] if len(c)>4 else None,
            "first_middle": c[5] if len(c)>5 else None,
            "spn": (c[6] if len(c)>6 else "").strip(),
            "race_code": c[7] if len(c)>7 else None,
            "sex_code": c[8] if len(c)>8 else None,
            "bond_amount": _to_int(c[9] if len(c)>9 else None),
            "bond_note": c[10] if len(c)>10 else None,
            "address": {
                "line1": _addr_line([
                    c[11] if len(c)>11 else None,
                    c[12] if len(c)>12 else None,
                    c[13] if len(c)>13 else None,
                    c[14] if len(c)>14 else None,
                    c[15] if len(c)>15 else None]),
                "city": c[16] if len(c)>16 else None,
                "zip": c[17] if len(c)>17 else None,
            },
            "group": group,
            "scraped_at": dt.datetime.now(dt.timezone.utc),
        }
        doc["name"] = ", ".join([x for x in [doc["last_name"], doc["first_middle"]] if x]) or None
        doc["needs_bond_help"] = _needs_bond_help(doc["bond_amount"], doc["bond_note"])
        
        # Add booking categorization
        doc["booking_age_category"] = _calculate_booking_age_category(file_date)
        doc["booking_priority"] = _get_booking_priority(doc["booking_age_category"])
        
        if doc["spn"] or doc["case_number"]:
            docs.append(doc)
    return docs

def parse_misfel(rows: List[List[str]], file_date: str, group: str) -> List[Dict[str, Any]]:
    docs = []
    for c in rows:
        doc = {
            "source": f"harris_{group.lower()}_misfel",
            "file_date": file_date,
            "name": c[0] if len(c)>0 else None,
            "dob": _parse_yymmdd(c[1] if len(c)>1 else None),
            "spn": (c[2] if len(c)>2 else "").strip(),
            "bond_amount": _to_int(c[3] if len(c)>3 else None),
            "bond_note": c[4] if len(c)>4 else None,
            "case_date": _parse_yymmdd(c[5] if len(c)>5 else None),
            "court_group": c[6] if len(c)>6 else None,
            "case_number": c[7] if len(c)>7 else None,
            "offense": c[8] if len(c)>8 else None,
            "address": {
                "line1": _addr_line([
                    c[9] if len(c)>9 else None,
                    c[10] if len(c)>10 else None,
                    c[11] if len(c)>11 else None]),
                "city": c[12] if len(c)>12 else None,
                "state": c[13] if len(c)>13 else None,
                "zip": c[14] if len(c)>14 else None,
                "phone": c[15] if len(c)>15 else None,
            },
            "group": group,
            "scraped_at": dt.datetime.now(dt.timezone.utc),
        }
        doc["needs_bond_help"] = _needs_bond_help(doc["bond_amount"], doc["bond_note"])
        
        # Add booking categorization
        doc["booking_age_category"] = _calculate_booking_age_category(file_date)
        doc["booking_priority"] = _get_booking_priority(doc["booking_age_category"])
        
        if doc["spn"] or doc["case_number"]:
            docs.append(doc)
    return docs

def parse_nafiling(rows: List[List[str]], file_date: str, group: str) -> List[Dict[str, Any]]:
    docs = []
    for c in rows:
        doc = {
            "source": f"harris_{group.lower()}_nafiling",
            "file_date": file_date,
            "court_group": c[0] if len(c)>0 else None,
            "case_number": c[1] if len(c)>1 else None,
            "offense": c[2] if len(c)>2 else None,
            "court_no": c[3] if len(c)>3 else None,
            "last_name": c[4] if len(c)>4 else None,
            "first_middle": c[5] if len(c)>5 else None,
            "spn": (c[6] if len(c)>6 else "").strip(),
            "filing_flag": c[7] if len(c)>7 else None,
            "aux_flag": c[8] if len(c)>8 else None,
            "bond_amount": _to_int(c[9] if len(c)>9 else None),
            "bond_note": c[10] if len(c)>10 else None,
            "address": {
                "line1": _addr_line([
                    c[11] if len(c)>11 else None,
                    c[12] if len(c)>12 else None,
                    c[13] if len(c)>13 else None,
                    c[14] if len(c)>14 else None,
                    c[15] if len(c)>15 else None]),
                "city": c[16] if len(c)>16 else None,
                "zip": c[17] if len(c)>17 else None,
            },
            "group": group,
            "scraped_at": dt.datetime.now(dt.timezone.utc),
        }
        doc["name"] = ", ".join([x for x in [doc["last_name"], doc["first_middle"]] if x]) or None
        doc["needs_bond_help"] = _needs_bond_help(doc["bond_amount"], doc["bond_note"])
        
        # Add booking categorization
        doc["booking_age_category"] = _calculate_booking_age_category(file_date)
        doc["booking_priority"] = _get_booking_priority(doc["booking_age_category"])
        
        if doc["spn"] or doc["case_number"]:
            docs.append(doc)
    return docs

# ---------- Mongo helpers ----------

def _get_cols(db):
    name_b = _env("HARRIS_COLL_BOND", "harris_bond")
    name_m = _env("HARRIS_COLL_MISFEL", "harris_misfel")
    name_n = _env("HARRIS_COLL_NAFILING", "harris_nafiling")
    col_b, col_m, col_n = db[name_b], db[name_m], db[name_n]
    for col in (col_b, col_m, col_n):
        try:
            col.create_index([("spn", ASCENDING)], background=True)
            col.create_index([("case_number", ASCENDING)], background=True)
            col.create_index([("first_seen_file_date", ASCENDING)], background=True)
            col.create_index([("last_seen_file_date", ASCENDING)], background=True)
            col.create_index([("group", ASCENDING)], background=True)
            # Enhanced indexes
            col.create_index([("booking_age_category", ASCENDING)], background=True)
            col.create_index([("booking_priority", ASCENDING)], background=True)
            col.create_index([("scraped_at", ASCENDING)], background=True)
        except Exception:
            pass
    return col_b, col_m, col_n

def _bulk_upsert(col, docs: List[Dict[str, Any]], file_date: str) -> Tuple[int, int]:
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    ops = []
    for d in docs:
        spn  = (d.get("spn") or "").strip()
        case = (d.get("case_number") or "").strip()
        if spn and case:
            filt = {"spn": spn, "case_number": case, "group": d.get("group")}
        else:
            filt = {"spn": spn or None, "case_number": case or None, "file_date": file_date, "group": d.get("group")}
        update = {
            "$set": {**d, "updated_at": now_iso, "last_seen_file_date": file_date},
            "$setOnInsert": {"first_seen_at": now_iso, "first_seen_file_date": file_date},
            "$push": {"history": {"ts": now_iso, "bond_amount": d.get("bond_amount"),
                                  "bond_note": d.get("bond_note"), "file_date": file_date,
                                  "source": d.get("source")}}
        }
        ops.append(UpdateOne(filt, update, upsert=True))
    if not ops: return (0, 0)
    res = col.bulk_write(ops, ordered=False)
    return (len(docs), (res.upserted_count or 0) + (res.modified_count or 0))

def _new_entries(col, file_date: str, window_days: int, group: str) -> List[Dict[str, Any]]:
    q = {"last_seen_file_date": file_date, "needs_bond_help": True, "group": group,
         "first_seen_file_date": {"$exists": True}}
    out: List[Dict[str, Any]] = []
    for doc in col.find(q).limit(5000):
        if _within_days(doc.get("first_seen_file_date"), window_days):
            out.append({
                "group": group,
                "spn": doc.get("spn"),
                "case_number": doc.get("case_number"),
                "name": doc.get("name") or f"{doc.get('last_name','')}, {doc.get('first_middle','')}",
                "offense": doc.get("offense"),
                "bond_amount": doc.get("bond_amount"),
                "bond_note": doc.get("bond_note"),
                "first_seen_file_date": doc.get("first_seen_file_date"),
                "booking_age_category": doc.get("booking_age_category", "unknown")
            })
    return out

# ---------- Download strategy ----------

def _discover_latest_paths_from_page() -> Dict[str, str]:
    """
    Return most-recent RELATIVE path for each of:
      Civil|Criminal × bond|misfel|nafiling
    """
    import re
    if not PAGE_URL:
        raise RuntimeError("HARRIS_DATASETS_PAGE not set")

    html = _fetch_text(PAGE_URL)

    # JS calls like DownloadDoc('Civil\\08-17-25-bond.txt')
    paths = re.findall(r"DownloadDoc\('([^']+\.txt)'\)", html, flags=re.IGNORECASE)
    paths = [p.replace("\\", "/") for p in paths]
    # normalize accidental double slashes and any leading slash
    paths = [re.sub(r"/+", "/", p).lstrip("/") for p in paths]

    # Fallback: scan the Civil/Criminal sections if JS not present
    if not paths:
        blocks = {
            "Civil": re.search(r"Public Datasets.*?Civil(.*?)(?:Criminal|$)", html, flags=re.S|re.I),
            "Criminal": re.search(r"Criminal(.*)$", html, flags=re.S|re.I),
        }
        for grp, m in blocks.items():
            if not m:
                continue
            text = m.group(1)
            for kind in KINDS:
                mfile = re.search(r"(\d{2}-\d{2}-\d{2}-" + kind + r"\.txt)", text, flags=re.I)
                if mfile:
                    paths.append(f"{grp}/{mfile.group(1)}")

    latest: Dict[str, str] = {}
    for g in GROUPS:
        for k in KINDS:
            match = next(
                (p for p in paths if p.lower().startswith(g.lower()+"/") and p.lower().endswith(f"{k}.txt")),
                None
            )
            if match:
                latest[f"{g}/{k}"] = match  # REL path

    want = [f"{g}/{k}" for g in GROUPS for k in KINDS]
    if len(latest) != 6:
        missing = [k for k in want if k not in latest]
        raise RuntimeError(f"Could not discover latest datasets from page. Missing: {missing}")

    return latest

def _fetch_six_files_latest() -> Dict[str, str]:
    """
    For each of the 6 keys, try direct file URL under /Files/<rel>.
    If that fails for any reason, fall back to WebForms POST.
    Only raise if BOTH methods fail.
    """
    rels = _discover_latest_paths_from_page()  # key -> 'Civil/08-17-25-bond.txt'
    out: Dict[str, str] = {}
    sess = _session()

    for key, rel in rels.items():
        direct_url = f"{FILES_BASE}/{rel}"

        # 1) try direct
        txt = None
        try:
            txt = _fetch_text(direct_url)
        except Exception:
            txt = None  # swallow; we'll try WebForms

        if txt is None:
            # 2) fall back to WebForms click
            try:
                txt = _download_via_webforms(sess, rel)
            except Exception as e:
                raise RuntimeError(f"Failed to fetch {key} via direct and WebForms: rel={rel}") from e

        out[key] = txt

    return out

# ---------- Public entrypoint ----------

def _already_have_latest(db, latest_by_key: Dict[str, str]) -> bool:
    """
    Return True iff for each of the 6 keys we already have at least one doc
    with matching `source` and `source_filename_date` in the correct collection.
    """
    # map key -> (collection_name_env_var_default, kind)
    key_to_col = {
        "Civil/bond":     (_env("HARRIS_COLL_BOND", "harris_bond"), "bond"),
        "Civil/misfel":   (_env("HARRIS_COLL_MISFEL", "harris_misfel"), "misfel"),
        "Civil/nafiling": (_env("HARRIS_COLL_NAFILING", "harris_nafiling"), "nafiling"),
        "Criminal/bond":     (_env("HARRIS_COLL_BOND", "harris_bond"), "bond"),
        "Criminal/misfel":   (_env("HARRIS_COLL_MISFEL", "harris_misfel"), "misfel"),
        "Criminal/nafiling": (_env("HARRIS_COLL_NAFILING", "harris_nafiling"), "nafiling"),
    }
    # map key -> source value we write during parsing
    key_to_source = {
        "Civil/bond":     "harris_civil_bond",
        "Civil/misfel":   "harris_civil_misfel",
        "Civil/nafiling": "harris_civil_nafiling",
        "Criminal/bond":     "harris_criminal_bond",
        "Criminal/misfel":   "harris_criminal_misfel",
        "Criminal/nafiling": "harris_criminal_nafiling",
    }

    for key, fname_date in latest_by_key.items():
        if not fname_date:
            return False
        col_name, _ = key_to_col[key]
        source_val   = key_to_source[key]
        col = db[col_name]
        if not col.find_one({"source": source_val, "source_filename_date": fname_date}):
            return False
    return True

def run_harris_ingest(file_date_iso: str | None = None) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    file_date = file_date_iso or _today_iso()

    # Discover which 6 files are currently the latest (absolute URLs).
    urls_by_key = _discover_latest_paths_from_page()  # e.g., {"Civil/bond": ".../Civil/08-21-25-bond.txt", ...}

    # Pull out filename dates like "08-21-25" per key for idempotence check.
    def _fname_date(source_url: str) -> str | None:
        m = re.search(r"/(\d{2}-\d{2}-\d{2})-", source_url)
        return m.group(1) if m else None

    latest_by_key = {key: _fname_date(url) for key, url in urls_by_key.items()}

    # DB client (used both for idempotence check and the upserts later)
    client = MongoClient(_env("MONGO_URI", "mongodb://localhost:27017"))
    db     = client[_env("MONGO_DB", "warrantdb")]

    # --- Idempotence: if DB already has all 6 with this filename date, skip work.
    if _already_have_latest(db, latest_by_key):
        return {
            "bond":     {g: [] for g in GROUPS},
            "misfel":   {g: [] for g in GROUPS},
            "nafiling": {g: [] for g in GROUPS},
        }

    # Get the latest 6 files (tries direct URLs; falls back to WebForms)
    six = _fetch_six_files_latest()

    parsed = {"bond": [], "misfel": [], "nafiling": []}
    booking_categories = {"bond": {}, "misfel": {}, "nafiling": {}}
    
    for g in GROUPS:
        # bond
        rows = _parse_rows(six[f"{g}/bond"])
        docs = parse_bond(rows, file_date, g)
        for d in docs:
            d["source_url"] = urls_by_key[f"{g}/bond"]
            d["source_filename_date"] = latest_by_key[f"{g}/bond"]
            # Track categories
            cat = d.get("booking_age_category", "unknown")
            booking_categories["bond"][cat] = booking_categories["bond"].get(cat, 0) + 1
        parsed["bond"].extend(docs)

        # misfel
        rows = _parse_rows(six[f"{g}/misfel"])
        docs = parse_misfel(rows, file_date, g)
        for d in docs:
            d["source_url"] = urls_by_key[f"{g}/misfel"]
            d["source_filename_date"] = latest_by_key[f"{g}/misfel"]
            cat = d.get("booking_age_category", "unknown")
            booking_categories["misfel"][cat] = booking_categories["misfel"].get(cat, 0) + 1
        parsed["misfel"].extend(docs)

        # nafiling
        rows = _parse_rows(six[f"{g}/nafiling"])
        docs = parse_nafiling(rows, file_date, g)
        for d in docs:
            d["source_url"] = urls_by_key[f"{g}/nafiling"]
            d["source_filename_date"] = latest_by_key[f"{g}/nafiling"]
            cat = d.get("booking_age_category", "unknown")
            booking_categories["nafiling"][cat] = booking_categories["nafiling"].get(cat, 0) + 1
        parsed["nafiling"].extend(docs)

    # Enhanced logging with booking categories
    print(f"[harris] Processing complete:")
    for kind in ("bond", "misfel", "nafiling"):
        total = len([d for d in parsed[kind]])
        print(f"  {kind}: {total} records")
        if booking_categories[kind]:
            print(f"    breakdown: {dict(sorted(booking_categories[kind].items()))}")
        
        # Log success messages with categories
        for d in parsed[kind][:10]:  # Sample first 10
            name = d.get("name", "UNKNOWN")
            category = d.get("booking_age_category", "unknown")
            print(f"[harris] SUCCESS: {name} [{category}]")

    # Mongo upserts (unchanged)
    col_b, col_m, col_n = _get_cols(db)
    _bulk_upsert(col_b, parsed["bond"], file_date)
    _bulk_upsert(col_m, parsed["misfel"], file_date)
    _bulk_upsert(col_n, parsed["nafiling"], file_date)

    window = int(_env("HARRIS_NEW_WINDOW_DAYS", "30"))
    alerts = {
        "bond":     {g: _new_entries(col_b, file_date, window, g) for g in GROUPS},
        "misfel":   {g: _new_entries(col_m, file_date, window, g) for g in GROUPS},
        "nafiling": {g: _new_entries(col_n, file_date, window, g) for g in GROUPS},
    }
    return alerts

# --- Enhanced Class wrapper ---
try:
    from .audited_scraper import AuditedScraper
except ImportError as e:
    print(f"[harris] Could not import AuditedScraper: {e}")
    # fallback if audited_scraper not available
    try:
        from .base_scraper import BaseScraper
        AuditedScraper = BaseScraper
        print("[harris] Using BaseScraper fallback")
    except ImportError as e2:
        print(f"[harris] Could not import BaseScraper: {e2}")
        raise

class HarrisInmateScraper(AuditedScraper):
    """
    Enhanced wrapper that delegates to run_harris_ingest() with comprehensive
    monitoring and booking age categorization.
    """
    name = "harris_inmate"

    def __init__(self, *args, **kwargs):
        # Accept optional db from run_ingestion; tolerate any signature
        db = args[0] if args else None

        # Try the various parent ctor signatures we might have
        try:
            super().__init__(db, "Harris")
        except TypeError:
            try:
                super().__init__(db)
            except TypeError:
                try:
                    super().__init__()
                except TypeError:
                    pass

    def fetch(self):
        # Start audit tracking (only if AuditedScraper is available)
        if hasattr(self, '_audit_start'):
            self._audit_start(
                letters_spec="CSV_DOWNLOAD",
                first_letters_spec="N/A",
                append_wildcard=False
            )
            
            # Track data processing metrics
            self._audit_inc("prefixes_scanned", 6)  # 6 file types processed
        
        # Run the real ingestion (fetch + upsert + de-dupe)
        alerts = run_harris_ingest()

        # Count results and track in audit
        total_alerts = 0
        for kind in ("bond", "misfel", "nafiling"):
            by_group = alerts.get(kind, {})
            count = sum(len(by_group.get(g, [])) for g in ("Civil", "Criminal"))
            total_alerts += count
        
        if hasattr(self, '_audit_inc'):
            self._audit_inc("details_parsed_ok", total_alerts)
            self._audit_inc("events_yielded", 1)  # One summary event

        # Log booking age summary from alerts
        booking_summary = {}
        for kind_alerts in alerts.values():
            for group_alerts in kind_alerts.values():
                for alert in group_alerts:
                    cat = alert.get("booking_age_category", "unknown")
                    booking_summary[cat] = booking_summary.get(cat, 0) + 1

        if booking_summary:
            print(f"[harris] BOOKING AGE SUMMARY:")
            for category, count in sorted(booking_summary.items()):
                print(f"  {category}: {count} new cases needing help")

        # Yield summary document
        yield {
            "_collection": "ingest_runs",
            "source": "harris_inmate",
            "run_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "alerts_counts": {
                "bond": sum(len(alerts.get("bond", {}).get(g, [])) for g in ("Civil", "Criminal")),
                "misfel": sum(len(alerts.get("misfel", {}).get(g, [])) for g in ("Civil", "Criminal")),
                "nafiling": sum(len(alerts.get("nafiling", {}).get(g, [])) for g in ("Civil", "Criminal")),
            },
            "booking_categories": dict(sorted(booking_summary.items())),
            # include samples of "new within window" alerts so you can spot-check
            "alerts_sample": {
                "bond": alerts.get("bond", {}).get("Civil", [])[:3] + alerts.get("bond", {}).get("Criminal", [])[:3],
                "misfel": alerts.get("misfel", {}).get("Civil", [])[:3] + alerts.get("misfel", {}).get("Criminal", [])[:3],
                "nafiling": alerts.get("nafiling", {}).get("Civil", [])[:3] + alerts.get("nafiling", {}).get("Criminal", [])[:3]
            }
        }
        
        # Finish audit tracking (only if available)
        if hasattr(self, '_audit_finish'):
            self._audit_finish()