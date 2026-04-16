# Dynamic MVT tile endpoint for H3 hex visualization at scale

**Date:** 2026-04-15
**Status:** Draft design, pending implementation
**Tracking issue:** boettiger-lab/mcp-data-server#4
**Related:** boettiger-lab/geo-agent#159 (UC1 counterpart), boettiger-lab/mcp-data-server#54 (Docker pre-install)

## Problem

Users want to visualize per-hex computations as native MapLibre layers — "show a biodiversity + carbon composite at H3 r8 across California" — where results are computed per-session in DuckDB and can reach millions of hex cells. Current mechanisms don't cover this:

- The MCP `query` tool returns markdown tables capped at 50 rows. Not a map layer.
- The S3 side-channel (`s3://public-output/`, documented in `h3-guide.md`) works for UC1 (attribute join onto existing polygons via client-side `setFeatureState`, tracked in boettiger-lab/geo-agent#159). It does not scale to rendering hex **geometry** — at CONUS/r8 (~10M cells) a single GeoJSON or GeoParquet dump is too large to hand a browser.
- Pre-baking global hex layers as PMTiles was tried and failed at global H8 (~691M cells) without chunking infrastructure.
- Client-side h3-js boundary generation covers small viewports but not interactive pan/zoom over large extents at interactive framerates.

## Goal

A single endpoint on the MCP server that serves MVT tiles for a user-registered hex query, producing a MapLibre vector source consumable by any MapLibre-compatible client (geo-agent, QGIS, kepler.gl, Felt). Unbounded scale through tiling, stateless at the HTTP layer, horizontally scalable.

**Non-goals for v1:**
- Non-hex geometry sources (arbitrary polygons, lines, points).
- Private-data tiles (requires a signed-request sidecar — see §Deferrals).
- CDN fronting (not needed for initial adoption; tile URLs are content-addressable and cache-friendly when added later).
- Authentication on the tile endpoint.

## Architecture

### URL scheme

```
GET /tiles/<namespace>/<name>/{z}/{x}/{y}.pbf
```

- `namespace` distinguishes source types. v1 ships `hex`.
- `name` is a content hash (SHA-1, truncated) of the registering inputs — the tile URL is deterministic from its inputs, so repeat registrations are natural no-ops and tiles are CDN-friendly when fronted later.
- `{z}/{x}/{y}` is standard XYZ web-mercator.

Response: `application/vnd.mapbox-vector-tile`, body is raw MVT bytes from DuckDB's `ST_AsMVT`.

### S3 as state store

The server remains stateless at the HTTP layer — no per-connection state, no coordination between replicas. All "state" lives in S3 as a content-addressable parquet pyramid:

```
s3://public-output/hex/<hash>/res=2/data_0.parquet
s3://public-output/hex/<hash>/res=3/data_0.parquet
...
s3://public-output/hex/<hash>/res=9/data_0.parquet
```

A tile request at `(z, x, y)` reads only the parquet partition for the appropriate resolution. The 30-day TTL on `public-output` handles cleanup; repeat registrations with identical inputs produce identical hashes and thus overwrite in place (`OVERWRITE_OR_IGNORE`).

This preserves the current statelessness property: any replica can serve any tile request, routing is round-robin, no sticky sessions required. Multi-replica deployments (current: 2 pods via HAProxy) need no coordination.

### LOD strategy — pre-aggregated resolution pyramid

Each `register_hex_tiles` call materializes a pyramid from the user's finest resolution down to a configurable minimum resolution (default r2). The pyramid is generated once at registration time via a single `COPY ... TO` with partitioning:

```sql
COPY (
  WITH src AS (<user sql>)
  SELECT h3_cell_to_parent(h_finest, 2) AS h, AVG(v1) AS v1, ..., 2 AS res FROM src GROUP BY 1
  UNION ALL
  SELECT h3_cell_to_parent(h_finest, 3) AS h, AVG(v1) AS v1, ..., 3 AS res FROM src GROUP BY 1
  -- ...
  UNION ALL
  SELECT h_finest AS h, v1, ..., <finest_res> AS res FROM src
) TO 's3://public-output/hex/<hash>/' (FORMAT PARQUET, PARTITION_BY (res), OVERWRITE_OR_IGNORE);
```

Trade-off: ~17% storage overhead vs. the finest level alone (sum of H3 cell counts at coarser resolutions is geometric in 1/7). Bounded per-tile read cost (one partition file, no cross-resolution aggregation at tile time). Explicit aggregation semantics — the user opts into the aggregation function at registration.

**Zoom → resolution mapping:**
```python
target_res = clamp(zoom - 4, min_res, finest_res)
```
Roughly matches a hex at the chosen resolution to a few screen pixels at typical zoom levels. Tunable via the tool's optional `zoom_offset` param (default 4).

### Tile generation at request time

Per request, the server:

1. Parses `(namespace, name, z, x, y)` from the URL.
2. Computes `target_res` from `z`.
3. Queries the pyramid partition for `res=target_res`:
   ```sql
   SELECT h3_cell_to_boundary_wkb(h) AS geom, v1, v2, ...
   FROM read_parquet('s3://public-output/hex/<hash>/res=<target_res>/*.parquet')
   WHERE h3_cell_to_parent(h, <tile_res>) IN (<cells_covering_tile>)
   ```
   Cell-covering is computed via `h3_polygon_wkt_to_cells` on the tile's web-mercator bounds.
4. Wraps the result in `ST_AsMVT(ST_AsMVTGeom(geom, tile_bounds))`.
5. Returns the bytes.

### Connection model

For the tile endpoint specifically (not the existing `query` tool, which keeps its fresh-per-request `:memory:` DB for credential isolation):

- **One persistent `:memory:` DuckDB connection per worker**, created at startup with extensions loaded. Tile requests acquire `con.cursor()` for isolation, execute via `anyio.to_thread.run_sync` to keep the event loop unblocked.
- Extensions (`httpfs`, `h3`, `spatial`) are already installed at image build time per boettiger-lab/mcp-data-server#54, so startup loads are milliseconds.
- No credentials are injected into the persistent connection — it only reads from public buckets (`public-output`). Private data for tiles is a v2 concern.

This is process-local cache, not cross-request server state — compatible with horizontal scaling. Each of the 2 k8s replicas maintains its own persistent connection; HAProxy round-robin routing works unchanged.

## MCP tool API

### `register_hex_tiles`

```
register_hex_tiles(
    sql: str,                    # SELECT returning (h3_index, value1, value2, ...)
    finest_res: int,             # resolution of the h3_index column (e.g. 8)
    min_res: int = 2,            # minimum pyramid resolution
    agg: str = "AVG",            # aggregation function applied to all value columns
    zoom_offset: int = 4,        # z → target_res offset; target_res = clamp(z - offset, min_res, finest_res)
    s3_key: str = None,          # optional private-bucket creds for reading the source data
    s3_secret: str = None,
    s3_endpoint: str = None,
    s3_scope: str = None,
) -> dict
```

**Input contract:**
- The user's `sql` MUST return a column that is an H3 index (uint64 or hex string — whatever DuckDB's h3 extension accepts as input to `h3_cell_to_parent`) as its first column.
- Subsequent columns are numeric values to aggregate. Column names become MVT feature properties.
- `finest_res` declares the resolution of the index column. The pyramid builds parents from there; the finest level stores the user's values unaggregated.
- `agg` applies uniformly to all value columns. Mixed aggregations (mean for one column, sum for another) are a v2 concern; users can register twice.
- **Private-source caveat:** if the user registers a SQL query that reads private data, the resulting pyramid still lands in `public-output` (a public bucket). This matches the existing `query` + `COPY TO 's3://public-output/...'` pattern — it's a deliberate user action, not accidental leakage. True private-data tile serving (keep the pyramid private, authenticate tile requests) is the v2 rclone-sidecar work.

