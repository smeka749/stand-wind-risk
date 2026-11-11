"""Quote store: phase/status state machine with atomic claims.

Phases: Requested -> PreProcess -> Simulation -> PostProcess -> Quoted.
A stage is claimed with one conditional UPDATE from the previous stage's
Success (PreProcess from Requested/Created), so a second claimant gets
nothing. A claim sets a lease; an expired lease returns the quote to its
entry state via expire_stale_leases().
"""
from __future__ import annotations

import re
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# (from_phase, from_status) -> (to_phase, to_status) for a claim.
STAGES = {
    "PreProcess": ("Requested", "Created"),
    "Simulation": ("PreProcess", "Success"),
    "PostProcess": ("Simulation", "Success"),
}


# Fail a quote whose worker keeps dying instead of retrying forever.
MAX_LEASE_RETRIES = 3

# Waiting state -> the stage that should run next.
WAITING = {entry: stage for stage, entry in STAGES.items()}


def next_stage(quote: "Quote") -> str | None:
    """The stage a quote is waiting for, or None (running/failed/done)."""
    return WAITING.get((quote.phase, quote.status))


STAGE_ORDER = ["PreProcess", "Simulation", "PostProcess"]
# ETA fallback until there are real runs to take a median from.
DEFAULT_DURATIONS = {"PreProcess": 60.0, "Simulation": 600.0, "PostProcess": 180.0}
ETA_SAMPLE = 20   # recent successful runs per stage


