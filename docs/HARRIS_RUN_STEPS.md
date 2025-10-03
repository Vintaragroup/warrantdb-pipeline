# Harris: Daily Run Steps (Simple & Direct)

This is the minimal set of commands to keep Harris data up to date.

Prereqs:
- Ensure `.env` has valid `MONGO_URI` and `MONGO_DB`.
- Use the repo’s virtualenv Python via `./.venv/bin/python` (no activation needed).

## 0) One-command end-to-end (recommended)

Run the entire Harris flow with clear progress logs:

```bash
./.venv/bin/python -m scripts.run_harris_e2e
```

Optional: choose specific steps (comma-separated): fetch, roster, ingest, normalize, rebucket, report

```bash
HARRIS_E2E_STEPS="fetch,roster,ingest,normalize,rebucket,report" \
./.venv/bin/python -m scripts.run_harris_e2e
```

Optional: continue past a failed step (e.g., if ingest fails due to an HTML error page) and still run later steps:

```bash
HARRIS_E2E_CONTINUE_ON_ERROR=1 \
./.venv/bin/python -m scripts.run_harris_e2e
```

The runner just orchestrates existing scripts and respects all env vars below.

Set-and-forget options:

- Allow slightly stale daily files (site lag):

```bash
HARRIS_ALLOW_STALE=1 HARRIS_STALE_MAX_DAYS=2 \
./.venv/bin/python -m scripts.run_harris_e2e
```

- Continue even if a step fails (finish normalize/rebucket/report):

```bash
HARRIS_E2E_CONTINUE_ON_ERROR=1 \
./.venv/bin/python -m scripts.run_harris_e2e
```

- Retry each step once and emit a JSON summary to logs/:

```bash
HARRIS_E2E_RETRY=1 HARRIS_E2E_RETRY_DELAY=10 HARRIS_E2E_WRITE_SUMMARY=1 \
./.venv/bin/python -m scripts.run_harris_e2e
```

- Cron example (UTC 02:00 daily) with append logs and summary:

```bash
# crontab -e
0 2 * * * cd /Users/ryanmorrow/Documents/Projects2025/WarrentDB/warrantdb-pipeline && \
  HARRIS_ALLOW_STALE=1 HARRIS_STALE_MAX_DAYS=2 HARRIS_E2E_RETRY=1 HARRIS_E2E_WRITE_SUMMARY=1 \
  ./.venv/bin/python -m scripts.run_harris_e2e >> logs/harris_e2e.log 2>&1
```

### What the E2E runner does

In order, with retries and clear logging:
- Fetch email rosters (IMAP) → saves attachments into HARRIS_EMAIL_ROSTER_DIR
- Import rosters → inserts into harris_email_roster and enriches existing harris_* docs
- Ingest Harris inmate datasets → pulls the six datasets (Civil/Criminal × bond/misfel/nafiling), resilient to site format changes (date-only files OK)
- Normalize → recompute simple_harris from raw, enforcing time_bucket strictly from booking_date
- Rebucket safety → server-side recompute to guarantee aging tags are correct
- Report deltas → summarize simple_harris changes (no long-lived cursor; Atlas-safe)

Notes:
- The runner loads .env so MONGO_*/IMAP_* are respected across steps.
- On ingest failure due to HTML/error pages, the runner prints a ready-to-copy HARRIS_PATH_OVERRIDES example for today.

## Canonical time buckets (v2)

The frontend/API read time_bucket_v2 with these tags derived from booking_datetime (America/Chicago semantics for date-only/naive inputs):

- 0_24h: 0 ≤ age < 24h
- 24_48h: 24h ≤ age < 48h
- 48_72h: 48h ≤ age < 72h
- 3d_7d: 72h ≤ age < 7d
- 7d_30d: 7d ≤ age < 30d
- 30d_60d: 30d ≤ age < 60d
- 60d_plus: age ≥ 60d

Normalization now derives booking_datetime (UTC ISO8601) from first_seen_at → updated_at → booking_date and sets booking_date_v2 (Central date). time_bucket_v2 is computed from booking_datetime using the half‑open ranges above. Legacy time_bucket remains for continuity but is not used by the API.

Backfill/repair utilities:

```bash
# Preview booking_datetime/time_bucket_v2 backfill
python -m scripts.backfill_booking_datetime_harris --dry-run

# Apply backfill
python -m scripts.backfill_booking_datetime_harris --batch 500

# Recompute v2 buckets (idempotent)
python -m scripts.rebucket_simple_harris_v2 --batch 1000
```

## 1) Ingestion scraper (Harris inmate feeds)

Default robust run (tolerates minor site drift):

