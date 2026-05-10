"""Pure helper functions for tile math. No side effects, no DuckDB."""
import hashlib
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


def zoom_to_h3_res(z: int, min_res: int, finest_res: int, zoom_offset: int = 1) -> int:
    """Clamp(z - zoom_offset, min_res, finest_res) — coarser hexes at lower zooms."""
    target = z - zoom_offset
    return max(min_res, min(finest_res, target))


def content_hash(sql: str, finest_res: int, min_res: int, agg: str, zoom_offset: int) -> str:
    """Deterministic 16-char hex hash of the registration inputs.

    Used as the <name> component of tile URLs so identical registrations
    dedupe naturally and URLs are CDN-friendly.
    """
    canonical = f"{sql}\0{finest_res}\0{min_res}\0{agg}\0{zoom_offset}"
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
