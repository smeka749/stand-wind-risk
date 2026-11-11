"""Store-level rules against Postgres (the only store): FOR UPDATE
SKIP LOCKED claims, ON CONFLICT creates, guarded lease expiry. Uses the
`store` fixture from conftest.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.store import DEFAULT_DURATIONS, MAX_LEASE_RETRIES


def test_create_is_idempotent(store):
    assert store.create("1 A St").id == store.create("  1 a st").id


def test_claim_once_and_reclaim_own(store):
    q = store.create("1 A St")
    first = store.claim_next("PreProcess", "job-1")
    assert (first.id, first.status) == (q.id, "Running")
    assert store.claim_next("PreProcess", "job-2") is None
    assert store.claim_next("PreProcess", "job-1").id == q.id   # Batch retry


def test_reclaimed_quote_cannot_be_completed_by_the_old_worker(store):
    q = store.create("1 A St")
    store.claim_next("PreProcess", "old", lease_seconds=-1)
    store.expire_stale_leases()
    store.claim_next("PreProcess", "new")
    assert store.complete(q.id, True, worker_id="old") is None
    assert store.complete(q.id, True, worker_id="new").status == "Success"


def test_expiry_retries_then_fails(store):
    q = store.create("1 A St")
    for i in range(MAX_LEASE_RETRIES):
        store.claim_next("PreProcess", "w", lease_seconds=-1)
        assert store.expire_stale_leases() == 1
        got = store.get(q.id)
        assert (got.status, got.retry_count) == ("Created", i + 1)
    store.claim_next("PreProcess", "w", lease_seconds=-1)
    store.expire_stale_leases()
    got = store.get(q.id)
    assert got.status == "Fail" and "too many times" in got.error


def test_expiry_does_not_undo_finished_stage(store):
    q = store.create("1 A St")
    store.claim_next("PreProcess", "w", lease_seconds=-1)
    store.complete(q.id, True, worker_id="w")
    store.expire_stale_leases()
    assert store.get(q.id).status == "Success"


def test_expiry_returns_simulation_to_its_entry_state(store):
    q = store.create("1 A St")
    store.claim_next("PreProcess", "w")
    store.complete(q.id, True, worker_id="w")
    store.claim_next("Simulation", "w", lease_seconds=-1)
    store.expire_stale_leases()
    got = store.get(q.id)
    assert (got.phase, got.status) == ("PreProcess", "Success")


def test_reopen_failed_quote(store):
    q = store.create("1 A St")
    store.claim_next("PreProcess", "w")
    store.complete(q.id, False, error="geocoding failed", worker_id="w")
    got = store.reopen(q.id)
    assert (got.phase, got.status, got.error) == ("Requested", "Created", None)


def test_events_and_typical_durations(store):
    q = store.create("1 A St")
    for d in (100, 300, 200):
        store.record_event(q.id, "Simulation", "Success", worker_id="j",
                           image_tag="t1", duration_seconds=d,
                           started_at="2026-11-07T00:00:00+00:00")
    store.record_event(q.id, "Simulation", "Fail", duration_seconds=5)
    store.record_event(q.id, "PostProcess", "Fail", error="render failed")
    events = store.events(q.id)
    assert [e["outcome"] for e in events] == ["Success"] * 3 + ["Fail"] * 2
    assert events[0]["image_tag"] == "t1" and isinstance(events[0]["ended_at"], str)
    typical = store.typical_durations()
    assert typical["Simulation"] == 200      # median of successes only
    assert typical["PostProcess"] == DEFAULT_DURATIONS["PostProcess"]


def test_claim_sets_phase_started_at(store):
    store.create("1 A St")
    assert store.claim_next("PreProcess", "j").phase_started_at is not None
