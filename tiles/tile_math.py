"""Pure helper functions for tile math. No side effects, no DuckDB."""
import math
from typing import Tuple


def tile_xyz_to_lnglat_bounds(z: int, x: int, y: int) -> Tuple[float, float, float, float]:
    """Return (west, south, east, north) in lng/lat (EPSG:4326) for XYZ tile."""
    n = 2.0 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (west, lat_s, east, lat_n)
