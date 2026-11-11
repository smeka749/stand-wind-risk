import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.postprocess_runner import run_postprocess
from app.store import Quote
from app.worker import poll_once

FAKE_SCORE_OK = '''\
import argparse, json, sys
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--case", required=True, type=Path)
ap.add_argument("--out", required=True, type=Path)
a = ap.parse_args()
a.out.parent.mkdir(parents=True, exist_ok=True)
a.out.write_text(json.dumps({"score": 71.5, "cp_peak": -1.2,
    "wind_speed": 20.0, "wind_deg": 340.0,
    "score_components": {"cp_peak_contribution": 71.5}}))
print("wrote score")
'''

FAKE_RENDER_OK = '''\
import argparse, sys
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--case", required=True, type=Path)
ap.add_argument("--out", required=True, type=Path)
a = ap.parse_args()
a.out.parent.mkdir(parents=True, exist_ok=True)
a.out.write_bytes(b"fake mp4")
print("wrote video")
'''

FAKE_SCORE_NO_HOUSE = '''\
import sys
print("target patch not found in mesh", file=sys.stderr)
sys.exit(2)
'''

FAKE_SCORE_BAD = '''\
import sys
print("no timestep directories", file=sys.stderr)
sys.exit(1)
'''


def _fake_sme(tmp_path, score_script, render_script=FAKE_RENDER_OK):
    sme = tmp_path / "sme"
    (sme / "postprocess").mkdir(parents=True)
    (sme / "postprocess" / "score.py").write_text(score_script)
    (sme / "postprocess" / "render.py").write_text(render_script)
    return sme


def _case_dir(tmp_path, quote_id="q1"):
    case = tmp_path / "work" / quote_id / "case"
    case.mkdir(parents=True)
    return case


def _quote(case_dir):
    return Quote(id="q1", address="169 Tuscaloosa Ave, Atherton, CA",
                 normalized_address="169 tuscaloosa ave, atherton, ca",
                 case_s3_key=str(case_dir), field_s3_key=str(case_dir))


def _to_simulation_success(store, case):
    poll_once(store, "PreProcess", lambda _q: {"case_s3_key": str(case)})
    poll_once(store, "Simulation", lambda _q: {"field_s3_key": str(case)})


def test_postprocess_success_returns_score_video_and_quotes(store, tmp_path):
    q = store.create("169 Tuscaloosa Ave, Atherton, CA")
    case = _case_dir(tmp_path, q.id)
    _to_simulation_success(store, case)
    sme = _fake_sme(tmp_path, FAKE_SCORE_OK)
    done = poll_once(store, "PostProcess",
                     lambda qq: run_postprocess(qq, sme_dir=sme,
                                               work_root=tmp_path / "work"))
    assert done.phase == "Quoted" and done.status == "Success"
    assert done.score == 71.5
    assert done.video_s3_key is not None
    video = Path(done.video_s3_key)
    assert video.is_file() and video.stat().st_size > 0
    assert video.name == "video.mp4"
    assert (tmp_path / "work" / q.id / "score.json").is_file()


def test_postprocess_relative_work_root_is_resolved_from_worker_cwd(store, 
        tmp_path, monkeypatch):
    # The subprocess runs with cwd=SME dir; relative paths must resolve
    # against the worker's cwd.
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    monkeypatch.chdir(service_dir)
    q = store.create("169 Tuscaloosa Ave, Atherton, CA")
    case = tmp_path / "work" / q.id / "case"
    case.mkdir(parents=True)
    _to_simulation_success(store, case)
    sme = _fake_sme(tmp_path, FAKE_SCORE_OK)
    result = run_postprocess(_quote(case), sme_dir=sme, work_root="work-rel")
    assert result["score"] == 71.5
    assert Path(result["video_s3_key"]).is_absolute()
    assert (service_dir / "work-rel" / "q1" / "video.mp4").is_file()
    assert not (sme / "work-rel").exists()