**Return:**
```json
{
  "tile_url_template": "https://duckdb-mcp.nrp-nautilus.io/tiles/hex/<hash>/{z}/{x}/{y}.pbf",
  "hash": "<hash>",
  "bounds": [minlon, minlat, maxlon, maxlat],
  "finest_res": 8,
  "min_res": 2,
  "value_columns": ["v1", "v2"],
  "feature_count_finest": 573421
}
```

The LLM only sees this summary — tile bytes never transit LLM context.

### Client usage pattern (geo-agent)

A client-side tool (`render_h3_tiles`, sibling of `filter_by_query` and the forthcoming `style_by_query`) takes the `tile_url_template` and adds it as a MapLibre vector source:

```js
map.addSource(sourceId, {
  type: 'vector',
  tiles: [tile_url_template],
  minzoom: 0,
  maxzoom: 14,
});
map.addLayer({
  id: layerId,
  type: 'fill',
  source: sourceId,
  'source-layer': 'hex',
  paint: {
    'fill-color': ['interpolate', ['linear'], ['get', 'v1'], vmin, c0, vmax, c1],
    'fill-opacity': 0.7,
  },
});
```

Dispatch hint in `assistant-role.md`: use `render_h3_tiles` when the result set would exceed the `style_by_query` / markdown-table scale (>100k features, or when the user asks to visualize raw hexes rather than color an existing polygon layer).

