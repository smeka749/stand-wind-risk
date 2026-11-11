from __future__ import annotations

import hashlib
import math

GRID_M = 100.0


def target_geometry(lat: float, lon: float):
    import numpy as np
    import trimesh

    lat_grid = round(lat * 111_320.0 / GRID_M)
    lon_grid = round(lon * 111_320.0 * math.cos(math.radians(lat)) / GRID_M)
    seed = int(hashlib.sha256(f"{lat_grid},{lon_grid}".encode()).hexdigest(), 16) % 10

    def _box(w, l, h, offset=(0.0, 0.0, 0.0)):
        m = trimesh.creation.box(extents=(w, l, h))
        m.apply_translation((offset[0], offset[1], h / 2 + offset[2]))
        return m

    def _gable_roof(w, l, ridge_h, base_z):
        verts = np.array([
            [-w / 2, -l / 2, base_z], [w / 2, -l / 2, base_z], [0, -l / 2, base_z + ridge_h],
            [-w / 2,  l / 2, base_z], [w / 2,  l / 2, base_z], [0,  l / 2, base_z + ridge_h],
        ])
        faces = np.array([
            [0, 1, 2], [3, 5, 4],
            [0, 3, 4], [0, 4, 1],
            [1, 4, 5], [1, 5, 2],
            [2, 5, 3], [2, 3, 0],
        ])
        return trimesh.Trimesh(vertices=verts, faces=faces, process=True)

    shapes = {
        0: lambda: _box(22, 18, 14),
        1: lambda: _box(20, 18, 20),
        2: lambda: _box(26, 22, 10),
        3: lambda: _box(14, 14, 30),
        4: lambda: _box(30, 16, 14),
        5: lambda: trimesh.util.concatenate([_box(22, 14, 16), _box(14, 22, 16, offset=(4, 4, 0))]),
        6: lambda: trimesh.util.concatenate([_box(22, 16, 12), _gable_roof(22, 16, 8, 12)]),
        7: lambda: _box(20, 20, 20),
        8: lambda: trimesh.util.concatenate([_box(16, 16, 20, offset=(-8, 0, 0)), _box(16, 16, 14, offset=(8, 0, 0))]),
        9: lambda: trimesh.util.concatenate([_box(26, 14, 14), _box(12, 22, 14, offset=(0, 8, 0))]),
    }
    return shapes[seed]()
