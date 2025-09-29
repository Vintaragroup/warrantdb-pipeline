#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   MONGO_URI="..." [DB_NAME=warrantdb] [MAX_DAYS=90] [DRY_RUN=0] ./scripts/run_rebucket.sh
#
# Notes:
# - Requires mongosh in PATH.
# - Uses scripts/rebucket_time_bucket_v2.js which reads DB_NAME, MAX_DAYS, DRY_RUN from env.

: "${MONGO_URI:?MONGO_URI is required}"
DB_NAME="${DB_NAME:-warrantdb}"
MAX_DAYS="${MAX_DAYS:-90}"
DRY_RUN="${DRY_RUN:-0}"

export DB_NAME MAX_DAYS DRY_RUN

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[run_rebucket] DB_NAME=${DB_NAME} MAX_DAYS=${MAX_DAYS} DRY_RUN=${DRY_RUN}"
exec mongosh "${MONGO_URI}/${DB_NAME}" "${SCRIPT_DIR}/rebucket_time_bucket_v2.js"
