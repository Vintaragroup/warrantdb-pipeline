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