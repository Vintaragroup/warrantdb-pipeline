# Scheduling the Warrant Pipeline (twice daily + idempotent writes)

This repo already includes:
- `scripts/run_pipeline.py` orchestrating ingestion ➜ normalize ➜ delta report
- Upserts for persons via `BaseScraper.upsert_person()`
- Normalized *simple_* collections with stable `_upsert_key` (idempotent)
- Optional audit logs in `scrape_audit`

Below are three production-ready scheduling options. All assume Python 3.11+ and a `.env` with `MONGO_URI` and `MONGO_DB`.

---

## Option A — crontab (Linux/macOS)

1) Ensure your venv and env are set:

```bash
cd /opt/warrantdb-pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Put MONGO_URI and MONGO_DB in .env (or export them in the crontab line)
```

2) Create indexes (one-time, safe to re-run):
```bash
python -m scripts.setup_indexes
python -m scripts.setup_indexes_extra
python -m scripts.setup_indexes_events
```

3) Add to crontab (runs 5:05 AM and 5:05 PM **America/New_York**):
```cron
# WarrantDB twice-daily pipeline
5 5,17 * * * cd /opt/warrantdb-pipeline &&   /usr/bin/env -S bash -lc 'source .venv/bin/activate &&   export TZ=America/New_York &&   export PIPELINE_SOURCES="harris_inmate,galveston_p2c_fast,jefferson_jail,fortbend_jail,brazoria_jail" &&   export PIPELINE_STEPS="ingest,normalize,report" &&   export JEFF_MIN_LAST_LEN=2 JEFF_MIN_FIRST_LEN=1 JEFF_SEARCH_DELAY_SEC=1 JEFF_ROW_DELAY_SEC=0.4 JEFF_REQ_TIMEOUT=30 &&   python -m scripts.run_pipeline >> logs/pipeline.$(date +\%F).log 2>&1'
```

> Tip: Make `/opt/warrantdb-pipeline/logs/` first. Log rotation can be handled by `logrotate`.

---

## Option B — systemd timer (Ubuntu/Debian)

Create `/etc/systemd/system/warrantdb.service`:

```ini
[Unit]
Description=WarrantDB twice-daily pipeline

[Service]
Type=oneshot
WorkingDirectory=/opt/warrantdb-pipeline
Environment=TZ=America/New_York
Environment=PIPELINE_SOURCES=harris_inmate,galveston_p2c_fast,jefferson_jail,fortbend_jail,brazoria_jail
Environment=PIPELINE_STEPS=ingest,normalize,report
Environment=JEFF_MIN_LAST_LEN=2
Environment=JEFF_MIN_FIRST_LEN=1
Environment=JEFF_SEARCH_DELAY_SEC=1
Environment=JEFF_ROW_DELAY_SEC=0.4
Environment=JEFF_REQ_TIMEOUT=30
ExecStart=/bin/bash -lc 'source .venv/bin/activate && python -m scripts.run_pipeline >> logs/pipeline.$(date +%%F).log 2>&1'
```

Create `/etc/systemd/system/warrantdb.timer`:

```ini
[Unit]
Description=Run WarrantDB pipeline twice daily

[Timer]
OnCalendar=05:05,17:05
Persistent=true

[Install]
WantedBy=timers.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now warrantdb.timer
```

---

## Option C — Render Cron Jobs (recommended if you deploy on Render)

1) Deploy this repo as a Background Worker on Render with `.env` (MONGO_URI, MONGO_DB).  
2) Create **two** Render Cron Jobs (UTC-based) that run the worker command:
```
render run python -m scripts.run_pipeline
```
Schedule them at 09:05 UTC and 21:05 UTC (which correspond to 5:05 AM/PM ET while DST is active). Update if DST changes.

You can pass env vars in the Worker’s settings (PIPELINE_SOURCES, PIPELINE_STEPS, JEFF_*).

---

## Idempotency & “Only Add New Data”

- **Persons**: `ingestion/base_scraper.py` uses a stable upsert key: `_ext_id` → booking number → (full_name, dob). This prevents duplicates naturally.
- **Events**: add the unique index below so we don’t double-insert the same custody event (same `person_id` and `source_url` or same `booking_number`). See `scripts/setup_indexes_events.py` included in this patch.
- **Normalized**: `scripts/normalize_to_simple.py` upserts into `simple_*` collections via `_upsert_key`. Safe to run repeatedly.

---

## Quick sanity check

```bash
# One-off run
python -m scripts.run_pipeline

# Limit to ingestion only:
PIPELINE_STEPS=ingest python -m scripts.run_pipeline

# Run only specified sources:
PIPELINE_SOURCES="jefferson_jail,galveston_p2c_fast" python -m scripts.run_pipeline

---

## Optional: Nightly DOB enrichment (Harris, last 24h)

Run this once per night after normalization to enrich recent Harris entries with DOB from HCSO. The script only targets rows missing DOB by default.

```bash
# Cron example (runs at 2:15 AM local time; adjust as needed)
15 2 * * * cd /opt/warrantdb-pipeline && /usr/bin/env -S bash -lc 'source .venv/bin/activate && python -m scripts.enrich_harris_dob --limit 200 --window 24h >> logs/enrich_harris_dob.$(date +\%F).log 2>&1'
```

For Render, create a Cron Job that runs your Worker command:

```
render run python -m scripts.enrich_harris_dob --limit 200 --window 24h
```

Environment variables required:
- HCSO_SPN_URL_FMT, HCSO_NAME_URL_FMT
- Optional: HCSO_THROTTLE_SEC, HCSO_TIMEOUT_SEC, HCSO_BETWEEN_PEOPLE_SEC
```