## Client-agnosticism

The tile URL is a standard XYZ MVT endpoint. Any MapLibre-compatible client consumes it identically — QGIS (`Add Vector Tile Layer`), kepler.gl, Felt, custom MapLibre apps. The `register_hex_tiles` MCP tool is the only geo-agent-specific coupling, and it returns enough metadata (`bounds`, `value_columns`, value ranges via a follow-up query if needed) for any client to build styling.

## Deferrals to v2

Tracked but explicitly out of scope for the first implementation:

- **Private-data tiles.** Reading private S3 buckets for the tile-query pyramid requires an rclone-sidecar pattern matching `wyoming/k8s/deployment.yaml` — a colocated rclone container exposing private S3 as authenticated HTTP, with signed-URL generation for the tile endpoint. Register-time read of private source can still use the existing `s3_key`/`s3_secret` flow; the issue is specifically serving tiles that reference private paths.
- **CDN fronting.** Content-addressable URLs are already cache-friendly. Add a CDN when traffic justifies it. The current 30-day S3 TTL bounds the cache-invalidation problem.
- **HAProxy consistent-hash routing by tile hash.** Round-robin is fine — each worker's persistent connection caches DuckDB-level metadata, not per-tileset state, so any worker can serve any tile.
- **Multi-worker per pod.** Currently one worker per replica. Uvicorn `--workers N` is a config change when CPU per replica saturates.
- **Mixed aggregations per tileset.** Register twice; v2 could accept `agg_per_column`.
- **Non-hex geometry sources** (arbitrary polygons, lines, points). v1 scope is H3 hexes. Generalization is a separate design once we see the shape of real usage.
- **Auth on the tile endpoint.** Public by construction in v1 (data is in `public-output`).

## Testing strategy

- **Unit:** `h3-cells-covering-tile` logic against known z/x/y → cell-set fixtures.
- **Integration:** register a small California r8 tileset, pull `{z=6,7,8,9}/{x,y}` samples, assert MVT bytes decode to expected feature counts per resolution.
- **Scale smoke test:** register CONUS r8 (~10M cells), measure registration time (single `COPY`), tile latency at representative zooms, S3 bytes-read per tile.
- **Multi-replica:** verify tile responses are identical across both k8s replicas for the same URL.

## Schema contract for pyramid parquet files

```
s3://public-output/hex/<hash>/res=<N>/data_0.parquet
```

Columns:
- `h` — H3 index at resolution `N` (matches the partition value).
- `<value_col_1>`, `<value_col_2>`, ... — the user's value columns, aggregated by `agg` for `N < finest_res`, raw for `N == finest_res`.

Partition column: `res` (integer).

Readers (the tile endpoint, future clients) can consume the pyramid directly without the tile server if they want — GeoParquet conversion is a follow-up enhancement.

## Open items to resolve during implementation

- Exact `anyio.to_thread.run_sync` pattern for DuckDB cursor use — confirm cursor is thread-safe for our access pattern.
- Error taxonomy: invalid hash, resolution out of range, empty tile — map to 404 vs. 204 vs. 200-empty-MVT. Convention is 204 for empty tiles so MapLibre treats as "nothing here" rather than "broken source."
- Tile-covering at very coarse zooms (z < 3) — the whole pyramid may lie in one tile; short-circuit the `h3_polygon_wkt_to_cells` call.
