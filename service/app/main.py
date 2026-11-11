"""Quote Service API.

POST /quotes {address} -> 202 + quote_id (idempotent on normalized address)
GET  /quotes/{id}     -> phase, status, score, video_url

The row stores video_s3_key; a short-lived presigned URL is minted on read
and never stored.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .logging_utils import get_logger, log_event, setup_logging
from .s3util import make_presigner
from .store import (DEFAULT_DURATIONS, PostgresQuoteStore, Quote,
                    estimate, database_url_from_env, next_stage)

_logger = get_logger("wind_risk.api")


class CreateQuoteRequest(BaseModel):
    address: str


def _quote_response(q: Quote, presigner: Callable[[str], str] | None,
                    store=None, history: bool = False):
    video_url = None
    if q.video_s3_key and presigner:
        video_url = presigner(q.video_s3_key)
    typical = (store.typical_durations() if hasattr(store, "typical_durations")
               else DEFAULT_DURATIONS)
    waiting_for, eta = estimate(q, typical)
    body = {
        "quote_id": q.id,
        "address": q.address,
        "phase": q.phase,
        "status": q.status,
        "score": q.score,
        "video_s3_key": q.video_s3_key,
        "video_url": video_url,
        "error": q.error,
        "retry_count": q.retry_count,
        "waiting_for": waiting_for,     # stage queued or running, else None
        "eta_seconds": eta,             # rough, from recent median durations
    }
    if history and hasattr(store, "events"):
        body["history"] = store.events(q.id)   # one entry per stage attempt
    return body


def create_app(store=None, presigner: Callable[[str], str] | None = None,
               submitter: Callable[..., str | None] | None = None):
    setup_logging()
    # Locally (no bucket) video_url stays None; video_s3_key is a path.
    if presigner is None and os.environ.get("S3_BUCKET"):
        try:
            presigner = make_presigner(os.environ["S3_BUCKET"])
        except Exception as exc:  # noqa: BLE001 -- boto3 missing/misconfigured
            _logger.warning("s3 presigner unavailable: %s", exc)
    if store is None:
        dsn = database_url_from_env()
        if not dsn:
            raise RuntimeError(
                "Postgres required: set DATABASE_URL or DB_HOST/DB_PASSWORD "
                "(locally: docker compose up -d db)")
        store = PostgresQuoteStore(dsn)
        schema = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
        if schema.is_file():
            store.ensure_schema(schema.read_text())
    if submitter is None:
        from .dispatcher import submit_stage_job as submitter
    app = FastAPI(title="Wind Risk Quote Service")

    @app.post("/quotes", status_code=202)
    def create_quote(req: CreateQuoteRequest):
        if not req.address or not req.address.strip():
            raise HTTPException(status_code=422, detail="address is required")
        q = store.create(req.address)
        log_event(_logger, "quote_created", quote_id=q.id,
                  phase=q.phase, status=q.status)
        # A repeat POST retries a failed quote, or re-dispatches a waiting one
        # (covers a lost submit; a duplicate job finds nothing to claim).
        if q.status == "Fail":
            q = store.reopen(q.id)
            log_event(_logger, "quote_reopened", quote_id=q.id,
                      phase=q.phase, status=q.status)
        stage = next_stage(q)
        if stage:
            submitter(stage, quote_id=q.id)
        return _quote_response(q, presigner, store)

    @app.get("/quotes/{quote_id}")
    def get_quote(quote_id: str):
        q = store.get(quote_id)
        if not q:
            raise HTTPException(status_code=404, detail="quote not found")
        log_event(_logger, "quote_fetched", quote_id=q.id,
                  phase=q.phase, status=q.status)
        return _quote_response(q, presigner, store, history=True)

    @app.get("/health")
    def health():
        return {"ok": True}

    app.state.store = store
    return app
