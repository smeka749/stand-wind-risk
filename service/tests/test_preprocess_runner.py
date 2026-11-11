import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.preprocess_runner import run_preprocess
from app.worker import poll_once

FAKE_OK = '''\
import argparse, sys
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--address", required=True)
ap.add_argument("--out", required=True, type=Path)
a = ap.parse_args()
out = a.out / "constant" / "triSurface" / "buildings.stl"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("fake stl")
print("wrote case")
'''

FAKE_FAIL = '''\
import sys
print("insufficient building points", file=sys.stderr)
sys.exit(3)
'''


def _fake_sme(tmp_path, script):
    sme = tmp_path / "sme"
    sme.mkdir()
    (sme / "preprocess.py").write_text(script)
    return sme


def _quote(store):
    return store.create("169 Tuscaloosa Ave, Atherton, CA")


def test_preprocess_success_returns_case_and_advances(store, tmp_path):
    _quote(store)
    sme = _fake_sme(tmp_path, FAKE_OK)
    work = tmp_path / "work"
    done = poll_once(store, "PreProcess",
                     lambda q: run_preprocess(q, sme_dir=sme, work_root=work))
    assert done is not None
    assert done.phase == "PreProcess" and done.status == "Success"
    assert done.case_s3_key == str(work / done.id / "case")
    assert Path(done.case_s3_key, "constant", "triSurface",
                "buildings.stl").is_file()


def test_preprocess_nonzero_exit_is_visible_fail(store, tmp_path):
    sme = _fake_sme(tmp_path, FAKE_FAIL)
    q = _quote(store)
    with pytest.raises(RuntimeError, match="fewer than 500"):
        run_preprocess(q, sme_dir=sme, work_root=tmp_path / "work")
    # Through the worker loop: Fail, not a crash, and no advance.
    done = poll_once(store, "PreProcess",
                     lambda qq: run_preprocess(qq, sme_dir=sme,
                                              work_root=tmp_path / "work"))
    assert done.phase == "PreProcess" and done.status == "Fail"
    assert "fewer than 500" in (done.error or "")
    assert poll_once(store, "Simulation", lambda _q: {}) is None


def test_preprocess_exit_zero_without_output_is_fail(store, tmp_path):
    sme = _fake_sme(tmp_path, "import sys; sys.exit(0)\n")
    with pytest.raises(RuntimeError, match="no case output"):
        run_preprocess(_quote(store), sme_dir=sme, work_root=tmp_path / "work")


def test_preprocess_timeout_is_fail_not_hang(store, tmp_path):
    sme = _fake_sme(tmp_path, "import time; time.sleep(30)\n")
    with pytest.raises(RuntimeError, match="timed out"):
        run_preprocess(_quote(store), sme_dir=sme, work_root=tmp_path / "work",
                       timeout_seconds=0.2)


def test_preprocess_relative_work_root_is_resolved_from_worker_cwd(store, 
        tmp_path, monkeypatch):
    # The subprocess runs with cwd=SME dir; the case must land under the
    # worker's cwd and come back as an absolute path.
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    monkeypatch.chdir(service_dir)
    sme = _fake_sme(tmp_path, FAKE_OK)
    q = _quote(store)
    result = run_preprocess(q, sme_dir=sme, work_root="work-rel")
    case_dir = Path(result["case_s3_key"])
    assert case_dir.is_absolute()
    assert case_dir == service_dir / "work-rel" / q.id / "case"
    assert (case_dir / "constant" / "triSurface" / "buildings.stl").is_file()
    assert not (sme / "work-rel").exists()


def test_preprocess_missing_script_is_fail(store, tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        run_preprocess(_quote(store), sme_dir=tmp_path / "nope",
                       work_root=tmp_path / "work")
