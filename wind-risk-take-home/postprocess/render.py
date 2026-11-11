#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND = "cpu" if os.environ.get("PYVISTA_USE_OSMESA") else "gpu"
FRAMES = 300


def render_flyover(case: Path, out: Path) -> None:
    import numpy as np
    import pyvista as pv

    timesteps = sorted(
        int(d.name) for d in case.iterdir()
        if d.is_dir() and d.name.isdigit() and int(d.name) > 0
    )
    if not timesteps:
        raise RuntimeError(f"no timestep directories under {case}")

    foam_marker = case / "case.foam"
    if not foam_marker.exists():
        foam_marker.touch()
    reader = pv.OpenFOAMReader(str(foam_marker))

    reader.set_active_time_value(float(timesteps[-1]))
    mesh0 = reader.read()
    buildings = mesh0["boundary"]["buildings"]
    house = mesh0["boundary"].get("house")
    slice_z = float(buildings.bounds[5]) * (2 / 3)

    slice_r = 180.0
    per_timestep = {}
    for ts in timesteps:
        reader.set_active_time_value(float(ts))
        m = reader.read()
        internal = m["internalMesh"]
        slc = internal.slice(normal="z", origin=(0, 0, slice_z))
        slc.point_data["_r"] = np.hypot(slc.points[:, 0], slc.points[:, 1])
        slc = slc.clip_scalar(scalars="_r", value=slice_r, invert=True)
        seeds = pv.Disc(center=(0, 0, slice_z), inner=0, outer=slice_r, r_res=10, c_res=14)
        try:
            lines = internal.streamlines_from_source(
                seeds, vectors="U", max_time=200.0, integration_direction="both",
            )
            tubes = lines.tube(radius=0.6) if lines.n_points else None
        except Exception:
            tubes = None
        per_timestep[ts] = (slc, tubes)

    clim_source = per_timestep[timesteps[-1]][0]
    if "p" in clim_source.point_data:
        p_arr = np.asarray(clim_source.point_data["p"])
        coords = np.asarray(clim_source.points)
    else:
        p_arr = np.asarray(clim_source.cell_data["p"])
        coords = np.asarray(clim_source.cell_centers().points)
    r = np.hypot(coords[:, 0], coords[:, 1])
    near = r < 50.0
    clim = (float(p_arr[near].min()), float(p_arr[near].max())) if near.any() else None

    plotter = pv.Plotter(off_screen=True, window_size=(3840, 2160))
    plotter.enable_anti_aliasing("msaa", multi_samples=8)
    plotter.set_background("white")
    plotter.add_mesh(buildings, color="lightgray", opacity=0.35, lighting=True)
    if house is not None:
        plotter.add_mesh(house, color="cyan", lighting=True)

    xmin, xmax, ymin, ymax, _, _ = buildings.bounds
    domain_r = float(np.hypot(max(abs(xmin), abs(xmax)), max(abs(ymin), abs(ymax))))
    focal = np.array([0.0, 0.0, slice_z])
    elevation_deg = 30.0
    cam_r = domain_r * 2.0

    slc_actor = None
    tubes_actor = None
    current_ts = None

    plotter.open_movie(str(out), framerate=30, quality=7)
    for i in range(FRAMES):
        ts_idx = min(int(i * len(timesteps) / FRAMES), len(timesteps) - 1)
        ts = timesteps[ts_idx]
        if ts != current_ts:
            current_ts = ts
            slc, tubes = per_timestep[ts]
            if slc_actor is not None:
                plotter.remove_actor(slc_actor)
            if tubes_actor is not None:
                plotter.remove_actor(tubes_actor)
            slc_actor = plotter.add_mesh(
                slc, scalars="p", cmap="RdBu_r", clim=clim,
                show_scalar_bar=(i == 0),
                scalar_bar_args={"title": "p (near-house)"} if i == 0 else None,
            )
            if tubes is not None:
                tubes_actor = plotter.add_mesh(
                    tubes, scalars="U", cmap="viridis", show_scalar_bar=False,
                )

        theta = 2 * np.pi * i / FRAMES
        cam = focal + np.array([
            cam_r * np.cos(theta) * np.cos(np.radians(elevation_deg)),
            cam_r * np.sin(theta) * np.cos(np.radians(elevation_deg)),
            cam_r * np.sin(np.radians(elevation_deg)),
        ])
        plotter.camera_position = [tuple(cam), tuple(focal), (0, 0, 1)]
        plotter.write_frame()
    plotter.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)

    print(f"render_backend={BACKEND}", file=sys.stderr)
    render_flyover(args.case, args.out)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
