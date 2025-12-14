#!/usr/bin/env python3
"""
Fetch Email Rosters via IMAP and save attachments to a target folder.

Features:
- Connects to IMAP (e.g., Gmail/Google Workspace)
- Filters by from address and subject patterns (optional)
- Downloads CSV/XLSX (optionally .xls) attachments
- Saves into HARRIS_EMAIL_ROSTER_DIR (supports ~ and $HOME; creates folders)
- Deduplicates by SHA-256 so identical attachments aren't saved twice
- Records a ledger in MongoDB (collection: email_roster_inbox)
- Optional: mark messages as SEEN

Usage:
  python3 -m scripts.fetch_email_rosters

Env vars:
  IMAP_HOST (e.g., imap.gmail.com)
  IMAP_PORT (default: 993)
  IMAP_USERNAME
  IMAP_PASSWORD  (for Gmail, use an App Password)
  IMAP_SSL (default: 1)
  IMAP_FOLDER (default: INBOX)
  IMAP_UNSEEN_ONLY (default: 1)
  IMAP_SINCE_DAYS (default: 14)
  ROSTER_EMAIL_FROM (optional: e.g., 'alerts@county.gov')
  ROSTER_SUBJECT_INCLUDE (optional: comma-separated substrings)
  ROSTER_SUBJECT_EXCLUDE (optional: comma-separated substrings)
  ROSTER_SAVE_BY_DATE (default: 1; saves into YYYY-MM-DD subfolders)
  ROSTER_MAX_MESSAGES (default: 100)
  ROSTER_ALLOWED_EXT (default: .csv,.xlsx,.xls)
  MARK_SEEN (default: 1)

  HARRIS_EMAIL_ROSTER_DIR (default: email_rosters)
"""

from __future__ import annotations

import base64
import email
import imaplib
import os
import re
import sys
import time
import hashlib
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

# Optional: Dropbox SDK for mirroring saved attachments to a Dropbox folder
try:
    import dropbox  # type: ignore
    from dropbox.files import WriteMode  # type: ignore
except Exception:
    dropbox = None  # gracefully degrade if not installed
    WriteMode = None  # type: ignore

# Load .env early so IMAP_* and other vars are available
load_dotenv()
DEBUG = os.getenv("FETCH_DEBUG", "0").strip() not in ("0", "false", "False", "")

try:
    # Ensure imports work whether run as module or file
    from storage.mongo_client import get_db  # type: ignore
except Exception:
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from storage.mongo_client import get_db  # type: ignore


def _expand_dir(d_raw: str) -> Path:
    d = os.path.expanduser(os.path.expandvars(d_raw.strip()))
    p = Path(d)
    if not p.is_absolute():
        p = Path.cwd() / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_maybe(value: str) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _sanitize_filename(name: str) -> str:
    name = _decode_maybe(name)
    # Remove path separators and illegal characters
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    # Trim super long names
    return name[:200] if len(name) > 200 else name


def _sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _imap_login() -> imaplib.IMAP4:
    # Default to Gmail IMAP host if not provided (or provided as blank)
    host = (os.getenv("IMAP_HOST") or "imap.gmail.com").strip()
    port = int(os.getenv("IMAP_PORT", "993"))
    use_ssl = os.getenv("IMAP_SSL", "1").strip() not in ("0", "false", "False")
    user = os.getenv("IMAP_USERNAME", "").strip()
    pwd = os.getenv("IMAP_PASSWORD", "").strip()

    missing = []
    if not host:
        missing.append("IMAP_HOST")
    if not user:
        missing.append("IMAP_USERNAME")
    if not pwd:
        missing.append("IMAP_PASSWORD")
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}. Add them to .env or export in your shell.")

    if use_ssl:
        client: imaplib.IMAP4 = imaplib.IMAP4_SSL(host, port)
    else:
        client = imaplib.IMAP4(host, port)
    typ, _ = client.login(user, pwd)
    if typ != "OK":
        raise RuntimeError("IMAP login failed")
    return client


def _imap_search(client: imaplib.IMAP4) -> List[bytes]:
    folder = os.getenv("IMAP_FOLDER", "INBOX")
    unseen_only = os.getenv("IMAP_UNSEEN_ONLY", "1") not in ("0", "false", "False")
    since_days = int(os.getenv("IMAP_SINCE_DAYS", "14"))
    if DEBUG:
        try:
            typ_list, boxes = client.list()
            if typ_list == "OK" and boxes:
                names = []
                for b in boxes:
                    # Response format: b'(* FLAGS) "/" "INBOX"'
                    try:
                        parts = b.decode(errors="ignore").split(" \"")
                        name = parts[-1].strip('"') if parts else str(b)
                        names.append(name)
                    except Exception:
                        names.append(str(b))
                print(f"[fetch_email_rosters] DEBUG mailboxes: {names}")
        except Exception as e:
            print(f"[fetch_email_rosters] DEBUG list mailboxes failed: {e}")

    typ, _ = client.select(folder)
    if typ != "OK":
        raise RuntimeError(f"Unable to select folder {folder}")

    # Use timezone-aware UTC now to avoid deprecation warnings
    since_date = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")
    search_terms = ["SINCE", since_date]
    if unseen_only:
        search_terms.insert(0, "UNSEEN")

    # We filter From/Subject in Python for flexibility
    if DEBUG:
        print(f"[fetch_email_rosters] DEBUG select folder: {folder}, unseen_only={unseen_only}, since={since_date}")
    typ, data = client.search(None, *search_terms)
    if typ != "OK":
        return []
    ids = data[0].split() if data and data[0] else []
    # Limit
    max_msgs = int(os.getenv("ROSTER_MAX_MESSAGES", "100"))
    return ids[-max_msgs:]


