import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.simulation_runner import run_simulation
from app.store import Quote
from app.worker import poll_once


def _fake_sme(tmp_path, script):
    sme = tmp_path / "sme"
    sme.mkdir(exist_ok=True)
    (sme / "run_simulation.sh").write_text(script)
    return sme


def _case_dir(tmp_path, quote_id="q1"):
    case = tmp_path / "work" / quote_id / "case"
    case.mkdir(parents=True)
    return case


def _quote(case_dir):
    return Quote(id="q1", address="169 Tuscaloosa Ave, Atherton, CA",
                 normalized_address="169 tuscaloosa ave, atherton, ca",
                 case_s3_key=str(case_dir))


FAKE_OK = """\
#!/usr/bin/env bash
# Fake run_simulation.sh: mimics the real contract -- writes the
# converged log, case.foam, and a solved timestep with a p field.
CASE_DIR="$1"
echo "solution converged" > "$CASE_DIR/log.simpleFoam"
touch "$CASE_DIR/case.foam"
mkdir -p "$CASE_DIR/100"
touch "$CASE_DIR/100/p"
exit 0
"""

FAKE_NONCONVERGED = """\
#!/usr/bin/env bash
CASE_DIR="$1"
echo "solution did not converge after 100 iterations" > "$CASE_DIR/log.simpleFoam"
exit 1
"""


def test_simulation_success_returns_field_and_advances(store, tmp_path):
    q = store.create("169 Tuscaloosa Ave, Atherton, CA")
    case = _case_dir(tmp_path, q.id)
    # PreProcess stage first, via a stub that records the case dir.
    done = poll_once(store, "PreProcess",
                     lambda _q: {"case_s3_key": str(case)})
    assert done.phase == "PreProcess" and done.status == "Success"
    sme = _fake_sme(tmp_path, FAKE_OK)
    done = poll_once(store, "Simulation",
                     lambda qq: run_simulation(qq, sme_dir=sme))
    assert done.phase == "Simulation" and done.status == "Success"
    assert done.field_s3_key == str(case)
    assert done.case_s3_key == str(case)


def test_simulation_nonconvergence_is_visible_fail(store, tmp_path):
    q = store.create("169 Tuscaloosa Ave, Atherton, CA")
    case = _case_dir(tmp_path, q.id)
    poll_once(store, "PreProcess", lambda _q: {"case_s3_key": str(case)})
    sme = _fake_sme(tmp_path, FAKE_NONCONVERGED)
    with pytest.raises(RuntimeError, match="did not converge"):
        run_simulation(_quote(case), sme_dir=sme)
    done = poll_once(store, "Simulation",
                     lambda qq: run_simulation(qq, sme_dir=sme))
    assert done.phase == "Simulation" and done.status == "Fail"
    assert "did not converge" in (done.error or "")
    # A failed Simulation does not advance to PostProcess.
    assert poll_once(store, "PostProcess", lambda _q: {}) is None


def test_simulation_without_case_is_fail(store, tmp_path):
    q = store.create("169 Tuscaloosa Ave, Atherton, CA")
    # Simulation can't claim it, and the runner also refuses a quote with no case.
    with pytest.raises(RuntimeError, match="no case directory"):
        run_simulation(q, sme_dir=tmp_path)
    assert poll_once(store, "Simulation",
                     lambda qq: run_simulation(qq, sme_dir=tmp_path)) is None


def test_simulation_exit_zero_without_outputs_is_fail(tmp_path):
    case = _case_dir(tmp_path)
    sme = _fake_sme(tmp_path, "#!/usr/bin/env bash\nexit 0\n")
    with pytest.raises(RuntimeError, match="case.foam|solution converged|no solved timestep"):
        run_simulation(_quote(case), sme_dir=sme)


def test_simulation_step_failure_exit_3(tmp_path):
    case = _case_dir(tmp_path)
    sme = _fake_sme(tmp_path,
                    "#!/usr/bin/env bash\necho 'blockMesh failed' >&2\nexit 3\n")
    with pytest.raises(RuntimeError, match="OpenFOAM step failed"):
        run_simulation(_quote(case), sme_dir=sme)


def test_simulation_timeout_is_fail_not_hang(tmp_path):
    case = _case_dir(tmp_path)
    sme = _fake_sme(tmp_path, "#!/usr/bin/env bash\nsleep 30\n")
    with pytest.raises(RuntimeError, match="timed out"):
        run_simulation(_quote(case), sme_dir=sme, timeout_seconds=0.2)


def test_simulation_missing_script_is_fail(tmp_path):
    case = _case_dir(tmp_path)
    with pytest.raises(RuntimeError, match="not found"):
        run_simulation(_quote(case), sme_dir=tmp_path / "nope")
