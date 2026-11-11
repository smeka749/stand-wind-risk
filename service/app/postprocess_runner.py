"""PostProcess runner: runs the SME's score.py, then render.py, for one quote.

Score runs first so a failed score doesn't waste a render. Exit 0 is
double-checked: score.json must hold a numeric score in [0, 100] and the video
must be non-empty. Paths passed to the scripts are absolute, because the
scripts run with the SME directory as their working directory. The video key is
stored; the presigned URL is minted on read, never stored.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .preprocess_runner import DEFAULT_WORK_ROOT, resolve_sme_dir
from .s3util import (
    download_dir,
    is_s3_key,
    parse_s3_key,
    resolve_bucket,
    s3_uri,
    score_key,
    upload_file,
    video_key,
)
from .logging_utils import get_logger, log_event
from .store import Quote

_logger = get_logger("wind_risk.postprocess")


def _step(quote: Quote, step: str, start: float) -> None:
    """Log one PostProcess sub-step's wall time (download/score/render/upload)."""
    log_event(_logger, "postprocess_step", quote_id=quote.id, step=step,
              seconds=round(time.monotonic() - start, 3))

# Score is a field read, not a solve; render is the long pole.
DEFAULT_SCORE_TIMEOUT_SECONDS = int(os.environ.get("SCORE_TIMEOUT_SECONDS", "300"))
DEFAULT_RENDER_TIMEOUT_SECONDS = int(os.environ.get("RENDER_TIMEOUT_SECONDS", "1500"))

SCORE_EXIT_MESSAGES = {
    1: "scoring failed (no timestep, empty p field, or wind speed zero)",
    2: "target 'house' patch not found in mesh",
}


def _run_script(
    cmd: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    label: str,
    quote: Quote,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{label} timed out after {timeout_seconds}s for "
            f"{quote.address!r}") from exc


def _check_exit(proc: subprocess.CompletedProcess, *, label: str,
                messages: dict[int, str], quote: Quote) -> None:
    if proc.returncode == 0:
        return
    detail = (proc.stderr or proc.stdout or "").strip()
    message = messages.get(proc.returncode, f"{label} failed")
    raise RuntimeError(
        f"{message} (exit {proc.returncode}) for {quote.address!r}"
        + (f": {detail[-2000:]}" if detail else ""))


def run_postprocess(
    quote: Quote,
    *,
    sme_dir: str | Path | None = None,
    work_root: str | Path | None = None,
    score_timeout_seconds: float = DEFAULT_SCORE_TIMEOUT_SECONDS,
    render_timeout_seconds: float = DEFAULT_RENDER_TIMEOUT_SECONDS,
    python: str | None = None,
    s3_bucket: str | None = None,
    s3_client=None,
) -> dict:
    """Score and render one quote; return {"score", "video_s3_key"}.

    Raises RuntimeError on any failure, so the quote is marked Fail.
    """
    case_key = quote.field_s3_key or quote.case_s3_key
    if not case_key:
        raise RuntimeError(
            f"no case directory for quote {quote.id} "
            f"({quote.address!r}) -- Simulation must succeed first")
    # The SME scripts need a local case: download it from S3 first.
    bucket = resolve_bucket(s3_bucket)
    if is_s3_key(case_key):
        bucket, s3_prefix = parse_s3_key(case_key)
        from .s3util import get_s3_client

        client = s3_client or get_s3_client()
        root = Path(work_root) if work_root is not None else Path(
            os.environ.get("PREPROCESS_WORK_ROOT", DEFAULT_WORK_ROOT))
        case_dir = (root / quote.id / "case").resolve()
        t0 = time.monotonic()
        download_dir(client, bucket, s3_prefix, case_dir)
        _step(quote, "download", t0)
    else:
        case_dir = Path(case_key)
    if not case_dir.is_dir():
        raise RuntimeError(f"case dir not found: {case_dir}")

    sme = resolve_sme_dir(sme_dir)
    score_script = sme / "postprocess" / "score.py"
    render_script = sme / "postprocess" / "render.py"
    for script in (score_script, render_script):
        if not script.is_file():
            raise RuntimeError(
                f"postprocess script not found: {script} "
                f"(set SME_DIR to the SME project directory)")

    # Absolute paths: the scripts run with the SME dir as cwd.
    root = Path(work_root) if work_root is not None else Path(
        os.environ.get("PREPROCESS_WORK_ROOT", DEFAULT_WORK_ROOT))
    out_dir = (root / quote.id).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    score_path = out_dir / "score.json"
    video_path = out_dir / "video.mp4"

    # 1. Score first -- a failed score must not waste a render.
    t0 = time.monotonic()
    proc = _run_script(
        [python or sys.executable, str(score_script),
         "--case", str(case_dir), "--out", str(score_path)],
        cwd=sme, timeout_seconds=score_timeout_seconds,
        label="score", quote=quote,
    )
    _check_exit(proc, label="score", messages=SCORE_EXIT_MESSAGES,
                quote=quote)
    if not score_path.is_file():
        raise RuntimeError(
            f"score exited 0 but no score output at {score_path} "
            f"for {quote.address!r}")
    try:
        score_data = json.loads(score_path.read_text())
    except (ValueError, OSError) as exc:
        raise RuntimeError(
            f"score output at {score_path} is not valid JSON for "
            f"{quote.address!r}: {exc}") from exc
    score = score_data.get("score") if isinstance(score_data, dict) else None
    if not isinstance(score, (int, float)) or isinstance(score, bool) \
            or not 0 <= float(score) <= 100:
        raise RuntimeError(
            f"score output at {score_path} has no numeric score in "
            f"[0, 100] for {quote.address!r}: {score_data!r}")
    _step(quote, "score", t0)

    # 2. Render the flyover video.
    t0 = time.monotonic()
    proc = _run_script(
        [python or sys.executable, str(render_script),
         "--case", str(case_dir), "--out", str(video_path)],
        cwd=sme, timeout_seconds=render_timeout_seconds,
        label="render", quote=quote,
    )
    _check_exit(proc, label="render", messages={}, quote=quote)
    if not video_path.is_file() or video_path.stat().st_size == 0:
        raise RuntimeError(
            f"render exited 0 but no video output at {video_path} "
            f"for {quote.address!r}")
    _step(quote, "render", t0)

    if bucket:
        from .s3util import get_s3_client

        client = s3_client or get_s3_client()
        t0 = time.monotonic()
        upload_file(client, bucket, score_path, score_key(quote.id))
        upload_file(client, bucket, video_path, video_key(quote.id))
        _step(quote, "upload", t0)
        return {"score": float(score),
                "video_s3_key": s3_uri(bucket, video_key(quote.id))}
    return {"score": float(score), "video_s3_key": str(video_path)}
