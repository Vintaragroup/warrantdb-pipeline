# Nightly Maintenance Jobs

This repo includes a small nightly maintenance flow for `simple_harris` to keep fields clean:

- Strip directive phrases from addresses (e.g., "REFER TO MAGISTRATE …")
- Mark and unset invalid SPNs (e.g., name accidentally placed into SPN)

## Script

`scripts/nightly_simple_harris.sh`

Behavior (default window: 24h):
- Runs a scanner for a summary
- Applies address cleaning
- Marks invalid SPNs then unsets them, preserving `spn_bad`, `spn_flagged`, and `spn_flag_reason`

Env requirements (loaded by Python code):
- `MONGO_URI` – Atlas connection string
- `MONGO_DB` – database name

Optional:
- `WINDOW` – one of `24h, 48h, 72h, 7d, 30d, 60d` (default `24h`)

## Render Cron setup

Two options:

1) As a background job under an existing Render service (preferred):
   - Ensure your service image or build produces a Python venv at `.venv` and includes repo code.
   - Command: `bash scripts/nightly_simple_harris.sh`
   - Schedule: `0 7 * * *` (7:00 UTC ≈ 2:00 AM Central Standard; adjust for DST as needed)
   - Environment: set `MONGO_URI`, `MONGO_DB`, and (optional) `WINDOW`.

2) As a separate Cron Job resource:
   - Runtime: Docker or Node/Static with bash support
   - Command: `bash scripts/nightly_simple_harris.sh`
   - Schedule: `0 7 * * *`
   - Environment: `MONGO_URI`, `MONGO_DB`, `WINDOW`

## Local test

You can test locally with your venv:

```bash
export MONGO_URI="..."
export MONGO_DB="..."
export WINDOW=24h
bash scripts/nightly_simple_harris.sh
```

Logs will show the scanner summary and the counts from the fixer steps. If anything looks off, re-run the scanner manually:

```bash
./.venv/bin/python -m scripts.scan_anomalies_simple_harris --window 24h --samples 10 --limit 100000
```
