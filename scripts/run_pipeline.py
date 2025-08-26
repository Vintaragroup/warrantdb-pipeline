

import os
import sys
import subprocess
from datetime import datetime
from typing import List

# ---------------------------------------------------------------------------
# Wrapper pipeline that orchestrates your existing scripts WITHOUT modifying
# any of your working code. It:
#   1) Runs each scraper via scripts.run_ingestion --source <name>
#   2) Runs your normalizer (scripts.normalize_to_simple)
#   3) Runs the delta reporter (scripts.report_simple_deltas)
# Configure behavior with environment variables (optional):
#   PIPELINE_SOURCES: comma-separated list of sources to run
#                     default: harris_inmate,galveston_p2c_fast,jefferson_jail
#   PIPELINE_STEPS:   comma-separated subset of steps to execute
#                     choices: ingest,normalize,report (default: all three)
# ---------------------------------------------------------------------------

DEFAULT_SOURCES: List[str] = [
    "harris_inmate",
    "galveston_p2c_fast",
    "jefferson_jail",
    # Add when ready:
    # "brazoria_jail",
    # "fortbend_jail",
]

NORMALIZER_MODULE = "scripts.normalize_to_simple"
REPORT_MODULE = "scripts.report_simple_deltas"


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _log(msg: str) -> None:
    print(f"[{_now()}] {msg}")


def run_cmd(cmd: List[str]) -> int:
    """Run a subprocess, stream output, and return the exit code."""
    _log("RUN → " + " ".join(cmd))
    proc = subprocess.Popen(cmd)
    proc.wait()
    code = proc.returncode
    if code == 0:
        _log("OK  ← " + " ".join(cmd))
    else:
        _log(f"ERR ← (exit {code}) " + " ".join(cmd))
    return code


def get_sources() -> List[str]:
    raw = os.getenv("PIPELINE_SOURCES")
    if not raw:
        return DEFAULT_SOURCES
    # allow commas and whitespace
    return [s.strip() for s in raw.split(",") if s.strip()]


def get_steps() -> List[str]:
    raw = os.getenv("PIPELINE_STEPS")
    if not raw:
        return ["ingest", "normalize", "report"]
    steps = [s.strip().lower() for s in raw.split(",") if s.strip()]
    valid = {"ingest", "normalize", "report"}
    return [s for s in steps if s in valid]


def step_ingest(sources: List[str]) -> int:
    _log("STEP: ingest")
    failures = 0
    for src in sources:
        code = run_cmd([sys.executable, "-m", "scripts.run_ingestion", "--source", src])
        if code != 0:
            failures += 1
    return 0 if failures == 0 else 1


def step_normalize() -> int:
    _log("STEP: normalize")
    return run_cmd([sys.executable, "-m", NORMALIZER_MODULE])


def step_report() -> int:
    _log("STEP: report")
    return run_cmd([sys.executable, "-m", REPORT_MODULE])


def main() -> int:
    sources = get_sources()
    steps = get_steps()

    _log(f"Pipeline start | steps={steps} | sources={sources}")

    overall_rc = 0

    if "ingest" in steps:
        rc = step_ingest(sources)
        overall_rc = overall_rc or rc

    if "normalize" in steps:
        rc = step_normalize()
        overall_rc = overall_rc or rc

    if "report" in steps:
        rc = step_report()
        overall_rc = overall_rc or rc

    _log(f"Pipeline finished | exit={overall_rc}")
    return overall_rc


if __name__ == "__main__":
    raise SystemExit(main())