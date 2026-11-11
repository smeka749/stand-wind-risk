"""Stage worker: expire stale leases, claim a quote, run the stage, complete it.

Workers are stateless and stage-specific, so any worker can take any eligible
quote. poll_once times each stage (claim to return), logs one JSON line per
outcome keyed by quote_id, and stores the duration used for ETA and cost.

Run: python -m app.worker --stage PreProcess [--once]. Needs DATABASE_URL (or
DB_*); S3_BUCKET switches artifacts to S3.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Callable

from .logging_utils import StageTimer, get_logger, log_event, setup_logging
from .store import Quote

_logger = get_logger("wind_risk.worker")


def stub_runner(_quote: Quote) -> dict:
    """No-op runner for tests (no CFD)."""
    return {}


def _record(store, result, quote, stage, outcome, worker_id, timing, error=None):
    """Best-effort history row; skipped when complete() was fenced off."""
    if result is None or not hasattr(store, "record_event"):
        return
    try:
        store.record_event(
            quote.id, stage, outcome, worker_id=worker_id,
            image_tag=os.environ.get("IMAGE_TAG"),
            started_at=timing.get("stage_started_at"),
            ended_at=timing.get("stage_ended_at"),
            duration_seconds=timing.get("stage_duration_seconds"), error=error)
    except Exception as exc:  # noqa: BLE001
        log_event(_logger, "event_record_failed", quote_id=quote.id,
                  stage=stage, error=str(exc)[-500:])


def poll_once(store, stage: str, runner: Callable[[Quote], dict],
              worker_id: str | None = None,
              on_success: Callable[[Quote], None] | None = None) -> Quote | None:
    worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    store.expire_stale_leases()
    # Lease must outlast the stage timeout (set *_TIMEOUT_SECONDS lower).
    lease = int(os.environ.get("LEASE_SECONDS", "1800"))
    quote = store.claim_next(stage, worker_id, lease_seconds=lease)
    if not quote:
        return None
    timer = StageTimer(stage=stage, quote_id=quote.id)
    log_event(_logger, "stage_started", quote_id=quote.id, stage=stage,
              worker_id=worker_id, address=quote.address)
    try:
        artifacts = runner(quote) or {}
        timing = timer.finish()
        result = store.complete(quote.id, True, worker_id=worker_id,
                                **artifacts, **timing)
        _record(store, result, quote, stage, "Success", worker_id, timing)
        log_event(_logger, "stage_succeeded", quote_id=quote.id, stage=stage,
                  worker_id=worker_id, **timing)
        if on_success and result is not None:
            try:
                on_success(result)
            except Exception as hook_exc:  # noqa: BLE001
                log_event(_logger, "on_success_failed", quote_id=quote.id,
                          stage=stage, error=str(hook_exc)[-500:])
        return result
    except Exception as exc:  # noqa: BLE001 -- worker must surface, not crash
        timing = timer.finish()
        result = store.complete(quote.id, False, error=str(exc),
                                worker_id=worker_id, **timing)
        _record(store, result, quote, stage, "Fail", worker_id, timing,
                error=str(exc)[-2000:])
        log_event(_logger, "stage_failed", quote_id=quote.id, stage=stage,
                  worker_id=worker_id, error=str(exc)[-2000:], **timing)
        return result


def run_loop(store, stage: str, runner: Callable[[Quote], dict],
             poll_interval_seconds: float = 5.0, once: bool = False,
             worker_id: str | None = None,
             on_success: Callable[[Quote], None] | None = None):
    worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    while True:
        result = poll_once(store, stage, runner, worker_id=worker_id,
                           on_success=on_success)
        if once and result is None:
            return
        if result is None:
            time.sleep(poll_interval_seconds)
        if once:
            return


if __name__ == "__main__":
    import argparse
    import os

    from .dispatcher import NEXT_STAGE, submit_stage_job
    from .postprocess_runner import run_postprocess
    from .preprocess_runner import run_preprocess
    from .s3util import get_s3_client, resolve_bucket
    from .simulation_runner import run_simulation
    from .store import PostgresQuoteStore, database_url_from_env

    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True,
                        choices=["PreProcess", "Simulation", "PostProcess"])
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--sme-dir", default=None)
    parser.add_argument("--work-root", default=None)
    args = parser.parse_args()
    dsn = database_url_from_env()
    if not dsn:
        parser.error("Postgres required: set DATABASE_URL or DB_HOST/DB_PASSWORD "
                     "(locally: docker compose up -d db)")
    _store = PostgresQuoteStore(dsn)
    bucket = resolve_bucket()
    s3_client = get_s3_client() if bucket else None
    if args.stage == "PreProcess":
        _runner = lambda q: run_preprocess(  # noqa: E731
            q, sme_dir=args.sme_dir, work_root=args.work_root,
            s3_bucket=bucket, s3_client=s3_client)
    elif args.stage == "Simulation":
        _runner = lambda q: run_simulation(  # noqa: E731
            q, sme_dir=args.sme_dir, work_root=args.work_root,
            s3_bucket=bucket, s3_client=s3_client)
    else:
        _runner = lambda q: run_postprocess(  # noqa: E731
            q, sme_dir=args.sme_dir, work_root=args.work_root,
            s3_bucket=bucket, s3_client=s3_client)
    # The Batch job ID survives job retries, so a retried job can reclaim
    # its own quote instead of finding it locked.
    _worker_id = os.environ.get("AWS_BATCH_JOB_ID") or None
    _next = NEXT_STAGE.get(args.stage)
    _on_success = (lambda q: submit_stage_job(_next, quote_id=q.id)) if _next else None
    run_loop(_store, args.stage, _runner,
             poll_interval_seconds=args.interval, once=args.once,
             worker_id=_worker_id, on_success=_on_success)
    print(f"worker done: stage={args.stage} store={type(_store).__name__}")
