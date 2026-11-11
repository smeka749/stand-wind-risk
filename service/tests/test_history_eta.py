"""Per-stage history (quote_events) and the ETA in GET /quotes/{id}."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import create_app
from app.store import DEFAULT_DURATIONS, Quote, estimate
from app.worker import poll_once, stub_runner

D = DEFAULT_DURATIONS


def _q(phase, status, started=None):
    return Quote(id="q", address="a", normalized_address="a",
                 phase=phase, status=status, phase_started_at=started)


def test_estimate_queued_running_and_terminal():
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    assert estimate(_q("Requested", "Created"), D, now) == (
        "PreProcess", round(sum(D.values())))
    running = _q("Simulation", "Running", (now - timedelta(seconds=100)).isoformat())
    assert estimate(running, D, now) == (
        "Simulation", round(D["Simulation"] - 100 + D["PostProcess"]))
    overdue = _q("PostProcess", "Running", now - timedelta(hours=1))
    assert estimate(overdue, D, now) == ("PostProcess", 0)
    assert estimate(_q("Quoted", "Success"), D, now) == (None, None)
    assert estimate(_q("Simulation", "Fail"), D, now) == (None, None)


def test_worker_records_attempts_with_image_tag(store, monkeypatch):
    monkeypatch.setenv("IMAGE_TAG", "abc123")
    q = store.create("1 A St")
    poll_once(store, "PreProcess", stub_runner, worker_id="w1")

    def boom(_):
        raise RuntimeError("solver diverged")
    poll_once(store, "Simulation", boom, worker_id="w2")

    events = store.events(q.id)
    assert [(e["stage"], e["outcome"]) for e in events] == [
        ("PreProcess", "Success"), ("Simulation", "Fail")]
    assert {e["image_tag"] for e in events} == {"abc123"}
    assert "solver diverged" in events[1]["error"]
    assert events[0]["duration_seconds"] is not None


def test_get_quote_has_eta_and_history(store):
    client = TestClient(create_app(store=store, submitter=lambda *a, **k: None))
    body = client.post("/quotes", json={"address": "1 A St"}).json()
    assert body["waiting_for"] == "PreProcess" and body["eta_seconds"] > 0
    for stage in ("PreProcess", "Simulation", "PostProcess"):
        poll_once(store, stage, stub_runner, worker_id="w")
    body = client.get(f"/quotes/{body['quote_id']}").json()
    assert body["phase"] == "Quoted"
    assert (body["waiting_for"], body["eta_seconds"]) == (None, None)
    assert [e["stage"] for e in body["history"]] == [
        "PreProcess", "Simulation", "PostProcess"]