def test_postprocess_score_failure_does_not_render(store, tmp_path):
    q = store.create("169 Tuscaloosa Ave, Atherton, CA")
    case = _case_dir(tmp_path, q.id)
    _to_simulation_success(store, case)
    # Render fake would write a video if called; score fails first.
    sme = _fake_sme(tmp_path, FAKE_SCORE_NO_HOUSE)
    with pytest.raises(RuntimeError, match="house.*patch not found"):
        run_postprocess(_quote(case), sme_dir=sme,
                        work_root=tmp_path / "work")
    done = poll_once(store, "PostProcess",
                     lambda qq: run_postprocess(qq, sme_dir=sme,
                                               work_root=tmp_path / "work"))
    assert done.phase == "PostProcess" and done.status == "Fail"
    assert "house" in (done.error or "")
    assert not (tmp_path / "work" / q.id / "video.mp4").exists()
    assert done.score is None


def test_postprocess_score_exit_1_is_fail(tmp_path):
    case = _case_dir(tmp_path)
    sme = _fake_sme(tmp_path, FAKE_SCORE_BAD)
    with pytest.raises(RuntimeError, match="scoring failed"):
        run_postprocess(_quote(case), sme_dir=sme,
                        work_root=tmp_path / "work")


def test_postprocess_render_failure_is_fail(tmp_path):
    case = _case_dir(tmp_path)
    sme = _fake_sme(tmp_path, FAKE_SCORE_OK,
                    "import sys\nprint('pyvista failed', file=sys.stderr)\n"
                    "sys.exit(1)\n")
    with pytest.raises(RuntimeError, match="render failed"):
        run_postprocess(_quote(case), sme_dir=sme,
                        work_root=tmp_path / "work")


def test_postprocess_exit_zero_without_outputs_is_fail(tmp_path):
    case = _case_dir(tmp_path)
    sme = _fake_sme(tmp_path, "import sys; sys.exit(0)\n",
                    "import sys; sys.exit(0)\n")
    with pytest.raises(RuntimeError, match="no score output|not valid JSON|no numeric score"):
        run_postprocess(_quote(case), sme_dir=sme,
                        work_root=tmp_path / "work")


def test_postprocess_invalid_score_json_is_fail(tmp_path):
    case = _case_dir(tmp_path)
    sme = _fake_sme(tmp_path,
                    "import argparse, sys\nfrom pathlib import Path\n"
                    "ap = argparse.ArgumentParser()\n"
                    "ap.add_argument('--case', type=Path)\n"
                    "ap.add_argument('--out', type=Path)\n"
                    "a = ap.parse_args()\n"
                    "a.out.parent.mkdir(parents=True, exist_ok=True)\n"
                    "a.out.write_text('not json')\n")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        run_postprocess(_quote(case), sme_dir=sme,
                        work_root=tmp_path / "work")


def test_postprocess_without_case_is_fail(store, tmp_path):
    q = store.create("169 Tuscaloosa Ave, Atherton, CA")
    with pytest.raises(RuntimeError, match="no case directory"):
        run_postprocess(q, sme_dir=tmp_path)
    assert poll_once(store, "PostProcess",
                     lambda qq: run_postprocess(qq, sme_dir=tmp_path)) is None


def test_postprocess_timeout_is_fail_not_hang(tmp_path):
    case = _case_dir(tmp_path)
    sme = _fake_sme(tmp_path, "import time; time.sleep(30)\n")
    with pytest.raises(RuntimeError, match="timed out"):
        run_postprocess(_quote(case), sme_dir=sme,
                        work_root=tmp_path / "work",
                        score_timeout_seconds=0.2)


def test_postprocess_missing_script_is_fail(tmp_path):
    case = _case_dir(tmp_path)
    with pytest.raises(RuntimeError, match="not found"):
        run_postprocess(_quote(case), sme_dir=tmp_path / "nope",
                        work_root=tmp_path / "work")
