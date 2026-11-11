"""Shared fixtures. Postgres is the only store, so tests that need one
run against a real database:

    docker compose up -d db
    TEST_DATABASE_URL=postgresql://windrisk:windrisk@localhost:5432/windrisk \
        python -m pytest tests/ -q

CI provides the same database as a service container.
"""
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from app.store import PostgresQuoteStore  # noqa: E402


@pytest.fixture()
def store():
    """A Postgres store with empty tables. Fails (not skips) without a
    database, so a green run always means the store was exercised."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.fail("TEST_DATABASE_URL not set: run `docker compose up -d db` "
                    "and set TEST_DATABASE_URL (see tests/conftest.py)",
                    pytrace=False)
    s = PostgresQuoteStore(dsn)
    s.ensure_schema((SERVICE / "db" / "schema.sql").read_text())
    with s._conn() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE quotes CASCADE")
    return s
