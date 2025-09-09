#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Load env (MONGO_URI, MONGO_DB) if present
if [ -f ".env" ]; then
  set -a
  source .env
  set +a
fi

export TZ=America/New_York
export PIPELINE_SOURCES="${PIPELINE_SOURCES:-harris_inmate,galveston_p2c_fast,jefferson_jail,fortbend_jail,brazoria_jail}"
export PIPELINE_STEPS="${PIPELINE_STEPS:-ingest,normalize,report}"

# Jefferson tuning
export JEFF_MIN_LAST_LEN="${JEFF_MIN_LAST_LEN:-2}"
export JEFF_MIN_FIRST_LEN="${JEFF_MIN_FIRST_LEN:-1}"
export JEFF_SEARCH_DELAY_SEC="${JEFF_SEARCH_DELAY_SEC:-1}"
export JEFF_ROW_DELAY_SEC="${JEFF_ROW_DELAY_SEC:-0.4}"
export JEFF_REQ_TIMEOUT="${JEFF_REQ_TIMEOUT:-30}"

python -m scripts.run_pipeline
