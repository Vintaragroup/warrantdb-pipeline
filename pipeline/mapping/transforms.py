from __future__ import annotations
import re
from datetime import datetime, timezone
from dateutil import parser
from typing import Any, Dict, List, Optional
import os

# ---------- basic utils ----------

def get_path(doc: Dict[str, Any], path: str, default=None):
    if doc is None or path is None:
        return default
    cur: Any = doc
    # Support array indices like charges[0].bond
    for part in re.split(r"\.", path):
        m = re.match(r"^(?P<key>[^\[]+)(\[(?P<idx>\d+)\])?$", part)
        if not m:
            return default
        key = m.group("key")
        cur = cur.get(key) if isinstance(cur, dict) else default
        if cur is default:
            return default
        idx = m.group("idx")
        if idx is not None:
            if isinstance(cur, list):
                i = int(idx)
                cur = cur[i] if 0 <= i < len(cur) else default
            else:
                return default
    return cur


def apply_transforms(value: Any, steps: List[Any]) -> Any:
    v = value
    for step in steps:
        # step can be a string (name) or a dict like {"parse_date": {"formats": [...]}} or {"default_midnight_utc_if_date_only": true}
        if isinstance(step, str):
            func = TRANSFORMS.get(step)
            if not func:
                # unknown transform; ignore silently to keep pipeline resilient
                continue
            v = func(v)
        elif isinstance(step, dict):
            name, param_val = next(iter(step.items()))
            func = TRANSFORMS.get(name)
            if not func:
                # unknown transform; ignore to avoid crashing
                continue
            if isinstance(param_val, dict):
                v = func(v, **param_val)
            else:
                # if the param is a scalar/bool, treat it as a flag and just call the transform on v
                v = func(v)
        else:
            # unsupported step type; ignore
            continue
    return v

# ---------- string transforms ----------

def trim(x):
    return x.strip() if isinstance(x, str) else x

def collapse_whitespace(x):
    return " ".join(x.split()) if isinstance(x, str) else x

def to_upper(x):
    return x.upper() if isinstance(x, str) else x

def to_lower(x):
    return x.lower() if isinstance(x, str) else x

def strip_whitespace(x):
    return x.strip() if isinstance(x, str) else x

def default_null(x):
    if x is None:
        return None
    if isinstance(x, str) and x.strip() == "":
        return None
    return x

# ---------- date/time transforms ----------

def _normalize_strptime(fmt: str) -> str:
    """
    Convert common Java-style tokens to Python strptime tokens.
    Supports: yyyy, MM, M, dd, d, HH, H, mm, m, ss, s.
    Leaves percent-based tokens intact.
    """
    # Replace longer tokens first to avoid partial overlaps
    repl = [
        ("yyyy", "%Y"),
        ("MM", "%m"),
        ("dd", "%d"),
        ("HH", "%H"),
        ("mm", "%M"),
        ("ss", "%S"),
        # single-letter fallbacks (accepts non-zero-padded)
        ("M", "%m"),
        ("d", "%d"),
        ("H", "%H"),
        ("m", "%M"),
        ("s", "%S"),
    ]
    out = fmt
    for a, b in repl:
        out = out.replace(a, b)
    return out

def parse_date(x, formats: List[str] | None = None):
    if x in (None, ""):
        return None
    s = str(x)
    # Try explicit formats first (after normalizing tokens)
    if formats:
        for f in formats:
            try:
                pyfmt = _normalize_strptime(f)
                return datetime.strptime(s, pyfmt)
            except ValueError:
                continue
    # Fallback: let dateutil try to parse
    try:
        return parser.parse(s)
    except Exception:
        return None

def parse_datetime(x, formats: List[str] | None = None):
    return parse_date(x, formats)


def to_iso_date(dt: datetime | None):
    if not isinstance(dt, datetime):
        return None
    return dt.date().isoformat()


