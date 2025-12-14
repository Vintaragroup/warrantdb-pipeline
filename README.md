# WarrantDB Pipeline

This repo contains a production-focused pipeline for multiple county sources. It includes:
- Source scrapers (ingestion)
- Normalization to `simple_*` collections
- Post-normalization maintenance utilities
- Scheduling helpers
- A support script for Harris: importing roster spreadsheets delivered by email to ensure full daily coverage

## Components
- **configs/**: Per‑county endpoints and schedules
- **ingestion/**: Source-specific scrapers
- **enrichment/**: Extra data pulls (cases, public records)
- **entity_resolution/**: Match/merge logic for Person entities
- **storage/**: Mongo client and schemas
- **api/**: Read-only service your agent hits
- **scripts/**: CLI entry points

## Quick Start

### 1) Environment
Create `.env` in project root (see `.env.example`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# (Optional) If you plan to use Playwright:
# python -m playwright install chromium
```

### 2) Run API
```bash
uvicorn api.main:app --reload --port 8080
```

### 3) Seed Mongo (optional)
```bash
python scripts/run_ingestion.py --source harris_inmate
```

### Notes
- All writes go through `storage/mongo_client.py` and schema helpers in `storage/schemas.py`.
- Keep provenance (source_url, scraped_at) on every record.
- See `RUNBOOK.md` for full operational docs and `SCHEDULING.md` for twice-daily orchestration.

## Harris roster support script

Harris County rosters sometimes arrive as spreadsheets via email. This repo provides:
- `scripts/fetch_email_rosters.py` to auto-save CSV/XLS/XLSX attachments to a folder
- `ingestion/harris_email_roster.py` to import/enrich these rows into MongoDB

Start here to run just the Harris roster support flow:
1) Configure `.env` (see `.env.example`) for MongoDB, Dropbox roster folder, and IMAP.
2) Fetch attachments: `python3 -m scripts.fetch_email_rosters`
3) Import rosters: `python3 -m scripts.run_ingestion --source harris_email_roster`

Detailed guidance, cron examples, and verification steps are in `RUNBOOK.md` (sections 9 and 10).

## Harris DOB enrichment (HCSO)

Add Date of Birth to `simple_harris` by querying HCSO using SPN or name:

1) Configure environment (URLs are intentionally env-driven and may change over time):
	- `HCSO_SPN_URL_FMT`  e.g. `https://example.harriscounty.gov/inmate?spn={spn}`
	- `HCSO_NAME_URL_FMT` e.g. `https://example.harriscounty.gov/inmate?last={last}&first={first}`
	- Optional: `HCSO_THROTTLE_SEC`, `HCSO_TIMEOUT_SEC`, `HCSO_BETWEEN_PEOPLE_SEC`

2) Create indexes (optional but recommended):
	- `python -m scripts.setup_indexes_extra` (adds `simple_harris.simplified` indexes incl. `spn`, `time_bucket_v2`)

3) Run enrichment:
	- `python -m scripts.enrich_harris_dob --limit 250 --window 30d`
	- Accepts `--all` (include rows with existing dob), `--prefix ADAMS` (last-name prefix), `--dry-run`.

Provenance is stored in each updated document: `dob_source='hcso'`, `dob_source_url`, `dob_confidence`, and `dob_checked_at`.
