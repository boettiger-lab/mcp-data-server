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


# H3 average hexagon edge length (km) by resolution, res 0-15. For a regular
# hexagon the circumradius (center-to-vertex) equals the edge length, so this
# doubles as the max center-to-boundary distance of a cell at each resolution.
# Source: https://h3geo.org/docs/core-library/restable/
_H3_EDGE_KM = [
    1107.712591, 418.6760055, 158.2446558, 59.81085794, 22.6063794,
    8.544408276, 3.229482772, 1.220629759, 0.461354684, 0.174375668,
    0.065907807, 0.024910561, 0.009415526, 0.003559893, 0.001348575,
    0.000509713,
]

_EARTH_KM_PER_DEG = 111.195


def h3_edge_padding_deg(res: int, lat: float) -> Tuple[float, float]:
    """Return (pad_lat, pad_lng) in degrees ≈ one hex circumradius at `res`.

    Used to widen a tile's cell-selection window so a hex whose *center* sits
    just outside the tile bbox — but whose body overlaps it — is still emitted
    into this tile. That is the seam fix (#188): center-in-tile selection drew
    each boundary-crossing hex in exactly one tile, leaving the overhang in the
    neighbor undrawn. Longitude degrees shrink toward the poles, so pad_lng is
    scaled by 1/cos(lat); cos is floored to keep it finite near the poles.
    """
    res = max(0, min(15, res))
    radius_km = _H3_EDGE_KM[res]
    pad_lat = radius_km / _EARTH_KM_PER_DEG
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    pad_lng = pad_lat / cos_lat
    return (pad_lat, pad_lng)


# The neutral zoom_offset: at this value the adaptive mapping applies no bias.
# Smaller offsets nudge one H3 res finer per step, larger ones coarser — the
# same direction the legacy linear mapping used (res = z - zoom_offset).
_BASE_ZOOM_OFFSET = 2

# Target hexes per *tile*. A screen shows ~4-8 tiles, so a few thousand per tile
# is ~10-30k hexes per view — dense enough to read as a continuous field, small
# enough to keep the .pbf and client parse fast. Tunable; see #188.
_TARGET_CELLS_PER_TILE = 4000

# H3 is aperture-7: each resolution has ~7x the cells of its parent. Rolling a
# contiguous region up one level multiplies the cell count by ~1/7.
_H3_APERTURE = 7.0


def _merc_y_norm(lat: float) -> float:
    """Latitude (deg) -> web-mercator Y normalized to [0,1] (0=north, 1=south).

    Clamped to the Web Mercator latitude limit (~85.0511 deg) so the log stays
    finite at the poles.
    """
    lat = max(-85.0511, min(85.0511, lat))
    siny = math.sin(math.radians(lat))
    return 0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)


def tiles_covering_bounds(z: int, bounds) -> int:
    """Count XYZ tiles at zoom z that the data's [w, s, e, n] bbox spans (>=1).

    Used by the adaptive zoom->res mapping to estimate how thinly the data's
    cells spread across tiles at a given zoom. A wide global extent covers many
    tiles (so each holds few cells -> finer res is affordable); a small bounded
    extent covers few tiles (so each holds many cells -> coarser res needed).
    """
    w, s, e, n = bounds
    nt = 2 ** z

    def _clamp_idx(v):
        # An edge falling exactly on the far boundary (lng 180, merc-y 1.0)
        # maps to index nt, which is past the last tile (nt-1).
        return min(nt - 1, max(0, math.floor(v)))

    x0 = _clamp_idx((w + 180.0) / 360.0 * nt)
    x1 = _clamp_idx((e + 180.0) / 360.0 * nt)
    y0 = _clamp_idx(_merc_y_norm(n) * nt)  # north edge -> smaller tile-y
    y1 = _clamp_idx(_merc_y_norm(s) * nt)
    cols = max(1, x1 - x0 + 1)
    rows = max(1, y1 - y0 + 1)
    return cols * rows


def zoom_to_h3_res(
    z: int,
    min_res: int,
    finest_res: int,
    zoom_offset: int = 2,
    feature_count_finest: int | None = None,
    bounds=None,
    target_cells_per_tile: int = _TARGET_CELLS_PER_TILE,
) -> int:
    """Map a map-zoom z to the H3 resolution to serve, clamped to [min_res, finest_res].

    Two modes:

    Adaptive (when feature_count_finest and bounds are both given) — pick the
    finest res whose estimated cells-per-tile stays under target_cells_per_tile.
    A region of N cells at finest_res has ~N * 7^(r-finest_res) cells at res r;
    spread across tiles_covering_bounds(z) tiles, that's the per-tile load. This
    resolves the bounded-vs-global tension a single linear offset can't (#188):
    CA-sized data covers few tiles so it can afford finer res at mid-zoom, while
    global data at the same zoom is held coarser to stay within the per-tile
    budget. zoom_offset still nudges the result one res per step off _BASE
    (smaller=finer), preserving the knob's legacy direction.

    Legacy (no count/bounds — older metadata) — the linear res = z - zoom_offset.
    zoom_offset=2 maps z -> z-2; smaller offsets render finer hexes (more
    features/tile). offset=-1 (the old default) produced 40k-130k hexes per tile
    at CA-scale zooms (#178).
    """
    if feature_count_finest and bounds is not None and feature_count_finest > 0:
        n_tiles = tiles_covering_bounds(z, bounds)
        # Solve target >= count_finest * 7^(r-finest) / n_tiles for the largest r:
        #   r <= finest + log7(target * n_tiles / count_finest)
        ratio = target_cells_per_tile * n_tiles / feature_count_finest
        delta = math.log(ratio) / math.log(_H3_APERTURE)
        bias = _BASE_ZOOM_OFFSET - zoom_offset  # smaller offset -> finer
        target = int(math.floor(finest_res + delta + 0.5)) + bias
    else:
        target = z - zoom_offset
    return max(min_res, min(finest_res, target))


# Bump when the pyramid on-disk layout changes in a way that the tile endpoint
# can't read with the old logic — forces a fresh registration directory so old
# parquet files written under a previous layout are never served by new code.
_LAYOUT_VERSION = "v3-iterative"


def content_hash(sql: str, finest_res: int, min_res: int, agg: str, zoom_offset: int) -> str:
    """Deterministic 16-char hex hash of the registration inputs.

    Used as the <name> component of tile URLs so identical registrations
    dedupe naturally and URLs are CDN-friendly.
    """
    canonical = f"{_LAYOUT_VERSION}\0{sql}\0{finest_res}\0{min_res}\0{agg}\0{zoom_offset}"
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