def to_iso_datetime(dt: datetime | None):
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def default_midnight_utc_if_date_only(x, flag: bool = True):
    # no-op shim: we already attach midnight UTC in to_iso_datetime when needed
    return x

# ---------- currency / number ----------
_currency_re = re.compile(r"([+-]?[\d,]+(?:\.\d{1,2})?)")

def to_currency_number(x):
    if x in (None, ""): return None
    s = str(x)
    m = _currency_re.search(s)
    if not m:
        # If there are no digits at all, do not try to parse as float; treat as label/signal, return None.
        if not any(ch.isdigit() for ch in s):
            return None
        try:
            return float(s)
        except Exception:
            return None
    try:
        return float(m.group(1).replace(",", ""))
    except Exception:
        return None


def parse_money_in_string_to_number(x):
    return to_currency_number(x)


def parse_money_or_na_to_number(x):
    if x is None:
        return None
    s = str(x).strip().upper()
    if s in {"N/A", "NA", "NONE", "UNKNOWN", ""}:
        return None
    return to_currency_number(s)

def none_if_blank(x):
    """Return None for empty strings or strings of only whitespace."""
    if x is None:
        return None
    if isinstance(x, str) and x.strip() == "":
        return None
    return x

def safe_float(x):
    """Parse a float safely from common string formats like '12,345.00' or '003.0'."""
    if x in (None, ""):
        return None
    try:
        s = str(x).strip().replace(",", "")
        return float(s)
    except Exception:
        return None

def normalize_bond_label(x):
    """
    Normalize common bond label variants to canonical tokens.
    Preserves certain signals/labels like 'REFER TO MAGISTRATE', 'SUMMONS ISSUED', or single-letter codes.
    """
    if x in (None, ""):
        return None
    s = str(x).strip()
    s_up = s.upper()
    # preserve signals
    if s_up in {"REFER TO MAGISTRATE", "SUMMONS ISSUED"}:
        return s
    if len(s_up) == 1 and s_up.isalpha():
        return s
    s_norm = re.sub(r"[^\w/ ]+", "", s_up)

    # canonical mappings
    if s_norm in {"NO BOND", "NOBOND", "NO-BOND", "NONE"}:
        return "No Bond"
    if s_norm in {"DENIED", "BOND DENIED"}:
        return "Denied"
    if s_norm in {"CASH", "CASH ONLY"}:
        return "Cash"
    if s_norm in {"SURETY", "CASH/SURETY", "CASH OR SURETY", "SURETY/CASH"}:
        return "Surety"
    if s_norm in {"PR", "P.R.", "PERSONAL RECOGNIZANCE", "PERSONAL BOND", "ROR", "R.O.R."}:
        return "PR"
    if "RECOGNIZANCE" in s_norm:
        return "PR"
    # fallback: title-case words, keep slashes
    return " ".join(w.capitalize() for w in re.split(r"(\s|/)", s_norm) if w)

def extract_bond_label(x):
    if x in (None, ""):
        return None
    s = str(x).strip()
    # return substring before the first currency amount
    m = _currency_re.search(s)
    label = s if not m else s[:m.start()].strip()

    # treat empty / bare currency symbol as no label
    if not label or label in {"$", "USD", "US$", "U.S.$"}:
        return None

    L = label.upper()
    # preserve signals
    if L in {"REFER TO MAGISTRATE", "SUMMONS ISSUED"}:
        return label
    if len(L) == 1 and L.isalpha():
        return label
    # normalize a few common variants (optional)
    if L.startswith("CASH"):
        return "Cash"
    if any(k in L for k in ("SURETY", "SRTY")):
        return "Surety"
    if "PR" in L or "PERSONAL RECOG" in L or "OWN RECOGNIZ" in L:
        return "PR"
    return label