def _iter_nested_messages(msg: email.message.Message):
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list):
                for sub in payload:
                    if isinstance(sub, email.message.Message):
                        yield sub
            elif isinstance(payload, email.message.Message):
                yield payload
            else:
                try:
                    raw = part.get_payload(decode=True)
                    if raw:
                        yield email.message_from_bytes(raw)
                except Exception:
                    continue


def _match_sender_and_subject(msg: email.message.Message) -> bool:
    from_filter = os.getenv("ROSTER_EMAIL_FROM", "").strip().lower()
    original_from_filter = os.getenv("ROSTER_ORIGINAL_FROM", "").strip().lower()
    inc = [s.strip().lower() for s in os.getenv("ROSTER_SUBJECT_INCLUDE", "").split(",") if s.strip()]
    exc = [s.strip().lower() for s in os.getenv("ROSTER_SUBJECT_EXCLUDE", "").split(",") if s.strip()]

    from_hdr = _decode_maybe(msg.get("From", "")).lower()
    subj_hdr = _decode_maybe(msg.get("Subject", "")).lower()

    # If either filter is provided, require at least one to match
    from_ok = True
    if from_filter or original_from_filter:
        from_ok = False
        if from_filter and from_filter in from_hdr:
            from_ok = True
        if not from_ok and original_from_filter and original_from_filter in from_hdr:
            from_ok = True
        if not from_ok and original_from_filter:
            # Check nested forwarded messages for original sender
            for sub in _iter_nested_messages(msg):
                sub_from = _decode_maybe(sub.get("From", "")).lower()
                if original_from_filter in sub_from:
                    from_ok = True
                    break

    if not from_ok:
        return False
    if inc and not any(p in subj_hdr for p in inc):
        return False
    if exc and any(p in subj_hdr for p in exc):
        return False
    return True


def _collect_attachments_from_message(msg: email.message.Message, allowed_exts: List[str]) -> List[Tuple[str, bytes]]:
    out: List[Tuple[str, bytes]] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "message/rfc822":
                # Recurse into nested messages
                payload = part.get_payload()
                subs: List[email.message.Message] = []
                if isinstance(payload, list):
                    subs = [p for p in payload if isinstance(p, email.message.Message)]
                elif isinstance(payload, email.message.Message):
                    subs = [payload]
                else:
                    try:
                        raw = part.get_payload(decode=True)
                        if raw:
                            subs = [email.message_from_bytes(raw)]
                    except Exception:
                        subs = []
                for sub in subs:
                    out.extend(_collect_attachments_from_message(sub, allowed_exts))
                continue

            cd = part.get("Content-Disposition", "")
            if "attachment" not in cd.lower():
                continue
            filename = part.get_filename()
            if not filename:
                continue
            filename = _sanitize_filename(str(filename))
            ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if allowed_exts and ext not in allowed_exts:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            out.append((filename, payload))
    return out


def _yield_attachments(msg: email.message.Message) -> List[Tuple[str, bytes]]:
    allowed = [s.strip().lower() for s in os.getenv("ROSTER_ALLOWED_EXT", ".csv,.xlsx,.xls").split(",") if s.strip()]
    return _collect_attachments_from_message(msg, allowed)


def _save_attachment(base_dir: Path, filename: str, content: bytes) -> Path:
    by_date = os.getenv("ROSTER_SAVE_BY_DATE", "1") not in ("0", "false", "False")
    target_dir = base_dir / datetime.now().strftime("%Y-%m-%d") if by_date else base_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    sha = _sha256_bytes(content)
    stem, dot, ext = filename.partition(".")
    safe_name = f"{_sanitize_filename(stem)}__{sha[:8]}.{ext}" if dot else f"{_sanitize_filename(filename)}__{sha[:8]}"
    path = target_dir / safe_name
    with path.open("wb") as f:
        f.write(content)
    return path


_DBX_CLIENT = None  # lazy-initialized Dropbox client


def _get_dbx_client():
    """Return a Dropbox client if DROPBOX_ACCESS_TOKEN is set and SDK is available, else None."""
    global _DBX_CLIENT
    token = os.getenv("DROPBOX_ACCESS_TOKEN", "").strip()
    if not token or dropbox is None:
        return None
    if _DBX_CLIENT is None:
        try:
            _DBX_CLIENT = dropbox.Dropbox(token, timeout=30)
        except Exception:
            _DBX_CLIENT = None
    return _DBX_CLIENT


