"""Per-stage timing, structured logging, and S3 mode with a fake client
(no real AWS calls). Local-path behaviour (S3_BUCKET unset) is covered
by the per-stage runner tests.
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.logging_utils import StageTimer, log_event
from app.main import create_app
from app.postprocess_runner import run_postprocess
from app.preprocess_runner import run_preprocess
from app.s3util import is_s3_key, make_presigner, parse_s3_key, s3_uri
from app.simulation_runner import run_simulation
from app.store import database_url_from_env
from app.worker import poll_once
from fastapi.testclient import TestClient



class FakeS3:
    """In-memory S3: {(bucket, key): bytes}. Same surface as s3util uses."""

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = bytes(Body) if isinstance(Body, (bytes, bytearray)) else Body.read()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        contents = [{"Key": k} for (b, k) in sorted(self.objects)
                    if b == Bucket and k.startswith(Prefix)]
        return {"Contents": contents, "IsTruncated": False}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return (f"https://{Params['Bucket']}.s3.amazonaws.com/"
                f"{Params['Key']}?expires={ExpiresIn}")


# ------------------------------------------------------------ timing
def test_poll_once_records_wrapper_timing(store, tmp_path):
    q = store.create("169 Tuscaloosa Ave, Atherton, CA")
    done = poll_once(store, "PreProcess", lambda _q: {"case_s3_key": "x"})
    assert done is not None
    assert done.stage_name == "PreProcess"
    assert done.stage_started_at and done.stage_ended_at
    assert done.stage_started_at <= done.stage_ended_at
    assert done.stage_duration_seconds is not None
    assert done.stage_duration_seconds >= 0
    # Timing is also recorded on a Fail, alongside the error.
    store.create("1265 Lands End Rd, Lantana, FL")

    def boom(_q):
        raise RuntimeError("boom")

    failed = poll_once(store, "PreProcess", boom)
    assert failed.status == "Fail"
    assert failed.stage_name == "PreProcess"
    assert failed.stage_duration_seconds is not None
    assert failed.stage_duration_seconds >= 0


def test_stage_timer_is_wall_time_not_openfoam_time():
    timer = StageTimer(stage="Simulation", quote_id="q1")
    timing = timer.finish()
    assert timing["stage_name"] == "Simulation"
    assert timing["stage_started_at"] and timing["stage_ended_at"]
    assert 0 <= timing["stage_duration_seconds"] < 60


def test_log_event_is_json_with_quote_id(caplog):
    import logging

    caplog.set_level(logging.INFO)
    logger = logging.getLogger("test-s3-and-timing")
    log_event(logger, "stage_succeeded", quote_id="q1", stage="Simulation",
              stage_duration_seconds=12.5)
    assert caplog.records
    record = json.loads(caplog.records[-1].getMessage())
    assert record["event"] == "stage_succeeded"
    assert record["quote_id"] == "q1"
    assert record["stage_duration_seconds"] == 12.5


def test_database_url_from_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    assert database_url_from_env() is None
    monkeypatch.setenv("DB_HOST", "db.example.test")
    monkeypatch.setenv("DB_PASSWORD", "secret")
    dsn = database_url_from_env()
    assert dsn.startswith("postgresql://windrisk:secret@db.example.test:5432/windrisk")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    assert database_url_from_env() == "postgresql://x"


# ------------------------------------------------------------ S3 mode
def _fake_preprocess_sme(tmp_path):
    sme = tmp_path / "sme"
    sme.mkdir(exist_ok=True)
    (sme / "preprocess.py").write_text(
        'import argparse\nfrom pathlib import Path\n'
        'ap = argparse.ArgumentParser()\n'
        'ap.add_argument("--address", required=True)\n'
        'ap.add_argument("--out", required=True, type=Path)\n'
        'a = ap.parse_args()\n'
        'out = a.out / "constant" / "triSurface" / "buildings.stl"\n'
        'out.parent.mkdir(parents=True, exist_ok=True)\n'
        'out.write_text("fake stl")\n')
    return sme


def test_preprocess_s3_mode_uploads_case_and_returns_s3_key(store, tmp_path):
    fake = FakeS3()
    q = store.create("169 Tuscaloosa Ave, Atherton, CA")
    sme = _fake_preprocess_sme(tmp_path)
    done = poll_once(store, "PreProcess",
                     lambda qq: run_preprocess(
                         qq, sme_dir=sme, work_root=tmp_path / "work",
                         s3_bucket="test-bucket", s3_client=fake))
    assert done.status == "Success"
    assert done.case_s3_key == "s3://test-bucket/quotes/" + q.id + "/case"
    assert is_s3_key(done.case_s3_key)
    bucket, key = parse_s3_key(done.case_s3_key)
    assert bucket == "test-bucket"
    assert ("test-bucket", f"quotes/{q.id}/case/constant/triSurface/buildings.stl") in fake.objects


def test_simulation_s3_mode_downloads_then_uploads(tmp_path):
    fake = FakeS3()
    # Seed a fake case in S3, as PreProcess would have left it.
    fake.objects[("test-bucket", "quotes/q1/case/case.foam")] = b""
    fake.objects[("test-bucket", "quotes/q1/case/log.simpleFoam")] = b"solution converged"
    fake.objects[("test-bucket", "quotes/q1/case/100/p")] = b"p"
    sme = tmp_path / "sme"
    sme.mkdir()
    (sme / "run_simulation.sh").write_text(
        "#!/usr/bin/env bash\nCASE_DIR=\"$1\"\n"
        "echo 'solution converged' > \"$CASE_DIR/log.simpleFoam\"\n"
        "touch \"$CASE_DIR/case.foam\"\nmkdir -p \"$CASE_DIR/100\"\ntouch \"$CASE_DIR/100/p\"\n")
    from app.store import Quote

    quote = Quote(id="q1", address="169 Tuscaloosa Ave, Atherton, CA",
                  normalized_address="169 tuscaloosa ave, atherton, ca",
                  case_s3_key="s3://test-bucket/quotes/q1/case")
    result = run_simulation(quote, sme_dir=sme, work_root=tmp_path / "work",
                            s3_bucket="test-bucket", s3_client=fake)
    assert result["field_s3_key"] == "s3://test-bucket/quotes/q1/case"
    # The downloaded case was run locally, then uploaded back.
    assert (tmp_path / "work" / "q1" / "case" / "case.foam").is_file()


def test_upload_dir_skips_excluded_top_level_dirs(tmp_path):
    from app.s3util import upload_dir

    case = tmp_path / "case"
    for rel in ["constant/polyMesh/points", "100/p", "case.foam",
                "processor0/100/p", "processor15/constant/x", "log.simpleFoam"]:
        (case / rel).parent.mkdir(parents=True, exist_ok=True)
        (case / rel).write_text(rel)
    fake = FakeS3()
    upload_dir(fake, "b", case, "quotes/q1/case", exclude=("processor*",))
    keys = sorted(k for (_, k) in fake.objects)
    assert keys == ["quotes/q1/case/100/p", "quotes/q1/case/case.foam",
                    "quotes/q1/case/constant/polyMesh/points",
                    "quotes/q1/case/log.simpleFoam"]


def test_parallel_round_trip_preserves_every_file(tmp_path, monkeypatch):
    from app.s3util import download_dir, upload_dir

    monkeypatch.setenv("S3_TRANSFER_WORKERS", "8")
    src = tmp_path / "src"
    for i in range(200):
        f = src / f"{i % 10}" / f"f{i}.dat"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(f"payload-{i}".encode() * (i + 1))
    fake = FakeS3()
    upload_dir(fake, "b", src, "quotes/q1/case")
    assert len(fake.objects) == 200
    dst = download_dir(fake, "b", "quotes/q1/case", tmp_path / "dst")
    got = {p.relative_to(dst).as_posix(): p.read_bytes()
           for p in dst.rglob("*") if p.is_file()}
    want = {p.relative_to(src).as_posix(): p.read_bytes()
            for p in src.rglob("*") if p.is_file()}
    assert got == want


def test_a_failed_file_fails_the_whole_upload(tmp_path):
    from app.s3util import upload_dir

    src = tmp_path / "src"
    src.mkdir()
    for i in range(20):
        (src / f"f{i}").write_text("x")

    class Flaky(FakeS3):
        def put_object(self, Bucket, Key, Body):
            if Key.endswith("/f7"):
                raise RuntimeError("SlowDown")
            super().put_object(Bucket, Key, Body)

    try:
        upload_dir(Flaky(), "b", src, "quotes/q1/case")
    except RuntimeError as exc:
        assert "SlowDown" in str(exc)
    else:
        raise AssertionError("expected the upload to fail")


def _failing_simulation_sme(tmp_path):
    """An SME stand-in that writes logs, then fails like a non-converged run."""
    sme = tmp_path / "sme"
    sme.mkdir()
    (sme / "run_simulation.sh").write_text(
        "#!/usr/bin/env bash\nCASE_DIR=\"$1\"\n"
        "echo 'Time = 1500' > \"$CASE_DIR/log.simpleFoam\"\n"
        "echo 'Mesh OK.' > \"$CASE_DIR/log.checkMesh\"\n"
        "echo 'solution did not converge after 1500 iterations' >&2\nexit 1\n")
    return sme


def test_simulation_failure_in_s3_mode_keeps_logs(tmp_path):
    from app.store import Quote

    fake = FakeS3()
    fake.objects[("test-bucket", "quotes/q1/case/case.foam")] = b""
    quote = Quote(id="q1", address="17 Concord Ave, Cambridge, MA",
                  normalized_address="17 concord ave, cambridge, ma",
                  case_s3_key="s3://test-bucket/quotes/q1/case")
    try:
        run_simulation(quote, sme_dir=_failing_simulation_sme(tmp_path),
                       work_root=tmp_path / "work",
                       s3_bucket="test-bucket", s3_client=fake)
    except RuntimeError as exc:
        error = str(exc)
    else:
        raise AssertionError("expected the simulation to fail")
    assert "did not converge" in error
    assert error.endswith("(logs: s3://test-bucket/quotes/q1/logs)")
    assert fake.objects[("test-bucket", "quotes/q1/logs/log.simpleFoam")] == b"Time = 1500\n"
    assert ("test-bucket", "quotes/q1/logs/log.checkMesh") in fake.objects
    # Nothing else is uploaded on failure: the case prefix is unchanged.
    assert not [k for (_, k) in fake.objects if k.startswith("quotes/q1/case/log.")]


def test_simulation_failure_without_s3_has_no_logs_suffix(tmp_path):
    from app.store import Quote

    case = tmp_path / "case"
    case.mkdir()
    quote = Quote(id="q1", address="17 Concord Ave, Cambridge, MA",
                  normalized_address="17 concord ave, cambridge, ma",
                  case_s3_key=str(case))
    try:
        run_simulation(quote, sme_dir=_failing_simulation_sme(tmp_path),
                       work_root=tmp_path / "work")
    except RuntimeError as exc:
        assert "did not converge" in str(exc)
        assert "logs:" not in str(exc)
    else:
        raise AssertionError("expected the simulation to fail")


def test_postprocess_s3_mode_uploads_video_and_returns_s3_key(tmp_path):
    fake = FakeS3()
    fake.objects[("test-bucket", "quotes/q1/case/case.foam")] = b""
    sme = tmp_path / "sme"
    (sme / "postprocess").mkdir(parents=True)
    (sme / "postprocess" / "score.py").write_text(
        'import argparse, json\nfrom pathlib import Path\n'
        'ap = argparse.ArgumentParser()\n'
        'ap.add_argument("--case", type=Path)\n'
        'ap.add_argument("--out", type=Path)\n'
        'a = ap.parse_args()\n'
        'a.out.parent.mkdir(parents=True, exist_ok=True)\n'
        'a.out.write_text(json.dumps({"score": 63.09}))\n')
    (sme / "postprocess" / "render.py").write_text(
        'import argparse\nfrom pathlib import Path\n'
        'ap = argparse.ArgumentParser()\n'
        'ap.add_argument("--case", type=Path)\n'
        'ap.add_argument("--out", type=Path)\n'
        'a = ap.parse_args()\n'
        'a.out.parent.mkdir(parents=True, exist_ok=True)\n'
        'a.out.write_bytes(b"fake mp4")\n')
    from app.store import Quote

    quote = Quote(id="q1", address="169 Tuscaloosa Ave, Atherton, CA",
                  normalized_address="169 tuscaloosa ave, atherton, ca",
                  case_s3_key="s3://test-bucket/quotes/q1/case",
                  field_s3_key="s3://test-bucket/quotes/q1/case")
    result = run_postprocess(quote, sme_dir=sme, work_root=tmp_path / "work",
                             s3_bucket="test-bucket", s3_client=fake)
    assert result["score"] == 63.09
    assert result["video_s3_key"] == "s3://test-bucket/quotes/q1/video.mp4"
    assert ("test-bucket", "quotes/q1/video.mp4") in fake.objects
    assert ("test-bucket", "quotes/q1/score.json") in fake.objects


def test_s3_presigner_uses_boto3_client_shape():
    fake = FakeS3()
    presign = make_presigner("test-bucket", client=fake, expiry_seconds=900)
    url = presign("s3://test-bucket/quotes/q1/video.mp4")
    assert url.startswith("https://test-bucket.s3.amazonaws.com/quotes/q1/video.mp4")
    assert "expires=900" in url


def test_api_still_uses_injected_presigner_for_local_keys(store):
    app = create_app(store=store, presigner=lambda k: f"https://example.test/{k}")
    client = TestClient(app)
    qid = client.post("/quotes", json={"address": "169 Tuscaloosa Ave, Atherton, CA"}).json()["quote_id"]
    poll_once(store, "PreProcess", lambda _q: {"case_s3_key": "x"})
    poll_once(store, "Simulation", lambda _q: {"field_s3_key": "x"})
    poll_once(store, "PostProcess",
              lambda _q: {"video_s3_key": "quotes/x/video.mp4", "score": 63.09})
    resp = client.get(f"/quotes/{qid}").json()
    assert resp["video_url"] == "https://example.test/quotes/x/video.mp4"