def extract_and_normalize_bond_label(x):
    """Extract any leading label before a currency amount, then normalize it.
    If the extracted label is a special signal (e.g., 'REFER TO MAGISTRATE', 'SUMMONS ISSUED', or a single letter), return it as-is.
    """
    label = extract_bond_label(x)
    if label is None:
        return None
    label_up = str(label).upper()
    if label_up in {"REFER TO MAGISTRATE", "SUMMONS ISSUED"} or (len(label_up) == 1 and label_up.isalpha()):
        return label
    return normalize_bond_label(label)


def strip_to_digits(x):
    if x in (None, ""): return x
    return "".join(ch for ch in str(x) if ch.isdigit()) or None

def digits_prefix(value=None, **kwargs):
    """
    Return the leading digit run from a string (e.g., '19299540101A' -> '19299540101').
    If no leading digits are present, return None.
    """
    if value is None:
        return None
    s = str(value)
    m = re.match(r"^(\d+)", s)
    return m.group(1) if m else None


def safe_int(x):
    if x in (None, ""): return None
    try:
        # allow strings like "00123" or floats that are whole numbers
        s = str(x).strip()
        if s.isdigit():
            return int(s)
        f = float(s.replace(",", ""))
        return int(f) if f.is_integer() else None
    except Exception:
        return None


# ---------- safe text helper ----------
def safe_text(x):
    """Best-effort convert to text for debugging/derivation without throwing."""
    try:
        return "" if x is None else str(x)
    except Exception:
        return ""

# ---------- name parsing ----------
def extract_last(s):
    if not isinstance(s, str): return None
    parts = [p.strip() for p in s.split(",", 1)]
    if len(parts) == 2:
        return parts[0]
    toks = s.split()
    return toks[-1] if toks else None

def extract_first_plus_middle(s):
    if not isinstance(s, str): return None
    parts = [p.strip() for p in s.split(",", 1)]
    if len(parts) == 2:
        return parts[1]
    toks = s.split()
    return " ".join(toks[:-1]) if len(toks) > 1 else (toks[0] if toks else None)

def extract_middle_initial(s):
    fpm = extract_first_plus_middle(s)
    if not fpm: return None
    toks = fpm.split()
    return toks[1][:1] if len(toks) > 1 else None

# ---------- status / code normalization ----------
SEX_MAP = {
    "M": "Male", "F": "Female",
    "MALE": "Male", "FEMALE": "Female",
    # Harris-specific letter codes occasionally seen
    "J": "Male",  # Harris feed sometimes uses J for male
    "B": "Female",  # Harris feed sometimes uses B for female
    "O": "Other",
    "U": "Unknown",
}

RACE_MAP = {
    # common letters
    "W": "White", "B": "Black", "H": "Hispanic", "A": "Asian", "O": "Other",
    # expanded letters sometimes present in Harris
    "I": "Native American", "P": "Pacific Islander", "U": "Unknown", "X": "Unknown",
    # already-normalized words
    "WHITE": "White", "BLACK": "Black", "ASIAN": "Asian", "HISPANIC": "Hispanic",
    "NATIVE AMERICAN": "Native American", "PACIFIC ISLANDER": "Pacific Islander", "UNKNOWN": "Unknown",
}

def decode_sex_code(x):
    if x is None: return None
    return SEX_MAP.get(str(x).upper(), str(x))

def decode_race_code(x):
    if x is None:
        return None
    s = str(x).strip()
    if s == "":
        return None
    up = s.upper()
    # drop purely numeric garbage (seen in some Harris rows)
    if up.isdigit():
        return None
    return RACE_MAP.get(up, s)

def normalize_status(x):
    if x is None:
        return None
    s = str(x).strip().lower()
    active = {
        "in custody", "active", "open", "booked", "jailed", "detained", "hold", "on hold", "warrant active"
    }
    released = {
        "released", "disposed", "closed", "out", "posted bond", "bonded out", "released on bond"
    }
    if s in active:
        return "Active"
    if s in released:
        return "Released"
    if s in {"dismissed"}:
        return "Dismissed"
    return s.title()

