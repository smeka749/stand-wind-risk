"""Start a stage's AWS Batch job when there is work.

Event-driven: the API starts PreProcess on a new quote; each worker starts the
next stage on success. Jobs carry no quote: a --once worker claims the next
eligible one from Postgres, so a duplicate job finds nothing and exits.

Config: BATCH_QUEUE_<STAGE> and BATCH_JOBDEF_<STAGE>. All unset means local mode
(nothing submitted); a partly configured set logs job_submit_skipped.
"""
from __future__ import annotations

import os

from .logging_utils import get_logger, log_event

_logger = get_logger("wind_risk.dispatcher")

NEXT_STAGE = {"PreProcess": "Simulation", "Simulation": "PostProcess"}

_batch_client = None
_STAGE_KEYS = ("PREPROCESS", "SIMULATION", "POSTPROCESS")


def stage_target(stage: str) -> tuple[str, str] | None:
    key = stage.upper()
    queue = os.environ.get(f"BATCH_QUEUE_{key}")
    jobdef = os.environ.get(f"BATCH_JOBDEF_{key}")
    return (queue, jobdef) if queue and jobdef else None


def _client():
    global _batch_client
    if _batch_client is None:
        import boto3
        _batch_client = boto3.client("batch")
    return _batch_client


def submit_stage_job(stage: str, quote_id: str | None = None,
                     batch=None) -> str | None:
    """Return the Batch job ID, or None in local mode or on a logged failure
    (the quote keeps its state and can be re-dispatched)."""
    target = stage_target(stage)
    if target is None:
        if any(os.environ.get(f"BATCH_QUEUE_{k}") for k in _STAGE_KEYS):
            log_event(_logger, "job_submit_skipped", quote_id=quote_id,
                      stage=stage,
                      error=f"BATCH_QUEUE_{stage.upper()} / "
                            f"BATCH_JOBDEF_{stage.upper()} not set")
        return None
    queue, jobdef = target
    name = f"{stage.lower()}-{(quote_id or 'any')[:8]}"
    try:
        resp = (batch or _client()).submit_job(
            jobName=name, jobQueue=queue, jobDefinition=jobdef)
    except Exception as exc:  # noqa: BLE001 -- never fail the caller
        log_event(_logger, "job_submit_failed", quote_id=quote_id,
                  stage=stage, error=str(exc)[-500:])
        return None
    job_id = resp.get("jobId")
    log_event(_logger, "job_submitted", quote_id=quote_id, stage=stage,
              job_id=job_id, job_queue=queue)
    return job_id
