"""Reference-score gate: scores must be within +/-2 of the SME's reference outputs.

Reference values are read from wind-risk-take-home/reference/*.json, keyed by
filename, never hardcoded. Copy each run's score.json to scores/<same filename>
and run scripts/check_reference_scores.py. A missing score file fails the gate
rather than passing it vacuously.
"""
from __future__ import annotations

import json
from pathlib import Path

TOLERANCE = 2.0


def check_score(reference: float, actual: float,
                tolerance: float = TOLERANCE) -> bool:
    """True when |actual - reference| is within tolerance (inclusive)."""
    return abs(float(actual) - float(reference)) <= tolerance


def _read_score(path: Path) -> float:
    data = json.loads(path.read_text())
    score = data.get("score") if isinstance(data, dict) else None
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise ValueError(f"{path} has no numeric 'score': {data!r}")
    return float(score)


def compare_scores(reference_dir: str | Path, scores_dir: str | Path,
                   tolerance: float = TOLERANCE) -> list[dict]:
    """One row per reference file: {name, reference, actual, diff, ok,
    reason}, where reason is "missing", "invalid" or None."""
    ref_dir, score_dir = Path(reference_dir), Path(scores_dir)
    rows = []
    for ref_path in sorted(ref_dir.glob("*.json")):
        score_path = score_dir / ref_path.name
        reference = _read_score(ref_path)
        if not score_path.is_file():
            rows.append({"name": ref_path.name, "reference": reference,
                         "actual": None, "diff": None, "ok": False,
                         "reason": "missing"})
            continue
        try:
            actual = _read_score(score_path)
        except (ValueError, OSError) as exc:
            rows.append({"name": ref_path.name, "reference": reference,
                         "actual": None, "diff": None, "ok": False,
                         "reason": f"invalid: {exc}"})
            continue
        rows.append({"name": ref_path.name, "reference": reference,
                     "actual": actual, "diff": abs(actual - reference),
                     "ok": check_score(reference, actual, tolerance),
                     "reason": None})
    return rows