```bash
LOG_LEVEL=DEBUG HARRIS_ALLOW_STALE=1 HARRIS_STALE_MAX_DAYS=2 HARRIS_FALLBACK_DAYS=7 \
./.venv/bin/python -m scripts.run_ingestion --source harris_inmate
```

If discovery fails (site changed), override paths for today (mm-dd-yy):

```bash
HARRIS_PATH_OVERRIDES='{
  "Civil/bond":"Civil/10-01-25-bond.txt",
  "Civil/misfel":"Civil/10-01-25-misfel.txt",
  "Civil/nafiling":"Civil/10-01-25-nafiling.txt",
  "Criminal/bond":"Criminal/10-01-25-bond.txt",
  "Criminal/misfel":"Criminal/10-01-25-misfel.txt",
  "Criminal/nafiling":"Criminal/10-01-25-nafiling.txt"
}' \
./.venv/bin/python -m scripts.run_ingestion --source harris_inmate
```

## 2) Roster support (Email → Dropbox → Import)

Fetch roster attachments from IMAP to your configured folder:

```bash
./.venv/bin/python -m scripts.fetch_email_rosters
```

Import rosters (recursively parses CSV/XLSX/XLS, skips "Document Map"):

```bash
HARRIS_ROSTER_DEBUG=1 \
./.venv/bin/python -m scripts.run_ingestion --source harris_email_roster
```

Optional: force reprocess already-seen files after logic changes:

```bash
HARRIS_ROSTER_DEBUG=1 HARRIS_ROSTER_FORCE_REPROCESS=1 \
./.venv/bin/python -m scripts.run_ingestion --source harris_email_roster
```

## 3) Normalize to `simple_harris`

Normalize Harris bond into `simple_harris` (idempotent):

```bash
./.venv/bin/python normalize_to_simple.py --county harris --debug
```

## 4) Bucket alignment (safety job)

Guarantee aging tags are strictly based on `booking_date` and remove tags when missing:

```bash
./.venv/bin/python -m scripts.rebucket_simple_harris
```

Optional: nightly cron example (runs at 02:05 UTC):

```bash
# crontab -e
5 2 * * * cd /Users/ryanmorrow/Documents/Projects2025/WarrentDB/warrantdb-pipeline \
&& ./.venv/bin/python -m scripts.rebucket_simple_harris >> logs/rebucket.log 2>&1
```

---

Notes:
- All commands are safe to re-run (idempotent upserts/updates).
- If the Harris site serves an error page, the inmate scraper will abort instead of inserting garbage; use the overrides block above.
 - The end-to-end runner prints timestamped lines per step and exits immediately on the first failure (nonzero exit).
 - To keep going after a failure (e.g., still normalize and rebucket), set HARRIS_E2E_CONTINUE_ON_ERROR=1.

## Verify frontend tags (mongosh snippets)

Connect to Atlas with your user, then run these in mongosh. Replace `warrantdb` if your DB name differs.

1) Counts by time_bucket in `simple_harris` (legacy):

```javascript
use warrantdb
db.simple_harris.aggregate([
  { $group: { _id: { $ifNull: ["$time_bucket", "missing"] }, count: { $sum: 1 } } },
  { $sort: { count: -1 } }
])
```

2) Counts by time_bucket_v2 (API canonical):

```javascript
db.simple_harris.aggregate([
  { $group: { _id: { $ifNull: ["$time_bucket_v2", "missing"] }, count: { $sum: 1 } } },
  { $sort: { count: -1 } }
])
```

3) Sanity: legacy buckets only when booking_date exists; check how many docs lack booking_date:

```javascript
db.simple_harris.countDocuments({ booking_date: { $exists: false } })
```

4) Quick samples per bucket (inspect in UI logs):

```javascript
db.simple_harris.find({ time_bucket: "24_hours_or_less" }, { full_name: 1, booking_date: 1, time_bucket: 1 }).limit(5)
db.simple_harris.find({ time_bucket: "48_hours" }, { full_name: 1, booking_date: 1, time_bucket: 1 }).limit(5)
db.simple_harris.find({ time_bucket: "72_hours" }, { full_name: 1, booking_date: 1, time_bucket: 1 }).limit(5)
```

5) If you also want to see raw collection “booking_age_category” (legacy, used during ingest):

```javascript
db.harris_bond.aggregate([{ $group: { _id: { $ifNull: ["$booking_age_category", "unknown"] }, count: { $sum: 1 } } }, { $sort: { count: -1 } }])
db.harris_misfel.aggregate([{ $group: { _id: { $ifNull: ["$booking_age_category", "unknown"] }, count: { $sum: 1 } } }, { $sort: { count: -1 } }])
db.harris_nafiling.aggregate([{ $group: { _id: { $ifNull: ["$booking_age_category", "unknown"] }, count: { $sum: 1 } } }, { $sort: { count: -1 } }])
```

