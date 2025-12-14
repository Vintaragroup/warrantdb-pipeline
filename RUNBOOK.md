# WarrantDB Pipeline Runbook

This runbook covers the full pipeline (ingestion ➜ normalize ➜ maintenance) and the Harris roster support flow (email rosters ➜ import ➜ enrich). Use `SCHEDULING.md` for twice-daily orchestration across sources.

## Prerequisites

- Python 3.10+ and pip
- Virtual environment recommended
- Dependencies from `requirements.txt`
- MongoDB Atlas or local MongoDB. Connection is configured via environment variables loaded from `.env` by `storage/mongo_client.py`.

Environment variables used:
- `MONGO_URI` (e.g., `mongodb+srv://user:pass@cluster-url/` or `mongodb://localhost:27017`)
- `MONGO_DB` (default: `warrantdb`)

Example `.env`:
```
MONGO_URI="mongodb+srv://<user>:<pass>@<cluster-url>/?retryWrites=true&w=majority"
MONGO_DB=warrantdb
```

Setup venv and install deps:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 1) Run a scraper

Use `scripts/run_ingestion.py` to run a county/source scraper. Available sources are defined in `scripts/run_ingestion.py` (SCRAPER_SPECS):
- `harris_inmate`
- `galveston_p2c_fast`
- `brazoria_jail`
- `fortbend_jail`
- `jefferson_jail`
- `harris_email_roster` (imports CSV/XLSX rosters dropped into a folder, enriches Harris docs)

Run example (module mode recommended to avoid import issues):
```bash
python3 -m scripts.run_ingestion --source harris_inmate
```
Alternative:
```bash
PYTHONPATH=. python3 scripts/run_ingestion.py --source harris_inmate
```

This script writes person documents into the `persons` collection and related custody events (if any) into the appropriate collections. It uses `storage/mongo_client.py` to connect to MongoDB.

## 2) Normalize to simple_<county>

Normalize ingested data into the `simple_<county>` collection (e.g., `simple_harris`). Use the normalizer at repo root:

Examples:
```bash
# Harris
python3 normalize_to_simple.py --county harris --debug

# Other counties (if supported by the normalizer)
python3 normalize_to_simple.py --county galveston --debug
python3 normalize_to_simple.py --county brazoria --debug
```

Notes:
- The normalizer may support feature flags (e.g., deriving booking_datetime and computing time_bucket_v2). Use the `--debug` flag for verbose logging while validating changes. Check `normalize_to_simple.py --help` for flags/options available in your environment.

## 3) Harris post-normalize housekeeping

After normalizing Harris, run this post-process to ensure canonical booking timestamps and v2 buckets are present, correctly typed (Date), and up-to-date.

Script: `scripts/harris_post_normalize.py`

- Dry-run (no writes):
```bash
python3 -m scripts.harris_post_normalize --dry-run
```

- Apply updates (includes rebucketing last 90 days by default):
```bash
python3 -m scripts.harris_post_normalize
```

- Limit rebucketing window (e.g., last 14 days):
```bash
python3 -m scripts.harris_post_normalize --max-days 14
```

- Skip rebucketing entirely (just backfill/compute):
```bash
python3 -m scripts.harris_post_normalize --max-days 0
```

Alternative if you prefer direct file invocation:
```bash
PYTHONPATH=. python3 scripts/harris_post_normalize.py --dry-run
```

What it does:
- Converts `booking_datetime` strings to a Date
- If missing, derives `booking_datetime` from `booking_date`
- Computes `time_bucket_v2` based on `booking_datetime`
- Optionally rebuckets recent docs so stored tags age with time

Target collection defaults to `simple_harris`.

## 4) Verification (mongosh)

Use the checker to validate bucket correctness and aging:

Script: `scripts/check_time_bucket_v2.js`

Run from shell (zsh):
```bash
mongosh "$MONGO_URI/warrantdb" scripts/check_time_bucket_v2.js
```

It prints:
- Live computed count in 24–48h
- Stored `time_bucket_v2` distribution inside that cohort
- Stored vs computed cross-tab for the last ~90 days
- Sample mismatches (if any)

