"""Simulation runner: runs the SME's run_simulation.sh on one quote's case.

Shells out rather than reimplementing OpenFOAM, so a solver failure fails the
quote, not the worker. Script exits: 0 solved; 1 did not converge; 2 missing
case or degenerate mesh; 3 an OpenFOAM step failed. Exit 0 is double-checked
(case.foam, "solution converged", a timestep with a p field). A Fail is final;
only a dead worker's expired lease is retried.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .preprocess_runner import DEFAULT_WORK_ROOT, resolve_sme_dir
from .s3util import (download_dir, is_s3_key, parse_s3_key, resolve_bucket,
                     s3_uri, upload_dir)
from .store import Quote

# Under the 30-minute claim lease, so a running job is never reclaimed.
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("SIMULATION_TIMEOUT_SECONDS", "1500"))

EXIT_MESSAGES = {
    1: "solution did not converge",
    2: "case dir not found or degenerate mesh (checkMesh)",
    3: "an OpenFOAM step failed",
}


def _final_timestep_dir(case_dir: Path) -> Path | None:
    times = []
    for entry in case_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            t = int(entry.name)
        except ValueError:
            continue
        if t == 0:
            continue
        times.append((t, entry))
    if not times:
        return None
    times.sort()
    return times[-1][1]


def _save_logs(s3_client, bucket: str, case_dir: Path, quote_id: str) -> str | None:
    """Upload the case's top-level log.* files to quotes/<id>/logs/.
    Returns the s3:// prefix, or None if there were none or it failed."""
    try:
        from .s3util import get_s3_client

        client = s3_client or get_s3_client()
        prefix = f"quotes/{quote_id}/logs"
        logs = sorted(p for p in case_dir.glob("log.*") if p.is_file())
        for path in logs:
            client.put_object(Bucket=bucket, Key=f"{prefix}/{path.name}",
                              Body=path.read_bytes())
        return s3_uri(bucket, prefix) if logs else None
    except Exception:  # noqa: BLE001 -- best effort only
        return None


def run_simulation(
    quote: Quote,
    *,
    sme_dir: str | Path | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    work_root: str | Path | None = None,
    s3_bucket: str | None = None,
    s3_client=None,
) -> dict:
    """Run the SME script on the quote's case; return {"field_s3_key": ...}.

    Raises RuntimeError on any failure. In S3 mode the log.* files are first
    copied to quotes/<id>/logs/ and the error ends with "(logs: s3://...)".
    """
    if not quote.case_s3_key:
        raise RuntimeError(
            f"no case directory for quote {quote.id} "
            f"({quote.address!r}) -- PreProcess must succeed first")
    # The SME script needs a local case: download it from S3 first.
    bucket = resolve_bucket(s3_bucket)
    s3_prefix: str | None = None
    if is_s3_key(quote.case_s3_key):
        bucket, s3_prefix = parse_s3_key(quote.case_s3_key)
        from .s3util import get_s3_client

        client = s3_client or get_s3_client()
        root = Path(work_root) if work_root is not None else Path(
            os.environ.get("PREPROCESS_WORK_ROOT", DEFAULT_WORK_ROOT))
        case_dir = (root / quote.id / "case").resolve()
        download_dir(client, bucket, s3_prefix, case_dir)
    else:
        case_dir = Path(quote.case_s3_key)
    if not case_dir.is_dir():
        raise RuntimeError(f"case dir not found: {case_dir}")

    sme = resolve_sme_dir(sme_dir)
    script = sme / "run_simulation.sh"
    if not script.is_file():
        raise RuntimeError(f"simulation script not found: {script} "
                           f"(set SME_DIR to the SME project directory)")

    try:
        cmd = ["bash", str(script), str(case_dir)]
        try:
            proc = subprocess.run(
                cmd, cwd=str(case_dir), capture_output=True, text=True,
                timeout=timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"simulation timed out after {timeout_seconds}s for "
                f"{quote.address!r}") from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            label = EXIT_MESSAGES.get(proc.returncode, "simulation failed")
            raise RuntimeError(
                f"{label} (exit {proc.returncode}) for {quote.address!r}"
                + (f": {detail[-2000:]}" if detail else ""))

        # Exit 0 is not enough: verify the solved field is actually there.
        if not (case_dir / "case.foam").is_file():
            raise RuntimeError(
                f"simulation exited 0 but no case.foam in {case_dir} "
                f"for {quote.address!r}")
        log = case_dir / "log.simpleFoam"
        if not log.is_file() or "solution converged" not in log.read_text(errors="replace"):
            raise RuntimeError(
                f"simulation exited 0 but log.simpleFoam shows no "
                f"'solution converged' for {quote.address!r}")
        final = _final_timestep_dir(case_dir)
        if final is None or not (final / "p").is_file():
            raise RuntimeError(
                f"simulation exited 0 but no solved timestep with a 'p' "
                f"field in {case_dir} for {quote.address!r}")
    except RuntimeError as exc:
        # The container's disk goes with the job: keep the logs (best effort).
        logs = (_save_logs(s3_client, bucket, case_dir, quote.id)
                if s3_prefix is not None and bucket else None)
        if logs:
            raise RuntimeError(f"{exc} (logs: {logs})") from exc
        raise

    if s3_prefix is not None and bucket:
        from .s3util import get_s3_client

        client = s3_client or get_s3_client()
        # processor*/ are per-rank leftovers no later stage reads (~90% of objects).
        return {"field_s3_key": upload_dir(client, bucket, case_dir, s3_prefix,
                                           exclude=("processor*",))}
    return {"field_s3_key": str(case_dir)}
