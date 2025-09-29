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