Quick ad-hoc queries inside mongosh:
```javascript
// Count truly in 24–48h by booking time
db.getSiblingDB("warrantdb").simple_harris.aggregate([
  { $match: { booking_datetime: { $type: "date" } } },
  { $addFields: { hrs: { $dateDiff: { startDate: "$booking_datetime", endDate: "$$NOW", unit: "hour" } } } },
  { $match: { hrs: { $gte: 24, $lt: 48 } } },
  { $count: "n" }
])

// Stored tags inside that cohort
db.getSiblingDB("warrantdb").simple_harris.aggregate([
  { $match: { booking_datetime: { $type: "date" } } },
  { $addFields: { hrs: { $dateDiff: { startDate: "$booking_datetime", endDate: "$$NOW", unit: "hour" } } } },
  { $match: { hrs: { $gte: 24, $lt: 48 } } },
  { $group: { _id: "$time_bucket_v2", n: { $sum: 1 } } },
  { $sort: { n: -1 } }
])
```

## 5) Maintenance utilities

- Rebucket stored tags so they keep aging: `scripts/rebucket_time_bucket_v2.js`
  - Preview (no writes):
    ```bash
    DRY_RUN=1 mongosh "$MONGO_URI/warrantdb" scripts/rebucket_time_bucket_v2.js
    ```
  - Apply for recent window (default 90 days):
    ```bash
    mongosh "$MONGO_URI/warrantdb" scripts/rebucket_time_bucket_v2.js
    ```
  - Limit the window:
    ```bash
    MAX_DAYS=14 mongosh "$MONGO_URI/warrantdb" scripts/rebucket_time_bucket_v2.js
    ```

- Backfill booking_datetime from strings/booking_date if needed: `scripts/backfill_booking_datetime_from_strings.js`
  - Dry-run:
    ```bash
    DRY_RUN=1 mongosh "$MONGO_URI/warrantdb" scripts/backfill_booking_datetime_from_strings.js
    ```
  - Apply:
    ```bash
    mongosh "$MONGO_URI/warrantdb" scripts/backfill_booking_datetime_from_strings.js
    ```

## 6) Recommended end-to-end flow (Harris)

1. Ingest:
   ```bash
  python3 -m scripts.run_ingestion --source harris_inmate
   ```
2. Normalize:
   ```bash
   python3 normalize_to_simple.py --county harris --debug
   ```
3. Post-normalize fix:
   ```bash
   python3 scripts/harris_post_normalize.py
   ```
4. Verify:
   ```bash
   mongosh "$MONGO_URI/warrantdb" scripts/check_time_bucket_v2.js
   ```

## 9) Email Roster Import (Harris) — support script

Use this when you receive roster files via email.

1. Drop files into `email_rosters/` at the repo root (supports `.csv` and `.xlsx`).
  - You can override the folder with env var `HARRIS_EMAIL_ROSTER_DIR`.
  - To keep files out of the repo, point `HARRIS_EMAIL_ROSTER_DIR` to a Dropbox (or other) folder, e.g. `~/Dropbox/WarrantDB/email_rosters`. The importer expands `~` and `$HOME` automatically.
2. Run the importer:
  ```bash
  python3 -m scripts.run_ingestion --source harris_email_roster
  ```
3. What it does:
  - Upserts each row into collection `harris_email_roster` (with `file_ref`, `loaded_at`).
  - Attempts to enrich existing `harris_bond`/`harris_misfel`/`harris_nafiling` by matching `spn`, `case_number`, or `name+dob`.
  - Adds new info like `facility`, `status`, `offense`, `booking_date`, `bond_amount` and pushes a `history` entry.
4. XLSX support requires `openpyxl` (already listed in requirements). If missing, install with `pip install openpyxl`.

Deduplicating weekly file repeats:
- Some emails reuse names like `BondNotif_Monday_AM` and `BondNotif_Changes_Monday_AM`. The importer uses a SHA‑256 content hash ledger (`harris_email_roster_files`) to skip exact duplicates automatically.
- Summary output includes `skipped_duplicates` and `skipped_files`.
- To force reprocessing a previously seen file (e.g., corrected content), modify the file contents or delete its hash document from `harris_email_roster_files`.

If you run inside Docker and want to read from a host Dropbox folder, bind mount it and point the env var to the container path:

```yaml
services:
  api:
    # ...existing config...
    volumes:
      - ~/Dropbox/WarrantDB/email_rosters:/app/email_rosters:ro
    environment:
      - HARRIS_EMAIL_ROSTER_DIR=/app/email_rosters
```

### 9.a) Auto-save rosters from email (IMAP)

Use the IMAP fetcher to automatically download CSV/XLSX attachments from your roster emails and save them into `HARRIS_EMAIL_ROSTER_DIR`. This is a support path to ensure Harris coverage beyond the main `harris_inmate` scraper.

Script: `scripts/fetch_email_rosters.py`

1. Enable IMAP and credentials
   - Gmail/Google Workspace: enable IMAP; create an App Password for your account.
   - Other providers: obtain IMAP host, username, and password.

