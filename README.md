# WarrantDB Pipeline (Starter)

A modular pipeline for discovering county sources, ingesting warrant/bond/jail data, enriching with public records,
performing entity resolution, and exposing a FastAPI for your voice agent.

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
- This is a **starter scaffold**. Scrapers include TODOs for selectors and parsing logic.
- All writes go through `storage/mongo_client.py` and schema helpers in `storage/schemas.py`.
- Keep **provenance** (source_url, scraped_at) on every record.
