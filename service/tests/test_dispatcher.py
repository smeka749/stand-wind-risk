import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.dispatcher import NEXT_STAGE, submit_stage_job
from app.main import create_app
from app.worker import poll_once, stub_runner


class FakeBatch:
    def __init__(self):
        self.submitted = []

    def submit_job(self, jobName, jobQueue, jobDefinition):
        self.submitted.append((jobQueue, jobDefinition))
        return {"jobId": f"job-{len(self.submitted)}"}


def _configure(monkeypatch):
    for stage in ("PREPROCESS", "SIMULATION", "POSTPROCESS"):
        monkeypatch.setenv(f"BATCH_QUEUE_{stage}", f"q-{stage.lower()}")
        monkeypatch.setenv(f"BATCH_JOBDEF_{stage}", f"jd-{stage.lower()}")


def test_no_queue_configured_submits_nothing(monkeypatch):
    monkeypatch.delenv("BATCH_QUEUE_PREPROCESS", raising=False)
    batch = FakeBatch()
    assert submit_stage_job("PreProcess", "abc", batch=batch) is None
    assert batch.submitted == []


def test_unconfigured_stage_in_aws_mode_is_logged_not_silent(monkeypatch, caplog):
    import json
    import logging

    caplog.set_level(logging.INFO)
    for stage in ("PREPROCESS", "SIMULATION", "POSTPROCESS"):
        monkeypatch.delenv(f"BATCH_QUEUE_{stage}", raising=False)
        monkeypatch.delenv(f"BATCH_JOBDEF_{stage}", raising=False)
    # Partly configured: only PreProcess.
    monkeypatch.setenv("BATCH_QUEUE_PREPROCESS", "q-preprocess")
    monkeypatch.setenv("BATCH_JOBDEF_PREPROCESS", "jd-preprocess")
    batch = FakeBatch()
    assert submit_stage_job("Simulation", "abc", batch=batch) is None
    assert batch.submitted == []
    events = [json.loads(r.getMessage()) for r in caplog.records
              if r.getMessage().startswith("{")]
    skipped = [e for e in events if e["event"] == "job_submit_skipped"]
    assert skipped and skipped[0]["stage"] == "Simulation"
    assert skipped[0]["quote_id"] == "abc"


def test_submit_uses_the_stage_queue(monkeypatch):
    _configure(monkeypatch)
    batch = FakeBatch()
    assert submit_stage_job("Simulation", "abc", batch=batch) == "job-1"
    assert batch.submitted == [("q-simulation", "jd-simulation")]


def test_submit_failure_is_logged_not_raised(monkeypatch):
    _configure(monkeypatch)

    class Broken:
        def submit_job(self, **_):
            raise RuntimeError("throttled")

    assert submit_stage_job("PreProcess", "abc", batch=Broken()) is None


def _client(store, calls):
    return TestClient(create_app(store=store,
                                 submitter=lambda s, quote_id: calls.append(s)))


def test_api_starts_preprocess_on_create_and_on_repeat_while_waiting(store):
    calls = []
    client = _client(store, calls)
    client.post("/quotes", json={"address": "1 A St"})
    client.post("/quotes", json={"address": "1 A St"})   # still waiting
    assert calls == ["PreProcess", "PreProcess"]


def test_repeat_post_resumes_a_quote_waiting_for_the_next_stage(store):
    calls = []
    client = _client(store, calls)
    client.post("/quotes", json={"address": "1 A St"})
    poll_once(store, "PreProcess", stub_runner)   # Success; next submit "lost"
    client.post("/quotes", json={"address": "1 A St"})
    assert calls == ["PreProcess", "Simulation"]


def test_repeat_post_retries_a_failed_quote(store):
    calls = []
    client = _client(store, calls)
    client.post("/quotes", json={"address": "1 A St"})

    def flaky(_q):
        raise RuntimeError("geocoding failed (exit 4)")

    poll_once(store, "PreProcess", flaky)
    body = client.post("/quotes", json={"address": "1 A St"}).json()
    assert (body["phase"], body["status"], body["error"]) == ("Requested", "Created", None)
    assert calls == ["PreProcess", "PreProcess"]


def test_running_or_done_quote_gets_no_new_job(store):
    calls = []
    client = _client(store, calls)
    q = store.create("1 A St")
    store.claim_next("PreProcess", "w1")
    client.post("/quotes", json={"address": "1 A St"})
    assert calls == []


def test_worker_starts_next_stage_only_on_success(store):
    store.create("1 A St")
    started = []
    poll_once(store, "PreProcess", stub_runner,
              on_success=lambda q: started.append(NEXT_STAGE["PreProcess"]))
    assert started == ["Simulation"]

    store.create("2 B St")

    def boom(_q):
        raise RuntimeError("no LiDAR")

    poll_once(store, "PreProcess", boom, on_success=lambda q: started.append("X"))
    assert started == ["Simulation"]