def harris_status_from_source_and_note(
    src: Optional[Dict[str, Any]] = None,
    out: Optional[Dict[str, Any]] = None,
    *,
    source: Optional[Any] = None,
    bond_note: Optional[Any] = None,
    **kwargs
) -> Optional[str]:
    """
    Derive a simple status label for Harris rows.

    This is a DERIVE function and must accept (src, out, **kwargs) positionally,
    because the mapper calls derive functions with those two positional arguments.

    It also accepts keyword-only hints `source` and `bond_note` which may be
    raw values or directive dicts like {"from": "field"} or {"path": "field"}.
    """
    def _resolve(v):
        # If mapper passed {"from": "path"} or {"path": "path"}, resolve it.
        if isinstance(v, dict):
            p = v.get("from") or v.get("path")
            if p:
                return get_path(src or {}, p)
            # If it's some other dict blob, just stringify safely later
        return v

    try:
        s = _resolve(source)
        n = _resolve(bond_note)

        # Coerce to strings safely (avoid calling .strip() on non-strings)
        s_str = safe_text(s).strip().lower()
        note_str = safe_text(n).strip().upper()

        # Optional debug logging (enabled by DEBUG_MAP or DEBUG_DERIVE)
        if os.getenv("DEBUG_MAP") or os.getenv("DEBUG_DERIVE"):
            rid = None
            try:
                rid = (src or {}).get("_id")
            except Exception:
                pass
            print(f"[HARRIS-STATUS-DBG] _id={rid} raw.source={repr(s)} raw.note={repr(n)} parsed.source={s_str} parsed.note={note_str}")

        # Note-driven signals
        if "REFER TO MAGISTRATE" in note_str:
            return "Pending Magistrate"
        if "UNSECURED GOB ELIGIBLE" in note_str:
            return "Eligible - Unsecured GOB"

        # Source-driven signals
        if "nafiling" in s_str:
            return "No Filing"

        # Default catch-all
        return "Active"

    except Exception as e:
        # Never fail derivation; emit debug and fall back to "Active"
        if os.getenv("DEBUG_MAP") or os.getenv("DEBUG_DERIVE"):
            rid = None
            try:
                rid = (src or {}).get("_id")
            except Exception:
                pass
            print(f"[HARRIS-STATUS-ERR] _id={rid} err={e} source={type(source)} bond_note={type(bond_note)}")
        return "Active"

# ---------- derives ----------
def time_bucket_from_booking_date(src, out, **kwargs):
    b = out.get("booking_date") if isinstance(out, dict) else None
    if not b:
        return "unknown"
    try:
        # Accept ISO strings or datetime/date objects
        if isinstance(b, str):
            dt = parser.isoparse(b)
        else:
            dt = b

        # If we got a date-only or tz-naive datetime, set midnight UTC
        if isinstance(dt, datetime):
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            # Handle rare case of a pure date object
            dt = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)

        now_utc = datetime.now(timezone.utc)
        days = (now_utc - dt).days
    except Exception:
        return "unknown"

    if days < 0: return "future"
    if days <= 30: return "0_to_30_days"
    if days <= 60: return "31_to_60_days"
    if days <= 90: return "61_to_90_days"
    if days <= 180: return "90_to_180_days"
    if days <= 365: return "180_to_365_days"
    return "365_days_or_older"

def derive_tags_from_offense(src, out, **kwargs):
    off = (out.get("offense") or "").upper() if isinstance(out, dict) else ""
    tags = []
    if any(k in off for k in ["ASSAULT", "AGG", "TERRORISTIC", "ROBBERY", "MURDER"]):
        tags.append("violent")
    if any(k in off for k in ["DWI", "INTOX", "ALCOHOL"]):
        tags.append("dwi")
    if any(k in off for k in ["THEFT", "BURGLARY", "ROBBERY"]):
        tags.append("property")
    if any(k in off for k in ["POSS CS", "DEL CS", "CONTROLLED SUBSTANCE", "NARCOT"]):
        tags.append("drug")
    return tags or None

