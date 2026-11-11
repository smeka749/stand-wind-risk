from __future__ import annotations

import hashlib
import math

TILE_M = 1000.0
SPEED_MIN = 15.0
SPEED_MAX = 35.0


def wind_conditions(lat: float, lon: float) -> tuple[float, float]:
    lat_tile = round(lat * 111_320.0 / TILE_M)
    lon_tile = round(lon * 111_320.0 * math.cos(math.radians(lat)) / TILE_M)
    seed = hashlib.sha256(f"{lat_tile},{lon_tile}|wind".encode()).digest()
    deg = (int.from_bytes(seed[:4], "big") / 2**32) * 360.0
    speed = SPEED_MIN + (int.from_bytes(seed[4:8], "big") / 2**32) * (SPEED_MAX - SPEED_MIN)
    return round(speed, 1), round(deg, 1)
