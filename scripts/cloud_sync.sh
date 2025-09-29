#!/usr/bin/env bash
# Simple, cron-friendly wrapper to fetch roster emails and import them into MongoDB.
# - Loads .env if present
# - Ensures HARRIS_EMAIL_ROSTER_DIR exists (defaults to ./email_rosters)
# - Runs IMAP fetcher then importer
#
# Usage (manual):
#   bash scripts/cloud_sync.sh
#
# Cron example (macOS):
#   10 * * * * /bin/bash -lc 'cd /Users/ryanmorrow/Documents/Projects2025/WarrentDB/warrantdb-pipeline && bash scripts/cloud_sync.sh >> /tmp/cloud_sync.log 2>&1'

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Load .env if present (export all vars)
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# Default roster dir if not set
HARRIS_EMAIL_ROSTER_DIR=${HARRIS_EMAIL_ROSTER_DIR:-"$HOME/Dropbox/ASAP_bail/harris_county"}
export HARRIS_EMAIL_ROSTER_DIR

# Ensure the directory exists
mkdir -p "$HARRIS_EMAIL_ROSTER_DIR"

# Python executable (prefer venv if available)
PY=python3
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
fi

# 1) Fetch attachments via IMAP
# Respect optional IMAP_* and ROSTER_* env vars already exported
$PY -m scripts.fetch_email_rosters || true

# 2) Import into Mongo
$PY -m scripts.run_ingestion --source harris_email_roster

echo "Cloud sync complete at $(date)"
