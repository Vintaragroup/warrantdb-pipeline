#!/usr/bin/env python3
"""
Harris end-to-end runner
------------------------
Runs the full Harris flow with simple timestamped progress logs:

  1) Fetch email rosters (IMAP attachments) -> local folder
  2) Import rosters into MongoDB and enrich existing Harris docs
  3) Ingest Harris inmate datasets (civil/criminal feeds)
  4) Normalize to simple_harris
  5) Recompute time_bucket strictly from booking_date (safety)
  6) Report deltas (new/changed) for simple_harris

Control which steps to run with HARRIS_E2E_STEPS (comma-separated):
  fetch, roster, ingest, normalize, rebucket, report
Default: all steps in the order above.

Usage:
  python3 -m scripts.run_harris_e2e

Notes:
  - The script uses your existing scripts/modules; it doesn't change data logic.
  - It prints clear start/finish lines for each step and propagates nonzero exits.
  - Honor all existing env vars (HARRIS_PATH_OVERRIDES, HARRIS_ROSTER_* etc.).
"""

from __future__ import annotations

import os
import sys
import subprocess
from datetime import datetime, timezone
from typing import List
import time, json
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None


ALL_STEPS = ["fetch", "roster", "ingest", "normalize", "rebucket", "report"]


def _now() -> str:
    # timezone-aware UTC to avoid deprecation warnings
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[{_now()}] {msg}")


def _parse_steps() -> List[str]:
    raw = os.getenv("HARRIS_E2E_STEPS", "")
    if not raw.strip():
        return list(ALL_STEPS)
    steps = [s.strip().lower() for s in raw.split(",") if s.strip()]
    valid = set(ALL_STEPS)
    return [s for s in steps if s in valid]


def _run(cmd: List[str], env: dict | None = None) -> int:
    _log("RUN → " + " ".join(cmd))
    proc = subprocess.Popen(cmd, env=env)
    proc.wait()
    code = proc.returncode
    if code == 0:
        _log("OK  ← " + " ".join(cmd))
    else:
        _log(f"ERR ← (exit {code}) " + " ".join(cmd))
    return code


def step_fetch() -> int:
    _log("STEP: fetch (email rosters)")
    return _run([sys.executable, "-m", "scripts.fetch_email_rosters"])


def step_roster() -> int:
    _log("STEP: roster (import + enrich)")
    return _run([sys.executable, "-m", "scripts.run_ingestion", "--source", "harris_email_roster"])


def step_ingest() -> int:
    _log("STEP: ingest (harris_inmate)")
    return _run([sys.executable, "-m", "scripts.run_ingestion", "--source", "harris_inmate"])


def step_normalize() -> int:
    _log("STEP: normalize (simple_harris)")
    # normalize_to_simple.py lives at repo root, so call the file directly
    args = [sys.executable, "normalize_to_simple.py", "--county", "harris"]
    # Optional tuning via env (with defaults)
    bs = os.getenv("HARRIS_BATCH_SIZE", "2000")
    bks = os.getenv("HARRIS_BULK_SIZE", "1000")
    pe = os.getenv("HARRIS_PROGRESS_EVERY", "1000")
    ll = os.getenv("HARRIS_LOG_LEVEL", "INFO")
    lf = os.getenv("HARRIS_LOG_FILE")
    # Ensure logs dir exists if writing file
    if not lf:
        from datetime import datetime
        logs = Path.cwd() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        lf = str(logs / f"normalize_harris_{stamp}.log")
    args += [
        "--batch-size", bs,
        "--bulk-size", bks,
        "--progress-every", pe,
        "--log-level", ll,
        "--log-file", lf,
    ]
    return _run(args)


def step_rebucket() -> int:
    _log("STEP: rebucket (safety; booking_date only)")
    return _run([sys.executable, "-m", "scripts.rebucket_simple_harris"])


def step_report() -> int:
    _log("STEP: report (simple deltas)")
    # Default to reporting only Harris unless caller overrides REPORT_COUNTIES
    env = dict(os.environ)
    env.setdefault("REPORT_COUNTIES", "harris")
    return _run([sys.executable, "-m", "scripts.report_simple_deltas"], env=env)


