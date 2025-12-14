#!/usr/bin/env bash
set -euo pipefail

# Nightly maintenance for simple_harris (default window 24h)
# - Scan anomalies (summary only)
# - Clean directive prefixes in address
# - Mark invalid SPNs and unset them
#
# Env required: MONGO_URI, MONGO_DB (loaded by storage/mongo_client.py)
# Optional: WINDOW (24h, 48h, 72h, 7d, 30d, 60d)

WINDOW="${WINDOW:-24h}"
PY="${PY:-$(dirname "$0")/../.venv/bin/python}"

echo "[nightly] window=$WINDOW at $(date -u +%FT%TZ)"

# 1) Scan (summary to logs)
$PY -m scripts.scan_anomalies_simple_harris --window "$WINDOW" --samples 3 --limit 100000 || true

# 2) Clean addresses
$PY -m scripts.fix_anomalies_simple_harris --window "$WINDOW" --fix clean_address --apply

# 3) Mark bad SPNs, then unset them
$PY -m scripts.fix_anomalies_simple_harris --window "$WINDOW" --fix mark --apply
$PY -m scripts.fix_anomalies_simple_harris --window "$WINDOW" --fix unset_spn --apply

echo "[nightly] done at $(date -u +%FT%TZ)"