def _as_datetime(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def estimate(quote: "Quote", typical: dict[str, float],
             now: datetime | None = None) -> tuple[str | None, float | None]:
    """(stage the quote is waiting for, seconds until Quoted), or
    (None, None) once it is Quoted or Failed. Typical durations of the
    remaining stages, minus time already spent in a running one."""
    if quote.status == "Running":
        current = quote.phase
        started = _as_datetime(quote.phase_started_at)
        elapsed = ((now or _now()) - started).total_seconds() if started else 0.0
    else:
        current, elapsed = next_stage(quote), 0.0
    if current not in STAGE_ORDER:
        return None, None
    rest = STAGE_ORDER[STAGE_ORDER.index(current):]
    eta = max(typical[current] - elapsed, 0.0) + sum(typical[s] for s in rest[1:])
    return current, round(eta)


def _medians(rows) -> dict[str, float]:
    """rows: (stage, duration) newest first -> median of the last
    ETA_SAMPLE per stage, DEFAULT_DURATIONS where there are none."""
    samples: dict[str, list[float]] = {}
    for stage, duration in rows:
        bucket = samples.setdefault(stage, [])
        if duration is not None and len(bucket) < ETA_SAMPLE:
            bucket.append(float(duration))
    out = dict(DEFAULT_DURATIONS)
    for stage, values in samples.items():
        if values and stage in out:
            out[stage] = statistics.median(values)
    return out


def _event_dict(row) -> dict:
    row = dict(row)
    for key in ("started_at", "ended_at"):
        if isinstance(row.get(key), datetime):
            row[key] = row[key].isoformat()
    return {k: row.get(k) for k in ("stage", "outcome", "started_at", "ended_at",
                                     "duration_seconds", "image_tag",
                                     "worker_id", "error")}


def normalize_address(address: str) -> str:
    return re.sub(r"\s+", " ", address.strip().lower())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def database_url_from_env() -> str | None:
    """DATABASE_URL if set (local), else built from DB_* vars (ECS/Batch,
    password injected from Secrets Manager). None means refuse to start."""
    import os

    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return dsn
    host = os.environ.get("DB_HOST")
    if not host:
        return None
    user = os.environ.get("DB_USER", "windrisk")
    password = os.environ.get("DB_PASSWORD", "")
    dbname = os.environ.get("DB_NAME", "windrisk")
    port = os.environ.get("DB_PORT", "5432")
    return (f"postgresql://{user}:{password}@{host}:{port}/{dbname}")


@dataclass
class Quote:
    id: str
    address: str
    normalized_address: str
    phase: str = "Requested"
    status: str = "Created"
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    retry_count: int = 0
    error: str | None = None
    score: float | None = None
    case_s3_key: str | None = None
    field_s3_key: str | None = None
    video_s3_key: str | None = None
    # Wall-clock stage timing, measured in app/worker.py.
    stage_name: str | None = None
    stage_started_at: str | None = None
    stage_ended_at: str | None = None
    stage_duration_seconds: float | None = None
    # When the current stage was claimed (drives the ETA).
    phase_started_at: datetime | str | None = None


def _row_to_quote(row: dict) -> Quote:
    # Postgres returns uuid.UUID; runners build paths with Path / quote.id.
    data = {k: row[k] for k in Quote.__dataclass_fields__ if k in row}
    if data.get("id") is not None:
        data["id"] = str(data["id"])
    return Quote(**data)


class PostgresQuoteStore:
    """Postgres store. claim_next uses FOR UPDATE SKIP LOCKED so concurrent
    workers never claim the same row."""

    def __init__(self, dsn: str):
        import psycopg
        self._psycopg = psycopg
        self.dsn = dsn

    def _conn(self):
        return self._psycopg.connect(self.dsn, row_factory=self._psycopg.rows.dict_row)

    def create(self, address: str) -> Quote:
        norm = normalize_address(address)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM quotes WHERE normalized_address = %s", (norm,))
            row = cur.fetchone()
            if row:
                return _row_to_quote(row)
            q = Quote(id=str(uuid.uuid4()), address=address.strip(),
                      normalized_address=norm)
            # Two concurrent POSTs for one address: the loser's insert is a
            # no-op and it reads back the winner's row (no 500).
            cur.execute(
                "INSERT INTO quotes (id, address, normalized_address, phase, status)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (normalized_address) DO NOTHING RETURNING *",
                (q.id, q.address, q.normalized_address, q.phase, q.status),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT * FROM quotes WHERE normalized_address = %s",
                            (norm,))
                row = cur.fetchone()
            return _row_to_quote(row)

    def get(self, quote_id: str) -> Quote | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM quotes WHERE id = %s", (quote_id,))
            row = cur.fetchone()
            return _row_to_quote(row) if row else None

    def claim_next(self, stage: str, worker_id: str,
                   lease_seconds: int = 1800) -> Quote | None:
        from_phase, from_status = STAGES[stage]
        lease = _now() + timedelta(seconds=lease_seconds)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM quotes
                   WHERE (phase = %s AND status = %s
                          AND (lease_expires_at IS NULL
                               OR lease_expires_at < now()))
                      -- a retried Batch job resumes the row it owns
                      OR (phase = %s AND status = 'Running' AND worker_id = %s)
                   ORDER BY (worker_id IS NOT DISTINCT FROM %s) DESC, created_at
                   LIMIT 1
                   FOR UPDATE SKIP LOCKED""",
                (from_phase, from_status, stage, worker_id, worker_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                """UPDATE quotes SET phase = %s, status = 'Running',
                       worker_id = %s, lease_expires_at = %s,
                       phase_started_at = now()
                   WHERE id = %s RETURNING *""",
                (stage, worker_id, lease, row["id"]),
            )
            return _row_to_quote(cur.fetchone())

    def complete(self, quote_id: str, success: bool, error: str | None = None,
                 score: float | None = None, video_s3_key: str | None = None,
                 case_s3_key: str | None = None,
                 field_s3_key: str | None = None,
                 stage_name: str | None = None,
                 stage_started_at: str | None = None,
                 stage_ended_at: str | None = None,
                 stage_duration_seconds: float | None = None,
                 worker_id: str | None = None) -> Quote | None:
        # Read, validate, then a guarded UPDATE (re-checks status and
        # worker_id, so a concurrent change makes it a no-op).
        q = self.get(quote_id)
        if not q or q.status != "Running":
            return None
        # Fencing: only the current lease holder may finish the stage, so
        # a worker whose quote was reclaimed cannot overwrite the new run.
        if worker_id is not None and q.worker_id != worker_id:
            return None
        phase = ("Quoted" if (success and q.phase == "PostProcess")
                 else q.phase)
        status = "Success" if success else "Fail"
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE quotes SET phase = %s, status = %s, error = %s,
                       score = COALESCE(%s, score),
                       video_s3_key = COALESCE(%s, video_s3_key),
                       case_s3_key = COALESCE(%s, case_s3_key),
                       field_s3_key = COALESCE(%s, field_s3_key),
                       stage_name = COALESCE(%s, stage_name),
                       stage_started_at = COALESCE(%s, stage_started_at),
                       stage_ended_at = COALESCE(%s, stage_ended_at),
                       stage_duration_seconds = COALESCE(%s, stage_duration_seconds),
                       worker_id = NULL, lease_expires_at = NULL,
                       updated_at = now()
                   WHERE id = %s AND status = 'Running'
                     AND (%s::text IS NULL OR worker_id = %s)""",
                (phase, status, error, score, video_s3_key, case_s3_key,
                 field_s3_key, stage_name, stage_started_at, stage_ended_at,
                 stage_duration_seconds, quote_id, worker_id, worker_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get(quote_id)

    def expire_stale_leases(self) -> int:
        """Return expired Running rows to their entry state, or fail them
        after MAX_LEASE_RETRIES. Guarded UPDATEs only, so a worker that just
        finished is never undone. Returns rows touched."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE quotes SET status = 'Fail',
                       error = 'worker lease expired too many times',
                       worker_id = NULL, lease_expires_at = NULL,
                       updated_at = now()
                   WHERE status = 'Running' AND lease_expires_at < now()
                     AND retry_count >= %s""", (MAX_LEASE_RETRIES,))
            failed = cur.rowcount
            cur.execute(
                """UPDATE quotes SET
                       phase = CASE phase WHEN 'PreProcess' THEN 'Requested'
                                          WHEN 'Simulation' THEN 'PreProcess'
                                          ELSE 'Simulation' END,
                       status = CASE phase WHEN 'PreProcess' THEN 'Created'
                                           ELSE 'Success' END,
                       worker_id = NULL, lease_expires_at = NULL,
                       retry_count = retry_count + 1, updated_at = now()
                   WHERE status = 'Running' AND lease_expires_at < now()
                     AND retry_count < %s""", (MAX_LEASE_RETRIES,))
            return failed + cur.rowcount

    def reopen(self, quote_id: str) -> Quote | None:
        """Return a failed quote to its stage's entry state for a retry."""
        q = self.get(quote_id)
        if not q or q.status != "Fail" or q.phase not in STAGES:
            return q
        from_phase, from_status = STAGES[q.phase]
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE quotes SET phase = %s, status = %s, error = NULL,
                       worker_id = NULL, lease_expires_at = NULL,
                       retry_count = 0, updated_at = now()
                   WHERE id = %s AND status = 'Fail'""",
                (from_phase, from_status, quote_id))
        return self.get(quote_id)


    def record_event(self, quote_id: str, stage: str, outcome: str,
                     worker_id: str | None = None, image_tag: str | None = None,
                     started_at: str | None = None, ended_at: str | None = None,
                     duration_seconds: float | None = None,
                     error: str | None = None) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO quote_events (quote_id, stage, outcome, worker_id,"
                " image_tag, started_at, ended_at, duration_seconds, error)"
                " VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now()),"
                " %s, %s)",
                (quote_id, stage, outcome, worker_id, image_tag, started_at,
                 ended_at, duration_seconds, error))

    def events(self, quote_id: str) -> list[dict]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM quote_events WHERE quote_id = %s"
                        " ORDER BY id", (quote_id,))
            return [_event_dict(r) for r in cur.fetchall()]

    def typical_durations(self) -> dict[str, float]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT stage, duration_seconds FROM ("
                "  SELECT stage, duration_seconds, row_number() OVER"
                "    (PARTITION BY stage ORDER BY ended_at DESC) AS n"
                "  FROM quote_events WHERE outcome = 'Success') t"
                " WHERE n <= %s", (ETA_SAMPLE,))
            return _medians((r["stage"], r["duration_seconds"])
                            for r in cur.fetchall())

    def ensure_schema(self, schema_sql: str) -> None:
        """Apply db/schema.sql (idempotent). RDS is private, so the API
        applies it at startup."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(schema_sql)