def main() -> int:
    # Load .env from repo root so subprocesses inherit MONGO_*, IMAP_*, etc.
    if load_dotenv is not None:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    steps = _parse_steps()
    _log(f"Harris E2E start | steps={steps}")
    continue_on_error = os.getenv("HARRIS_E2E_CONTINUE_ON_ERROR", "0").strip() not in ("0", "false", "False", "")
    rc = 0
    retry_enabled = int(os.getenv("HARRIS_E2E_RETRY", "1") or "1")
    retry_delay_s = int(os.getenv("HARRIS_E2E_RETRY_DELAY", "10") or "10")
    write_summary = os.getenv("HARRIS_E2E_WRITE_SUMMARY", "0").strip() not in ("0", "false", "False", "")
    summary: List[dict] = []

    def _run_step(name: str, fn) -> int:
        started = datetime.now(timezone.utc).isoformat()
        start_ts = time.time()
        code = fn()
        # Simple one-time retry on failure if enabled
        if code != 0 and retry_enabled:
            _log(f"Retrying step '{name}' once in {retry_delay_s}s...")
            time.sleep(retry_delay_s)
            code = fn()
        dur = round(time.time() - start_ts, 3)
        ended = datetime.now(timezone.utc).isoformat()
        summary.append({"step": name, "rc": code, "started_at": started, "ended_at": ended, "duration_s": dur})
        return code

    def _maybe_stop(step_name: str, code: int) -> bool:
        """Return True if we should stop the pipeline after this step failure."""
        if code == 0:
            return False
        # Provide friendly guidance if the ingest step fails with Harris HTML/error pages
        if step_name == "ingest":
            today = datetime.now().strftime("%m-%d-%y")
            _log(
                "HINT: Ingest failed. If the county site returned an HTML/error page, "
                "set HARRIS_PATH_OVERRIDES to today’s files and re-run. Example:"
            )
            example = (
                "HARRIS_PATH_OVERRIDES='{" +
                f"\"Civil/bond\":\"Civil/{today}-bond.txt\", "
                f"\"Civil/misfel\":\"Civil/{today}-misfel.txt\", "
                f"\"Civil/nafiling\":\"Civil/{today}-nafiling.txt\", "
                f"\"Criminal/bond\":\"Criminal/{today}-bond.txt\", "
                f"\"Criminal/misfel\":\"Criminal/{today}-misfel.txt\", "
                f"\"Criminal/nafiling\":\"Criminal/{today}-nafiling.txt\"" +
                "}' python -m scripts.run_harris_e2e"
            )
            _log(example)
        if continue_on_error:
            _log(f"CONTINUE_ON_ERROR is set; continuing after {step_name} failure (exit={code})")
            return False
        return True

    if "fetch" in steps:
        rc = _run_step("fetch", step_fetch)
        if _maybe_stop("fetch", rc):
            return rc
    if "roster" in steps:
        rc = _run_step("roster", step_roster)
        if _maybe_stop("roster", rc):
            return rc
    if "ingest" in steps:
        rc = _run_step("ingest", step_ingest)
        if _maybe_stop("ingest", rc):
            return rc
    if "normalize" in steps:
        rc = _run_step("normalize", step_normalize)
        if _maybe_stop("normalize", rc):
            return rc
    if "rebucket" in steps:
        rc = _run_step("rebucket", step_rebucket)
        if _maybe_stop("rebucket", rc):
            return rc
    if "report" in steps:
        rc = _run_step("report", step_report)
        if _maybe_stop("report", rc):
            return rc

    # Optional JSON summary output for monitoring/log collection
    if write_summary:
        data = {"run_started_at": summary[0]["started_at"] if summary else datetime.now(timezone.utc).isoformat(),
                "run_finished_at": datetime.now(timezone.utc).isoformat(),
                "steps": summary}
        path = os.getenv("HARRIS_E2E_SUMMARY")
        if not path:
            # default to logs/ with datestamp
            logs = Path.cwd() / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            path = str(logs / f"harris_e2e_summary_{stamp}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            _log(f"Summary written: {path}")
        except Exception as e:
            _log(f"WARN: failed to write summary: {e}")

    _log("Harris E2E finished | exit=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