Tip: the runner’s rebucket step keeps `simple_harris.time_bucket` aligned nightly, purely from `booking_date`.

## Health check: legacy + v2

Run this to verify legacy time_bucket derives strictly from `booking_date`, see counts, v2 coverage, and any mismatches:

```bash
./.venv/bin/python -m scripts.health_simple_harris
```

Optional webhook notification (POST JSON on each run):

```bash
HEALTH_WEBHOOK_URL="https://your-webhook-url" \
./.venv/bin/python -m scripts.health_simple_harris
```

Cron example (runs after E2E):

```bash
15 2 * * * cd /Users/ryanmorrow/Documents/Projects2025/WarrentDB/warrantdb-pipeline && \
  ./.venv/bin/python -m scripts.health_simple_harris >> logs/health_harris.log 2>&1
```

## Frontend integration: API contract

The API exposes read endpoints tailored for the frontend to consume the v2 buckets and row data. All fields below come from the `simple_harris` collection and are stable.

1) Summary (v2 buckets + coverage)

GET /simple/harris/summary

Response shape:

- date: ISO string (UTC)
- by_bucket_v2: array of { _id: one of [0_24h,24_48h,48_72h,3d_7d,7d_30d,30d_60d,60d_plus,missing], count: number }
- windows: { 24h, 48h, 72h, 7d, 30d } rollups based on v2 buckets
- coverage: { total, pct_booking_datetime, pct_time_bucket_v2 }

2) Inmates list (supports v2 filter, paging, and sort)

GET /simple/harris/inmates?bucket_v2=7d_30d&limit=50&skip=0&sort=-booking_datetime

Query parameters:

- bucket_v2 (optional): filter by a canonical v2 tag: 0_24h | 24_48h | 48_72h | 3d_7d | 7d_30d | 30d_60d | 60d_plus
- limit (default 200, max 1000): page size
- skip (default 0): offset for paging
- sort (default -booking_datetime): supports +/- prefix. Typical fields: booking_datetime, booking_date_v2.

Response shape:

- count: number of items in this page
- skip, limit: echo of paging inputs
- items: array of rows with these fields:
  - county: "harris"
  - category: "Civil" | "Criminal" (if present)
  - case_number: digits prefix format
  - anchor: stable upsert key, generally case_number or spn fallback
  - full_name: "LAST, FIRST MIDDLE"
  - charge: string
  - status: derived status string
  - bond_amount: number | null
  - bond_label: string (e.g., "REFER TO MAGISTRATE", may be empty)
  - booking_datetime: ISO8601 UTC (e.g., 2025-08-22T05:00:00Z)
  - booking_date_v2: YYYY-MM-DD (America/Chicago date)
  - time_bucket_v2: one of v2 canonical tags
  - address: object | null; shape: { line1, city, state?, zip } — not all keys guaranteed
  - tags: [] (reserved for future enrichment)
  - normalized_at: ISO8601 string

Notes:

- Address field is a passthrough from Harris source; some zip values may have trailing punctuation like ";". Frontend should trim non-digit suffixes from zip for display.
- time_bucket_v2 is computed from booking_datetime with Central Time interpretation for date-only inputs; values drift with time, so expect yesterday’s 24_48h to become 48_72h, etc.
- Legacy time_bucket exists but should not be used in new UI; it’s based strictly on booking_date and kept for parity.

3) Buckets breakdown (canonical order)

GET /simple/harris/buckets

Response shape:

- by_bucket_v2: array ordered as [0_24h, 24_48h, 48_72h, 3d_7d, 7d_30d, 30d_60d, 60d_plus, missing]
- total: total number of `simple_harris` documents (excludes "missing")

This is a lightweight way for FE or ops to confirm that "new today" are in 0_24h and older items have shifted to subsequent buckets.

---

Operational note: Normalizer logging

The normalizer prints progress and writes a per-run log file via the E2E runner. You can tune it via env:

```bash
# defaults used by E2E
HARRIS_BATCH_SIZE=2000
HARRIS_BULK_SIZE=1000
HARRIS_PROGRESS_EVERY=1000
HARRIS_LOG_LEVEL=INFO
# E2E creates logs/normalize_harris_<timestamp>.log if HARRIS_LOG_FILE is not set
```

Run normalizer standalone with explicit flags:

```bash
./.venv/bin/python normalize_to_simple.py \
  --county harris \
  --batch-size 2000 \
  --bulk-size 1000 \
  --progress-every 1000 \
  --log-level INFO \
  --log-file logs/normalize_harris_$(date +%Y%m%dT%H%M%S).log
```
