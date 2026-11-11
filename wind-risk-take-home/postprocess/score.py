#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

RHO = 1.225


@dataclass
class Score:
    score: float
    cp_peak: float
    wind_speed: float
    wind_deg: float
    score_components: dict


def final_timestep_dir(case: Path) -> Path:
    times = []
    for entry in case.iterdir():
        if not entry.is_dir():
            continue
        try:
            t = int(entry.name)
        except ValueError:
            continue
        if t == 0:
            continue
        times.append((t, entry))
    if not times:
        raise RuntimeError(f"no timestep directories under {case}")
    times.sort()
    return times[-1][1]


def read_boundary_patches(case: Path) -> dict[str, tuple[int, int]]:
    bpath = case / "constant" / "polyMesh" / "boundary"
    text = bpath.read_text()

    patches: dict[str, tuple[int, int]] = {}
    entry_re = re.compile(
        r"(\w+)\s*\{[^}]*?nFaces\s+(\d+);[^}]*?startFace\s+(\d+);",
        re.DOTALL,
    )
    for m in entry_re.finditer(text):
        name, n_faces, start = m.group(1), int(m.group(2)), int(m.group(3))
        patches[name] = (n_faces, start)
    return patches


def read_patch_scalar(case: Path, time_name: str, field: str, patch: str) -> list[float]:
    import fluidfoam

    arr = fluidfoam.readscalar(
        str(case), time_name=time_name, name=field, boundary=patch, verbose=False
    )
    return list(arr)


def read_vector_internal_field(field_path: Path) -> list[tuple[float, float, float]]:
    text = field_path.read_text()
    m = re.search(
        r"internalField\s+uniform\s*\(\s*([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s*\)",
        text,
    )
    if m:
        return [(float(m.group(1)), float(m.group(2)), float(m.group(3)))]
    raise RuntimeError(f"non-uniform internalField in {field_path} — not supported here")


def cp_lookup(cp_peak: float) -> float:
    cp_abs = abs(cp_peak)
    breakpoints = [(0.0, 0.0), (0.5, 15.0), (1.0, 40.0), (1.5, 65.0), (2.5, 90.0), (4.0, 100.0)]
    for (x0, y0), (x1, y1) in zip(breakpoints, breakpoints[1:]):
        if cp_abs <= x1:
            frac = (cp_abs - x0) / (x1 - x0) if x1 > x0 else 0.0
            return y0 + frac * (y1 - y0)
    return 100.0


def read_wind_from_U(zero_U: Path) -> tuple[float, float]:
    ux, uy, _ = read_vector_internal_field(zero_U)[0]
    speed = math.hypot(ux, uy)
    text = zero_U.read_text()
    m = re.search(r"//\s*wind_deg_input\s*=\s*([-\d.]+)", text)
    deg = float(m.group(1)) if m else (math.degrees(math.atan2(-ux, -uy)) % 360.0 if speed else 0.0)
    return speed, deg


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)

    patches = read_boundary_patches(args.case)
    if "house" not in patches:
        print("target patch not found in mesh", file=sys.stderr)
        return 2

    try:
        final = final_timestep_dir(args.case)
        p_face = read_patch_scalar(args.case, final.name, "p", "house")
    except RuntimeError as exc:
        print(f"score: {exc}", file=sys.stderr)
        return 1

    if not p_face:
        print("empty p field on buildings patch", file=sys.stderr)
        return 1

    p_ref_kin = 0.0
    speed, deg = read_wind_from_U(args.case / "0" / "U")
    if speed == 0:
        print("wind speed zero — Cp undefined", file=sys.stderr)
        return 1

    p_peak_kin = max(p_face, key=abs)
    cp_peak = (p_peak_kin - p_ref_kin) / (0.5 * speed**2)
    score = cp_lookup(cp_peak)

    out = Score(
        score=round(score, 2),
        cp_peak=round(cp_peak, 4),
        wind_speed=round(speed, 4),
        wind_deg=round(deg, 2),
        score_components={"cp_peak_contribution": round(score, 2)},
    )
    args.out.write_text(json.dumps(asdict(out), indent=2))
    print(f"wrote {args.out} — score={out.score}, cp_peak={out.cp_peak}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