2. Configure environment (example for Gmail)
```bash
export IMAP_HOST=imap.gmail.com
export IMAP_PORT=993
export IMAP_USERNAME="ryan@vintaragroup.com"
export IMAP_PASSWORD="<APP_PASSWORD>"
export ROSTER_EMAIL_FROM="alerts@harris.tx.us"        # adjust to actual sender or leave blank
export ROSTER_SUBJECT_INCLUDE="BondNotif,BondNotif_Changes"  # optional filters
export HARRIS_EMAIL_ROSTER_DIR=~/Dropbox/ASAP_bail/harris_county
```

3. Run the fetcher
```bash
python3 -m scripts.fetch_email_rosters
```

What it does:
- Scans recent messages (default 14 days; unseen by default) in INBOX.
- Filters by From/Subject if configured.
- Saves .csv/.xlsx (.xls optional) attachments into `HARRIS_EMAIL_ROSTER_DIR` (optionally in date subfolders).
- Deduplicates by SHA-256 so the same attachment isn’t saved twice.
- Records a ledger in collection `email_roster_inbox`.

Optional Dropbox mirroring:
- If you set `DROPBOX_ACCESS_TOKEN` (and optionally `DROPBOX_BASE_FOLDER`), each saved attachment is also uploaded to Dropbox.
- Default Dropbox target is `/warrantdb/email_rosters` with a `/YYYY-MM-DD` subfolder when `ROSTER_SAVE_BY_DATE=1`.
- This provides durable archival even when running on ephemeral environments like Render jobs.

Then run the importer to load/enrich into Mongo:
```bash
python3 -m scripts.run_ingestion --source harris_email_roster
```

Importer flags and behavior:
- Sheet handling: Processes all non-empty sheets except a sheet named "Document Map" (case-insensitive; spaces ignored). This covers Sheet1/Sheet 1/Sheet2… and any other data sheets.
- Ledger: Exact file content dedup via `harris_email_roster_files` (SHA-256). To reprocess the same content, either force or delete ledger entries.
- Force reprocess: Set `HARRIS_ROSTER_FORCE_REPROCESS=1` to ignore the ledger and parse files again.
- History dedup: Enrichment history pushes are deduped per file hash, tracked in `history_hashes` on the target doc.
- Debug: Set `HARRIS_ROSTER_DEBUG=1` to print per-file sheet names and per-sheet row counts.

Optional settings:
- `IMAP_UNSEEN_ONLY=0` to scan seen emails too.
- `IMAP_SINCE_DAYS=3` to tighten the window.
- `ROSTER_SAVE_BY_DATE=0` to save directly into the base folder (no date subfolders).
- `ROSTER_ALLOWED_EXT=".csv,.xlsx"` to restrict extensions.
- `MARK_SEEN=0` to leave emails unread.
- Forwarded email case: if a colleague forwards you the county emails, set `ROSTER_EMAIL_FROM` to the forwarder (e.g., `asaphtown@gmail.com`) and `ROSTER_ORIGINAL_FROM` to the county’s actual sender. The fetcher inspects nested message/rfc822 parts to match the original sender and extract attachments.

Cron examples (macOS):
```bash
crontab -e
# Fetch new emails at :10 every hour
10 * * * * IMAP_HOST=imap.gmail.com IMAP_PORT=993 IMAP_USERNAME='ryan@vintaragroup.com' IMAP_PASSWORD='<APP_PASSWORD>' ROSTER_EMAIL_FROM='alerts@harris.tx.us' ROSTER_SUBJECT_INCLUDE='BondNotif,BondNotif_Changes' HARRIS_EMAIL_ROSTER_DIR='$HOME/Dropbox/ASAP_bail/harris_county' /usr/bin/python3 -m scripts.fetch_email_rosters >> /tmp/imap_fetch.log 2>&1
# Then import at :15 every hour
15 * * * * HARRIS_EMAIL_ROSTER_DIR='$HOME/Dropbox/ASAP_bail/harris_county' /usr/bin/python3 -m scripts.run_ingestion --source harris_email_roster >> /tmp/roster_import.log 2>&1
```

## 10) Cloud sync: Dropbox + MongoDB Atlas

Goal: automatically pull roster files from Gmail to your Dropbox folder and sync data into a cloud MongoDB (Atlas) so everything is available remotely.

### A) Dropbox folder
- Use `~/Dropbox/ASAP_bail/harris_county` (or pick a path). Set:
  ```bash
  export HARRIS_EMAIL_ROSTER_DIR=~/Dropbox/ASAP_bail/harris_county
  ```
- The importer expands `~` and `$HOME`. The IMAP fetcher will create the path if missing.

