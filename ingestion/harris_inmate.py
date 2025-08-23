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

# ---------- Parsers (same as before) ----------

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
            "group": group
        }
        doc["name"] = ", ".join([x for x in [doc["last_name"], doc["first_middle"]] if x]) or None
        doc["needs_bond_help"] = _needs_bond_help(doc["bond_amount"], doc["bond_note"])
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
            "group": group
        }
        doc["needs_bond_help"] = _needs_bond_help(doc["bond_amount"], doc["bond_note"])
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
            "group": group
        }
        doc["name"] = ", ".join([x for x in [doc["last_name"], doc["first_middle"]] if x]) or None
        doc["needs_bond_help"] = _needs_bond_help(doc["bond_amount"], doc["bond_note"])
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
                "first_seen_file_date": doc.get("first_seen_file_date")
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
    for g in GROUPS:
        # bond
        rows = _parse_rows(six[f"{g}/bond"])
        docs = parse_bond(rows, file_date, g)
        for d in docs:
            d["source_url"] = urls_by_key[f"{g}/bond"]
            d["source_filename_date"] = latest_by_key[f"{g}/bond"]
        parsed["bond"].extend(docs)

        # misfel
        rows = _parse_rows(six[f"{g}/misfel"])
        docs = parse_misfel(rows, file_date, g)
        for d in docs:
            d["source_url"] = urls_by_key[f"{g}/misfel"]
            d["source_filename_date"] = latest_by_key[f"{g}/misfel"]
        parsed["misfel"].extend(docs)

        # nafiling
        rows = _parse_rows(six[f"{g}/nafiling"])
        docs = parse_nafiling(rows, file_date, g)
        for d in docs:
            d["source_url"] = urls_by_key[f"{g}/nafiling"]
            d["source_filename_date"] = latest_by_key[f"{g}/nafiling"]
        parsed["nafiling"].extend(docs)

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

# --- Class wrapper so scripts.run_ingestion can invoke this scraper uniformly ---
try:
    from .base_scraper import BaseScraper
except Exception:
    BaseScraper = object  # fallback if imported standalone

class HarrisInmateScraper(BaseScraper):
    """
    Thin wrapper that delegates to run_harris_ingest(), which already
    handles fetching + upserting (idempotent) and returns alerts.
    This `fetch()` just calls it and emits a summary heartbeat doc.
    """
    name = "harris_inmate"

    def __init__(self, db):
        super().__init__(db)

    def fetch(self):
        # run the real ingestion (fetch + upsert + de-dupe)
        alerts = run_harris_ingest()

        # quick counts for the heartbeat
        def _cnt(kind):
            by_group = alerts.get(kind, {})
            return sum(len(by_group.get(g, [])) for g in ("Civil", "Criminal"))

        yield {
            "_collection": "ingest_runs",
            "source": "harris_inmate",
            "run_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "alerts_counts": {
                "bond": _cnt("bond"),
                "misfel": _cnt("misfel"),
                "nafiling": _cnt("nafiling"),
            },
            # include samples of “new within window” alerts so you can spot-check
            "alerts_sample": {
                "bond":     alerts.get("bond", {}).get("Civil", [])[:3] + alerts.get("bond", {}).get("Criminal", [])[:3],
                "misfel":   alerts.get("misfel", {}).get("Civil", [])[:3] + alerts.get("misfel", {}).get("Criminal", [])[:3],
                "nafiling": alerts.get("nafiling", {}).get("Civil", [])[:3] + alerts.get("nafiling", {}).get("Criminal", [])[:3],
            }
        }
        return
        yield