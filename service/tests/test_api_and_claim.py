import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import create_app
from app.worker import poll_once, stub_runner


def make_client(store):
    app = create_app(store=store, presigner=lambda k: f"https://example.test/{k}")
    return TestClient(app), store


def test_create_is_idempotent_on_normalized_address(store):
    client, _ = make_client(store)
    r1 = client.post("/quotes", json={"address": "169 Tuscaloosa Ave, Atherton, CA"})
    r2 = client.post("/quotes", json={"address": "  169   tuscaloosa ave, ATHERTON, ca "})
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["quote_id"] == r2.json()["quote_id"]
    assert r1.json()["phase"] == "Requested"


def test_full_flow_through_stages_with_stub_runner(store):
    client, store = make_client(store)
    qid = client.post("/quotes", json={"address": "17 Concord Ave, Cambridge, MA"}).json()["quote_id"]
    # PreProcess: claim -> Success -> eligible for Simulation, not before
    assert poll_once(store, "Simulation", stub_runner) is None
    done = poll_once(store, "PreProcess", stub_runner)
    assert done.phase == "PreProcess" and done.status == "Success"
    done = poll_once(store, "Simulation", stub_runner)
    assert done.phase == "Simulation" and done.status == "Success"
    # PostProcess completes with a video key; API mints URL on read
    done = poll_once(store, "PostProcess",
                     lambda _q: {"video_s3_key": "quotes/x/video.mp4", "score": 71.5})
    assert done.phase == "Quoted" and done.status == "Success"
    resp = client.get(f"/quotes/{qid}").json()
    assert resp["score"] == 71.5
    assert resp["video_url"] == "https://example.test/quotes/x/video.mp4"


def test_failed_stage_does_not_advance_and_is_visible(store):
    _, store = make_client(store)
    store.create("15 Valencia Ct, Portola Valley, CA")

    def boom(_q):
        raise RuntimeError("preprocess failed: no buildings found")

    done = poll_once(store, "PreProcess", boom)
    assert done.phase == "PreProcess" and done.status == "Fail"
    assert "no buildings" in done.error
    assert poll_once(store, "Simulation", stub_runner) is None
