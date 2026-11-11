"""JSON logs (one object per line on stdout, keyed by quote_id) and stage timing.

StageTimer measures a stage from claim to runner return. That is the stage
total; OpenFOAM's per-iteration ExecutionTime/ClockTime is not.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def setup_logging(level: str | None = None) -> logging.Logger:
    """Pass-through stdout handler at LOG_LEVEL (default INFO); idempotent."""
    import os

    lvl = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, lvl, logging.INFO))
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, **fields) -> None:
    """Emit one JSON line: {"event", "timestamp", ...fields}. No secrets."""
    record = {"event": event, "timestamp": _now_iso()}
    record.update(fields)
    logger.info(json.dumps(record, default=str))


@dataclass
class StageTimer:
    """Monotonic wall-clock timer for one stage, claim to return or raise."""

    stage: str
    quote_id: str
    started_at: str = field(default_factory=_now_iso)
    _start_mono: float = field(default_factory=time.monotonic, repr=False)
    ended_at: str | None = None
    duration_seconds: float | None = None

    def finish(self) -> dict:
        """Stop the timer and return the timing dict for the store."""
        self.ended_at = _now_iso()
        self.duration_seconds = time.monotonic() - self._start_mono
        return {
            "stage_name": self.stage,
            "stage_started_at": self.started_at,
            "stage_ended_at": self.ended_at,
            "stage_duration_seconds": self.duration_seconds,
        }
