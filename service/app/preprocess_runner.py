"""PreProcess runner: runs the SME's preprocess.py for one quote.

Runs the script as a subprocess (cwd = the SME dir, for its sibling imports),
so a script crash fails the quote, not the worker. Script exits: 0 case
written; 1 PDAL failed; 2 no 3DEP LiDAR coverage; 3 under 500 building points;
4 geocoding failed. Exit 0 is double-checked for buildings.stl. There is no
cache: a retried quote re-queries the geocoder and the LiDAR source.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .s3util import case_prefix, resolve_bucket, upload_dir
from .store import Quote

DEFAULT_SME_DIR = Path(__file__).resolve().parents[2] / "wind-risk-take-home"
DEFAULT_WORK_ROOT = Path("work")

# Under the 30-minute claim lease, so a running job is never reclaimed.
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("PREPROCESS_TIMEOUT_SECONDS", "1500"))

EXIT_MESSAGES = {
    1: "PDAL pipeline failed",
    2: "no USGS 3DEP LiDAR coverage",
    3: "fewer than 500 building points",
    4: "geocoding failed",
}

def resolve_sme_dir(sme_dir: str | Path | None = None) -> Path:
    if sme_dir is not None:
        return Path(sme_dir)
    env = os.environ.get("SME_DIR")
    if env:
        return Path(env)
    return DEFAULT_SME_DIR

def run_preprocess(
    quote: Quote,
    *,
    sme_dir: str | Path | None = None,
    work_root: str | Path | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    python: str | None = None,
    s3_bucket: str | None = None,
    s3_client=None,
) -> dict:
    """Build the quote's case; return {"case_s3_key": ...}. Raises on failure."""
    sme = resolve_sme_dir(sme_dir)
    script = sme / "preprocess.py"
    if not script.is_file():
        raise RuntimeError(f"preprocess script not found: {script} "
                           f"(set SME_DIR to the SME project directory)")

    root = Path(work_root) if work_root is not None else Path(
        os.environ.get("PREPROCESS_WORK_ROOT", DEFAULT_WORK_ROOT))
    # Absolute, because the script runs with the SME dir as cwd.
    case_dir = (root / quote.id / "case").resolve()
    case_dir.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        python or sys.executable,
        str(script),
        "--address", quote.address,
        "--out", str(case_dir),
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(sme), capture_output=True, text=True,
            timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"preprocess timed out after {timeout_seconds}s for "
            f"{quote.address!r}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        label = EXIT_MESSAGES.get(proc.returncode, "preprocess failed")
        raise RuntimeError(
            f"{label} (exit {proc.returncode}) for {quote.address!r}"
            + (f": {detail[-2000:]}" if detail else ""))

    buildings = case_dir / "constant" / "triSurface" / "buildings.stl"
    if not buildings.is_file():
        raise RuntimeError(
            f"preprocess exited 0 but no case output at {buildings} "
            f"for {quote.address!r}")

    # With a bucket, store the case in S3 and return its prefix.
    bucket = resolve_bucket(s3_bucket)
    if bucket:
        from .s3util import get_s3_client

        client = s3_client or get_s3_client()
        return {"case_s3_key": upload_dir(client, bucket, case_dir,
                                          case_prefix(quote.id))}
    return {"case_s3_key": str(case_dir)}