def now_iso(src=None, out=None, **kwargs):
    return datetime.now(timezone.utc).isoformat()

def first_nonnull_from_paths(src, out, paths: List[str], **kwargs):
    for p in paths:
        v = get_path(src, p)
        if v not in (None, ""):
            return v
    return None

def first_nonnull_parsed_amount_from(src, out, paths: List[str], **kwargs):
    for p in paths:
        v = get_path(src, p)
        n = to_currency_number(v)
        if n not in (None, 0):
            return n
    # return 0 if any path had a parseable zero; else None
    for p in paths:
        v = get_path(src, p)
        if to_currency_number(v) == 0:
            return 0.0
    return None

def first_nonnull_parsed_type_from(src, out, paths: List[str], **kwargs):
    for p in paths:
        v = get_path(src, p)
        label = extract_bond_label(v)
        if label:
            return label
    return None

def bond_type_from_charge_bonds(src, out, **kwargs):
    charges = src.get("charges") or []
    for ch in charges:
        lbl = extract_bond_label(ch.get("bond"))
        if lbl:
            return lbl
    return None

def join_nonempty_from_paths(src, out, paths: List[str], sep: str = " ", **kwargs):
    parts: List[str] = []
    for p in paths:
        v = get_path(src, p)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            parts.append(s)
    return sep.join(parts) if parts else None

def parse_age_years(x):
    if x in (None, ""): return None
    s = str(x).strip()
    m = re.search(r"(\d{1,3})", s)
    return int(m.group(1)) if m else None

def current_index(src=None, out=None, **kwargs):
    # placeholder if you wire a per-charge normalizer that sets context
    return None

# ---------- registries ----------
TRANSFORMS = {
    "trim": trim,
    "collapse_whitespace": collapse_whitespace,
    "to_upper": to_upper,
    "to_lower": to_lower,
    "strip_whitespace": strip_whitespace,
    "parse_date": parse_date,
    "parse_datetime": parse_datetime,
    "to_iso_date": to_iso_date,
    "to_iso_datetime": to_iso_datetime,
    "default_midnight_utc_if_date_only": default_midnight_utc_if_date_only,
    "to_currency_number": to_currency_number,
    "parse_money_in_string_to_number": parse_money_in_string_to_number,
    "parse_money_or_na_to_number": parse_money_or_na_to_number,
    "none_if_blank": none_if_blank,
    "safe_float": safe_float,
    "default_null": default_null,
    "normalize_bond_label": normalize_bond_label,
    "extract_bond_label": extract_bond_label,
    "extract_and_normalize_bond_label": extract_and_normalize_bond_label,
    "extract_last": extract_last,
    "extract_first_plus_middle": extract_first_plus_middle,
    "extract_middle_initial": extract_middle_initial,
    "decode_sex_code": decode_sex_code,
    "decode_race_code": decode_race_code,
    "normalize_status": normalize_status,
    "parse_age_years": parse_age_years,
    "now_iso": now_iso,
    "strip_to_digits": strip_to_digits,
    "digits_prefix": digits_prefix,
    "safe_int": safe_int,
    "safe_text": safe_text,
}

DERIVE = {
    "now_iso": now_iso,
    "time_bucket_from_booking_date": time_bucket_from_booking_date,
    "derive_tags_from_offense": derive_tags_from_offense,
    "first_nonnull_from_paths": first_nonnull_from_paths,
    "first_nonnull_parsed_amount_from": first_nonnull_parsed_amount_from,
    "first_nonnull_parsed_type_from": first_nonnull_parsed_type_from,
    "bond_type_from_charge_bonds": bond_type_from_charge_bonds,
    "current_index": current_index,
    "join_nonempty_from_paths": join_nonempty_from_paths,
    "harris_status_from_source_and_note": harris_status_from_source_and_note,
}
