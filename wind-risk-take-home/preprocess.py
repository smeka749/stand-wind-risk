#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from _target_geometry import target_geometry
from _wind_conditions import wind_conditions

REPO = Path(__file__).resolve().parent
CASE_TEMPLATE = REPO / "case_template"
COVERAGE_CATALOG = REPO / "usgs_3dep_catalog.json"

MIN_BUILDING_POINTS = 500
POISSON_DEPTH = 10
STL_TARGET_FACES = 15_000
RADIUS_M = 200.0
CELL_SIZE_M = 16.0


@dataclass
class BBox:
    xmin: float
    ymin: float
    xmax: float
    ymax: float

    def as_pdal_bounds(self) -> str:
        return f"([{self.xmin},{self.xmax}],[{self.ymin},{self.ymax}])"


def geocode(address: str) -> tuple[float, float]:
    url = (
        "https://nominatim.openstreetmap.org/search?"
        + urllib.parse.urlencode({"q": address, "format": "json", "limit": "1"})
    )
    req = urllib.request.Request(url, headers={"User-Agent": "sptl-take-home/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        results = json.loads(resp.read())
    if not results:
        raise RuntimeError(f"no geocoding results for {address!r}")
    return float(results[0]["lat"]), float(results[0]["lon"])


def latlon_to_bbox(lat: float, lon: float, radius_m: float) -> BBox:
    from pyproj import Transformer

    to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    cx, cy = to_3857.transform(lon, lat)
    scale = 1.0 / math.cos(math.radians(lat))
    r = radius_m * scale
    return BBox(cx - r, cy - r, cx + r, cy + r)


def load_catalog() -> list[dict]:
    if not COVERAGE_CATALOG.exists():
        return []
    return json.loads(COVERAGE_CATALOG.read_text())


def find_ept_url(lat: float, lon: float) -> str | None:
    for entry in load_catalog():
        bb = entry["latlon_bbox"]
        if bb[0] <= lon <= bb[2] and bb[1] <= lat <= bb[3]:
            return entry["ept_url"]
    return None


def run_pdal_pipeline(
    ept_url: str, bbox_3857: BBox, out_stl: Path, lat: float, lon: float, wind_deg: float
) -> int:
    import pdal

    stages = [
        {
            "type": "readers.ept",
            "filename": ept_url,
            "bounds": bbox_3857.as_pdal_bounds(),
            "resolution": 1.0,
        },
        {"type": "filters.crop", "bounds": bbox_3857.as_pdal_bounds()},
        {"type": "filters.outlier", "method": "statistical", "mean_k": 48, "multiplier": 2.2},
        {"type": "filters.smrf"},
        {"type": "filters.range", "limits": "Classification[6:6]"},
    ]

    count_pipeline = pdal.Pipeline(json.dumps({"pipeline": stages}))
    n = count_pipeline.execute()

    if n < MIN_BUILDING_POINTS:
        return n

    ply_path = out_stl.with_suffix(".ply")
    mesh_stages = stages + [
        {"type": "filters.poisson", "depth": POISSON_DEPTH},
        {
            "type": "writers.ply",
            "filename": str(ply_path),
            "storage_mode": "little endian",
            "faces": True,
        },
    ]
    mesh_pipeline = pdal.Pipeline(json.dumps({"pipeline": mesh_stages}))
    mesh_pipeline.execute()

    cx = 0.5 * (bbox_3857.xmin + bbox_3857.xmax)
    cy = 0.5 * (bbox_3857.ymin + bbox_3857.ymax)
    _write_stls(ply_path, out_stl, (cx, cy), lat, lon, wind_deg)
    return n


def _write_stls(
    ply_path: Path,
    stl_path: Path,
    center: tuple[float, float],
    lat: float,
    lon: float,
    wind_deg: float,
) -> None:
    import numpy as np
    import trimesh

    terrain = trimesh.load(ply_path, force="mesh")

    if len(terrain.faces) > STL_TARGET_FACES:
        terrain = terrain.simplify_quadric_decimation(face_count=STL_TARGET_FACES)

    z_min = float(terrain.vertices[:, 2].min())
    terrain.vertices = terrain.vertices - np.array([center[0], center[1], z_min])

    target = target_geometry(lat, lon)
    footprint_r = float(np.max(np.abs(target.bounds[:, :2]))) + 3.0

    xy_r = np.hypot(terrain.vertices[:, 0], terrain.vertices[:, 1])
    under_footprint = xy_r < footprint_r
    if under_footprint.any():
        z_low = float(terrain.vertices[under_footprint, 2].min())
        z_high = float(terrain.vertices[under_footprint, 2].max())
    else:
        z_low = z_high = 0.0

    target.apply_translation((0.0, 0.0, z_high))
    base_z = z_high
    is_base_vertex = np.isclose(target.vertices[:, 2], base_z, atol=0.1)
    target.vertices[is_base_vertex, 2] = z_low - 2.0

    rot = trimesh.transformations.rotation_matrix(
        math.radians((180.0 + wind_deg) % 360.0), [0, 0, 1]
    )
    terrain.apply_transform(rot)
    target.apply_transform(rot)

    terrain.export(stl_path, file_type="stl")
    target.export(stl_path.parent / "house.stl", file_type="stl")
    ply_path.unlink(missing_ok=True)


def copy_case_template(case_dir: Path) -> None:
    if case_dir.exists():
        shutil.rmtree(case_dir)
    shutil.copytree(CASE_TEMPLATE, case_dir)
    (case_dir / "constant" / "triSurface").mkdir(parents=True, exist_ok=True)


def turbulence_inlets(speed: float) -> tuple[float, float]:
    k = 1.5 * (0.1 * speed) ** 2
    epsilon = (0.09**0.75) * (k**1.5) / 10.0
    return k, epsilon


def write_blockMeshDict(case_dir: Path, bbox: BBox, cell_size_m: float) -> None:
    pad = 40.0
    half_x = 0.5 * (bbox.xmax - bbox.xmin) + pad
    half_y = 0.5 * (bbox.ymax - bbox.ymin) + pad
    x0, x1 = -half_x, half_x
    y0, y1 = -half_y, half_y
    z0, z1 = 0.0, 200.0

    nx = max(20, int((x1 - x0) / cell_size_m))
    ny = max(20, int((y1 - y0) / cell_size_m))
    nz = max(10, int((z1 - z0) / cell_size_m))

    dict_text = f"""/*--------------------------------*- C++ -*----------------------------------*\\
| =========                 |                                                 |
| \\\\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox           |
|  \\\\    /   O peration     | sptl-take-home stub                             |
|   \\\\  /    A nd           |                                                 |
|    \\\\/     M anipulation  |                                                 |
\\*---------------------------------------------------------------------------*/
FoamFile
{{
    version     2.0;
    format      ascii;
    class       dictionary;
    location    "system";
    object      blockMeshDict;
}}
// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //

scale   1;

vertices
(
    ({x0:.4f} {y0:.4f} {z0:.4f})
    ({x1:.4f} {y0:.4f} {z0:.4f})
    ({x1:.4f} {y1:.4f} {z0:.4f})
    ({x0:.4f} {y1:.4f} {z0:.4f})
    ({x0:.4f} {y0:.4f} {z1:.4f})
    ({x1:.4f} {y0:.4f} {z1:.4f})
    ({x1:.4f} {y1:.4f} {z1:.4f})
    ({x0:.4f} {y1:.4f} {z1:.4f})
);

blocks
(
    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1)
);

edges
(
);

boundary
(
    inlet   {{ type patch; faces ( (0 4 5 1) ); }}
    outlet  {{ type patch; faces ( (3 2 6 7) ); }}
    ground  {{ type wall;  faces ( (0 1 2 3) ); }}
    sides   {{ type patch; faces ( (0 3 7 4) (1 5 6 2) ); }}
    top     {{ type patch; faces ( (4 7 6 5) ); }}
);

mergePatchPairs
(
);
"""
    (case_dir / "system" / "blockMeshDict").write_text(dict_text)


def write_snappyHexMeshDict(case_dir: Path) -> None:
    cx, cy, cz = 0.0, 0.0, 150.0
    dict_text = f"""/*--------------------------------*- C++ -*----------------------------------*\\
| =========                 |                                                 |
| \\\\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox           |
|  \\\\    /   O peration     | sptl-take-home stub                             |
|   \\\\  /    A nd           |                                                 |
|    \\\\/     M anipulation  |                                                 |
\\*---------------------------------------------------------------------------*/
FoamFile
{{
    version     2.0;
    format      ascii;
    class       dictionary;
    location    "system";
    object      snappyHexMeshDict;
}}
// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //

castellatedMesh true;
snap            true;
addLayers       false;

geometry
{{
    buildings.stl
    {{
        type triSurfaceMesh;
        name buildings;
    }}
    house.stl
    {{
        type triSurfaceMesh;
        name house;
    }}
}}

castellatedMeshControls
{{
    maxLocalCells       2000000;
    maxGlobalCells      8000000;
    minRefinementCells  10;
    maxLoadUnbalance    0.10;
    nCellsBetweenLevels 3;

    features
    (
        {{
            file "buildings.eMesh";
            level 1;
        }}
        {{
            file "house.eMesh";
            level 3;
        }}
    );

    refinementSurfaces
    {{
        buildings
        {{
            level (1 2);
            patchInfo {{ type wall; }}
        }}
        house
        {{
            level (3 4);
            patchInfo {{ type wall; }}
        }}
    }}

    resolveFeatureAngle 30;
    refinementRegions
    {{
    }}
    locationInMesh      ({cx:.4f} {cy:.4f} {cz:.4f});
    allowFreeStandingZoneFaces true;
}}

snapControls
{{
    nSmoothPatch        3;
    tolerance           2.0;
    nSolveIter          30;
    nRelaxIter          5;
    nFeatureSnapIter    10;
    implicitFeatureSnap false;
    explicitFeatureSnap true;
    multiRegionFeatureSnap false;
}}

addLayersControls
{{
    relativeSizes       true;
    layers {{}}
    expansionRatio      1.2;
    finalLayerThickness 0.3;
    minThickness        0.1;
    nGrow               0;
    featureAngle        60;
    nRelaxIter          3;
    nSmoothSurfaceNormals 1;
    nSmoothNormals      3;
    nSmoothThickness    10;
    maxFaceThicknessRatio 0.5;
    maxThicknessToMedialRatio 0.3;
    minMedialAxisAngle  90;
    nBufferCellsNoExtrude 0;
    nLayerIter          50;
}}

meshQualityControls
{{
    maxNonOrtho         50;
    maxBoundarySkewness 10;
    maxInternalSkewness 3;
    maxConcave          80;
    minVol              1e-13;
    minTetQuality       1e-15;
    minArea             -1;
    minTwist            0.05;
    minDeterminant      0.001;
    minFaceWeight       0.05;
    minVolRatio         0.01;
    minTriangleTwist    -1;
    nSmoothScale        4;
    errorReduction      0.75;
    relaxed {{ maxNonOrtho 75; }}
}}

debug 0;
mergeTolerance 1e-6;
"""
    (case_dir / "system" / "snappyHexMeshDict").write_text(dict_text)


def rewrite_wind_field(case_dir: Path, speed: float, deg: float) -> None:
    U_path = case_dir / "0" / "U"
    text = U_path.read_text()
    vec = f"(0 {speed:.4f} 0)"
    text = text.replace("uniform (0 0 0)", f"uniform {vec}")
    text = text.replace(
        "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //",
        "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //\n"
        f"// wind_deg_input = {deg} (source compass bearing; geometry pre-rotated so wind → +Y)",
        1,
    )
    U_path.write_text(text)


def rewrite_turbulence_fields(case_dir: Path, speed: float) -> None:
    k, eps = turbulence_inlets(speed)
    k_path = case_dir / "0" / "k"
    k_path.write_text(k_path.read_text().replace("kInlet          0.375;", f"kInlet          {k:.6f};"))
    eps_path = case_dir / "0" / "epsilon"
    eps_path.write_text(
        eps_path.read_text().replace("epsilonInlet    0.00787;", f"epsilonInlet    {eps:.6f};")
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--address", required=True, type=str)
    args = ap.parse_args(argv)

    try:
        lat, lon = geocode(args.address)
    except (RuntimeError, OSError) as exc:
        print(f"geocoding failed for {args.address!r}: {exc}", file=sys.stderr)
        return 4

    wind_speed, wind_deg = wind_conditions(lat, lon)

    ept_url = find_ept_url(lat, lon)
    if ept_url is None:
        print(f"no USGS 3DEP LiDAR coverage at ({lat}, {lon})", file=sys.stderr)
        return 2

    bbox = latlon_to_bbox(lat, lon, RADIUS_M)

    copy_case_template(args.out)
    stl_path = args.out / "constant" / "triSurface" / "buildings.stl"

    try:
        n_points = run_pdal_pipeline(ept_url, bbox, stl_path, lat, lon, wind_deg)
    except RuntimeError as exc:
        print(f"PDAL pipeline failed: {exc}", file=sys.stderr)
        return 1

    if n_points < MIN_BUILDING_POINTS:
        print(
            f"insufficient building points at ({lat}, {lon}) — "
            f"got {n_points}, need {MIN_BUILDING_POINTS}",
            file=sys.stderr,
        )
        return 3

    write_blockMeshDict(args.out, bbox, CELL_SIZE_M)
    write_snappyHexMeshDict(args.out)
    rewrite_wind_field(args.out, wind_speed, wind_deg)
    rewrite_turbulence_fields(args.out, wind_speed)

    print(f"wrote case to {args.out} ({n_points} building points)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