def _maybe_upload_dropbox(local_path: Path, content: bytes) -> Optional[str]:
    """If configured, upload the attachment bytes to Dropbox and return the remote path."""
    dbx = _get_dbx_client()
    if dbx is None:
        return None
    base_folder = os.getenv("DROPBOX_BASE_FOLDER", "/warrantdb/email_rosters").strip() or "/warrantdb/email_rosters"
    # Ensure path starts with '/'
    if not base_folder.startswith("/"):
        base_folder = "/" + base_folder

    # Mirror the relative path under HARRIS_EMAIL_ROSTER_DIR if it contains a date folder
    # Otherwise, upload flat under base_folder
    try:
        rel_name = local_path.name
        parent = local_path.parent.name  # may be a YYYY-MM-DD if ROSTER_SAVE_BY_DATE=1
        # If parent looks like a date, keep it as a subfolder in Dropbox
        drop_dir = base_folder
        if re.match(r"^\d{4}-\d{2}-\d{2}$", parent):
            drop_dir = f"{base_folder}/{parent}"
        drop_path = f"{drop_dir}/{rel_name}"
        # Create folder(s) implicitly by upload; Dropbox API allows this
        dbx.files_upload(content, drop_path, mode=WriteMode.overwrite)
        return drop_path
    except Exception as e:
        if DEBUG:
            print(f"[fetch_email_rosters] DEBUG dropbox upload failed: {e}")
        return None


def main() -> Dict[str, Any]:
    base_dir = _expand_dir(os.getenv("HARRIS_EMAIL_ROSTER_DIR", "email_rosters"))
    mark_seen = os.getenv("MARK_SEEN", "1") not in ("0", "false", "False")

    db = get_db()
    ledger = db["email_roster_inbox"]
    try:
        ledger.create_index([("sha256", 1)], unique=True, background=True)
        ledger.create_index([("message_id", 1)], background=True)
        ledger.create_index([("saved_at", 1)], background=True)
    except Exception:
        pass

    client = _imap_login()
    if DEBUG:
        # Print a minimal sanitized config snapshot
        cfg = {
            "IMAP_HOST": (os.getenv("IMAP_HOST") or "imap.gmail.com"),
            "IMAP_PORT": os.getenv("IMAP_PORT", "993"),
            "IMAP_USERNAME": os.getenv("IMAP_USERNAME", ""),
            "IMAP_PASSWORD_set": bool(os.getenv("IMAP_PASSWORD")),
            "ROSTER_EMAIL_FROM": os.getenv("ROSTER_EMAIL_FROM", ""),
            "ROSTER_ORIGINAL_FROM": os.getenv("ROSTER_ORIGINAL_FROM", ""),
            "HARRIS_EMAIL_ROSTER_DIR": str(base_dir),
        }
        print("[fetch_email_rosters] DEBUG config:", cfg)
    ids = _imap_search(client)
    if DEBUG:
        print(f"[fetch_email_rosters] DEBUG search: found {len(ids)} message ids")
    saved = 0
    skipped_dupe = 0
    processed_msgs = 0
    errors: List[str] = []
    saved_paths_debug: List[str] = []

    for id_ in ids:
        try:
            typ, data = client.fetch(id_, "(RFC822 UID)")
            if typ != "OK" or not data or not data[0]:
                continue
            processed_msgs += 1
            raw = data[0][1]
            msg = email.message_from_bytes(raw)
            if not _match_sender_and_subject(msg):
                continue
            # Use Message-ID or fallback to UID
            msg_id = _decode_maybe(msg.get("Message-ID", "")) or f"UID:{id_.decode()}"

            for fname, content in _yield_attachments(msg):
                sha = _sha256_bytes(content)
                if ledger.find_one({"sha256": sha}):
                    skipped_dupe += 1
                    continue
                save_path = _save_attachment(base_dir, fname, content)
                saved += 1
                if DEBUG:
                    saved_paths_debug.append(str(save_path))
                # Optional: mirror to Dropbox for archival
                dropbox_path = _maybe_upload_dropbox(save_path, content)
                doc = {
                    "message_id": msg_id,
                    "from": _decode_maybe(msg.get("From", "")),
                    "subject": _decode_maybe(msg.get("Subject", "")),
                    "date": _decode_maybe(msg.get("Date", "")),
                    "filename": fname,
                    "saved_path": str(save_path),
                    "dropbox_path": dropbox_path,
                    "sha256": sha,
                    "size": len(content),
                    "saved_at": _now_iso(),
                }
                try:
                    ledger.insert_one(doc)
                except Exception:
                    pass

            if mark_seen:
                try:
                    client.store(id_, "+FLAGS", "(\\Seen)")
                except Exception:
                    pass
        except Exception as e:
            errors.append(str(e))

    try:
        client.close()
        client.logout()
    except Exception:
        pass

    result = {
        "base_dir": str(base_dir),
        "messages_scanned": len(ids),
        "messages_processed": processed_msgs,
        "attachments_saved": saved,
        "attachments_skipped_duplicates": skipped_dupe,
        "errors": errors,
    }
    if DEBUG:
        result["saved_paths"] = saved_paths_debug
    print(result)
    return result


if __name__ == "__main__":
    main()
