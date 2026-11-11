import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.reference_gate import TOLERANCE, check_score, compare_scores

REF = {"score": 64.74, "cp_peak": -1.4948, "wind_speed": 21.9, "wind_deg": 339.2}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_check_score_tolerance_boundary():
    assert check_score(64.74, 64.74)
    assert check_score(64.74, 66.74)   # exactly +2 passes
    assert check_score(64.74, 62.74)   # exactly -2 passes
    assert not check_score(64.74, 66.75)
    assert not check_score(64.74, 62.73)
    assert TOLERANCE == 2.0


def test_compare_scores_reads_reference_files_not_hardcoded(tmp_path):
    # Values come from the reference files (Atherton is 64.74).
    ref_dir, scores_dir = tmp_path / "reference", tmp_path / "scores"
    _write(ref_dir / "169-tuscaloosa-ave-atherton-ca-94027.json", REF)
    _write(scores_dir / "169-tuscaloosa-ave-atherton-ca-94027.json",
           {"score": 66.0})
    rows = compare_scores(ref_dir, scores_dir)
    assert len(rows) == 1
    assert rows[0]["reference"] == 64.74
    assert rows[0]["actual"] == 66.0
    assert rows[0]["diff"] == pytest.approx(1.26)
    assert rows[0]["ok"] is True


def test_compare_scores_missing_is_fail(tmp_path):
    ref_dir, scores_dir = tmp_path / "reference", tmp_path / "scores"
    _write(ref_dir / "169-tuscaloosa-ave-atherton-ca-94027.json", REF)
    scores_dir.mkdir()
    rows = compare_scores(ref_dir, scores_dir)
    assert rows[0]["ok"] is False
    assert rows[0]["reason"] == "missing"


def test_gate_cli_pass_and_fail(tmp_path):
    ref_dir, scores_dir = tmp_path / "reference", tmp_path / "scores"
    _write(ref_dir / "a.json", {"score": 70.0})
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_reference_scores.py"
    _write(scores_dir / "a.json", {"score": 71.0})
    ok = subprocess.run([sys.executable, str(script), "--reference-dir",
                         str(ref_dir), "--scores-dir", str(scores_dir)],
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout
    _write(scores_dir / "a.json", {"score": 73.0})
    bad = subprocess.run([sys.executable, str(script), "--reference-dir",
                          str(ref_dir), "--scores-dir", str(scores_dir)],
                         capture_output=True, text=True)
    assert bad.returncode == 1
    assert "FAIL" in bad.stdout


def test_gate_against_api(monkeypatch, tmp_path, capsys):
    """--api: submits each reference address, polls, gates the scores."""
    import scripts.check_reference_scores as cli
    ref = tmp_path / "ref"; ref.mkdir()
    for name in cli.ADDRESSES:
        (ref / name).write_text('{"score": 70.0}')
    scores = {cli.ADDRESSES[n]: s for n, s in
              zip(sorted(cli.ADDRESSES), (71.5, 73.0))}   # pass, fail
    by_id = {}

    def fake_call(url, body=None):
        if body is not None:
            by_id[f"id{len(by_id)}"] = body["address"]
            return {"quote_id": f"id{len(by_id) - 1}", "phase": "Requested",
                    "status": "Created"}
        address = by_id[url.rsplit("/", 1)[1]]
        return {"quote_id": "x", "phase": "Quoted", "status": "Success",
                "score": scores.get(address, 50.0), "error": None}

    monkeypatch.setattr(cli, "_call", fake_call)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    code = cli.main(["--api", "http://x", "--reference-dir", str(ref),
                     "--address", "1 Extra St"])
    out = capsys.readouterr().out
    assert code == 1
    assert out.count("PASS") == 1 and "diff=3.00" in out
    assert "OK   1 Extra St: score=50.0" in out