### B) MongoDB Atlas setup
1. Create a free/shared cluster at MongoDB Atlas.
2. Add a database user and note the username/password.
3. Add your IP (or 0.0.0.0/0 for quick testing) to the network access allowlist.
4. Set `.env` in repo root:
   ```
   MONGO_URI="mongodb+srv://<user>:<pass>@<cluster-url>/?retryWrites=true&w=majority"
   MONGO_DB=warrantdb
   ```
5. Test connectivity from your machine:
   ```bash
   python3 - <<'PY'
import os
from storage.mongo_client import get_db
db = get_db()
print('Collections in', os.getenv('MONGO_DB', 'warrantdb'), ':', db.list_collection_names())
PY
   ```

### C) Gmail IMAP fetch
- Turn on IMAP on your Gmail/Workspace account.
- Create an App Password (Account Security -> App passwords) and save it.
- Minimal env for Gmail:
  ```bash
  export IMAP_HOST=imap.gmail.com
  export IMAP_PORT=993
  export IMAP_USERNAME="you@example.com"
  export IMAP_PASSWORD="<APP_PASSWORD>"
  export HARRIS_EMAIL_ROSTER_DIR=~/Dropbox/ASAP_bail/harris_county
  # Optional filters
  export ROSTER_EMAIL_FROM="alerts@harris.tx.us"
  export ROSTER_SUBJECT_INCLUDE="BondNotif,BondNotif_Changes"
  ```

### D) One-time run (updates Atlas)
```bash
# 1) Fetch attachments into Dropbox
python3 -m scripts.fetch_email_rosters

# 2) Import into MongoDB Atlas
python3 -m scripts.run_ingestion --source harris_email_roster
```

### E) Hourly automation (cron)
Option 1 — run steps separately (as above in 9.a).

Option 2 — use the wrapper script `scripts/cloud_sync.sh` that loads `.env`, ensures the Dropbox folder, and runs both steps:
```bash
crontab -e
# run at :10 every hour
10 * * * * /bin/bash -lc 'cd /Users/ryanmorrow/Documents/Projects2025/WarrentDB/warrantdb-pipeline && bash scripts/cloud_sync.sh >> /tmp/cloud_sync.log 2>&1'
```

### F) Quick verification
- Files saved: `ls -l ~/Dropbox/ASAP_bail/harris_county` (expect CSV/XLS/XLSX files). If subfolders by date are enabled, check inside the dated folders.
- Import result: The importer prints a JSON summary including `inserted_roster`, `updated_existing`, `skipped_duplicates`.
- Data in Atlas:
  ```javascript
  // In mongosh, after connecting to your cluster:
  db.getSiblingDB('warrantdb').harris_email_roster.countDocuments()
  db.getSiblingDB('warrantdb').harris_email_roster.find().sort({loaded_at: -1}).limit(3)
  ```

### G) Troubleshooting
- Gmail finds 0 messages: widen window `IMAP_SINCE_DAYS=30`, set `IMAP_UNSEEN_ONLY=0`, and ensure the script lists and searches `[Gmail]/All Mail` (see DEBUG output). You can set `FETCH_DEBUG=1` for verbose logs.
- App Password missing in Workspace: admin must allow App Passwords; otherwise use a provider that supports per-app passwords or an IMAP proxy with OAuth2.
- Attachments saved = 0 but duplicates > 0: files already fetched earlier — dedup ledger prevents re-saving. Clear dedup only if necessary.
- Atlas connection fails: verify IP allowlist, user/pass, and that `MONGO_URI` uses `mongodb+srv://` with `retryWrites=true`.

## 7) Scheduling (optional)

Use cron to keep buckets fresh automatically.

Hourly rebucket for last 14 days (macOS):
```bash
crontab -e
# add:
15 * * * * MONGO_URI='YOUR_ATLAS_URI' MAX_DAYS=14 mongosh "$MONGO_URI/warrantdb" /Users/ryanmorrow/Documents/Projects2025/WarrentDB/warrantdb-pipeline/scripts/rebucket_time_bucket_v2.js >> /tmp/rebucket.log 2>&1
```

## 8) Adding a new scraper/process

- Implement the scraper class in `ingestion/<new_source>.py`.
- Add it to `SCRAPER_SPECS` in `scripts/run_ingestion.py`.
- If normalization needs updates, extend `normalize_to_simple.py` accordingly.
- If the county requires a post-normalize step, add a new script under `scripts/` and document it here.
- Update this RUNBOOK with usage examples and any verification steps.

---

For data schema details and field guarantees, see `SCHEMA_CONTRACT.md`.
