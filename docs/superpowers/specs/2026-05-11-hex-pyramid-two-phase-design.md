# Hex pyramid build: two-phase iterative construction

**Date:** 2026-05-11
**Status:** Draft design, pending implementation
**Tracking issue:** boettiger-lab/mcp-data-server (TBD — file alongside the implementation PR)
**Builds on:** #131 (h0 partitioning) and #132 (h0 computed once in src CTE)

## Problem

`build_pyramid_sql` currently emits one `COPY` whose body is a 7-branch `UNION ALL` from a CTE that materializes the entire user-supplied source query. For global high-cardinality datasets (GBIF: billions of points → ~50M h8 cells, irrecoverable carbon: ~126M h8 cells), DuckDB must hold simultaneously:

- The materialized `src` CTE.
- Seven parallel `HASH_GROUP_BY` aggregations (one per resolution).
- The per-(res, h0) hash bucketing that `PARTITION_BY (res, h0)` performs before writing partition files.

This combined working set OOMs at GBIF scale and was already pushing memory pressure on the irrecoverable carbon dataset.

The source data and the output are both h0-partitioned. The build path should leverage that and never need the whole globe in flight at once.

## Solution

Replace the one-shot `COPY` with two phases, each a smaller `COPY` whose memory footprint is bounded by its output cardinality rather than the source.

### Phase 1 — finest from source

One `COPY` reads `user_sql`, aggregates by `(h, h0)`, and writes only the finest resolution.

```sql
WITH src AS (
  SELECT *, CAST(h3_cell_to_parent({qh}, 0) AS BIGINT) AS h0
  FROM ({user_sql})
)
COPY (
  SELECT {qh} AS h,
         h0,
         {finest_value_exprs},
         {finest_res} AS res
  FROM src
  GROUP BY 1, 2
) TO '{output_uri}' (FORMAT PARQUET, PARTITION_BY (res, h0), OVERWRITE_OR_IGNORE)
```

`{finest_value_exprs}` depends on `agg`:

| Agg | Expression(s) at finest |
|---|---|
| COUNT | `COUNT(*) AS count` |
| SUM | `SUM("v") AS "v"` per value column |
| MIN | `MIN("v") AS "v"` per value column |
| MAX | `MAX("v") AS "v"` per value column |
| AVG | `AVG("v1") AS "v1", AVG("v2") AS "v2", …, COUNT(*) AS count` (single shared count for the parent rollup) |

### Phase 2 — iterative parents

For each `res` from `finest_res - 1` down to `min_res`, a separate `COPY` reads from the previously written resolution and writes the next coarser level.

```sql
COPY (
  SELECT h3_cell_to_parent(h, {res}) AS h,
         h0,
         {parent_value_exprs},
         {res} AS res
  FROM read_parquet('{output_uri}res={res+1}/**/*.parquet', hive_partitioning=true)
  GROUP BY 1, 2
) TO '{output_uri}' (FORMAT PARQUET, PARTITION_BY (res, h0), OVERWRITE_OR_IGNORE)
```

`{parent_value_exprs}` rolls up the previous level:

| Agg | Expression(s) at parent |
|---|---|
| COUNT | `SUM(count) AS count` |
| SUM | `SUM("v") AS "v"` |
| MIN | `MIN("v") AS "v"` |
| MAX | `MAX("v") AS "v"` |
| AVG | `SUM("v1" * count) / SUM(count) AS "v1", …, SUM(count) AS count` (single shared count, weighted per value column) |

Each Phase 2 step's working set is bounded by the cardinality of the *previous* resolution, which shrinks by a factor of ~7 per step. The expensive scan is Phase 1; every Phase 2 step thereafter is small.

## Output schema per resolution

| Agg mode | Columns |
|---|---|
| COUNT | `h, h0, count` |
| SUM / MIN / MAX | `h, h0, <value_col>` (one per user value column) |
| AVG | `h, h0, <value_col>, count` |

`h0` and `res` only live in the hive path (`res=N/h0=X/data.parquet`) — DuckDB's `PARTITION_BY (res, h0)` strips them from file content, and `hive_partitioning=true` re-adds them on read.

The `count` column for AVG mode is an internal build aid. It is not reported in `value_stats`. The tile endpoint sees the schema it always saw (`value_columns` from registration metadata).

## Behavior change at the finest level

The current code passes raw source rows through at the finest level for non-COUNT modes:

```python
# Current (non-COUNT):
finest_values = ", ".join(f'"{c}"' for c in value_columns)
selects.append(
    f"  SELECT {qh} AS h, h0, {finest_values}, {finest_res} AS res FROM src"
)  # no GROUP BY
```

If `user_sql` returned multiple rows per `(h, h0)`, the tile endpoint rendered each as a separate MVT feature — producing overlapping hexagons. The new design always aggregates at the finest level by `(h, h0)`, so each cell renders as exactly one feature in every tile, in every mode.

Callers that were intentionally relying on raw passthrough (none observed in production) would see one feature per cell instead.

Knock-on effect on registration metadata: `feature_count_finest` is currently `COUNT(*)` from the finest-resolution parquet. Under the old layout that was the number of *source rows* for non-COUNT modes; under the new layout it's the number of unique `(h, h0)` cells at finest in every mode. This is the value the agent actually wanted — the previous semantics were a side effect of the raw-passthrough quirk.

## Layout version

Bump `_LAYOUT_VERSION` in `tiles/tile_math.py` from `"v2-h0"` to `"v3-iterative"`. Old pyramids written under the single-COPY layout retain their hashes and parquet on S3; new registrations get fresh content addresses and write to fresh directories.

The pin in `test_h0_partition_layout_does_not_collide_with_pre_h0_hashes` should be extended (or a new test added) to also assert the new hash differs from a known `v2-h0` hash.

## Atomicity and resumability

`metadata.json` remains the single success marker, written only after both phases complete. The cache short-circuit (read `metadata.json`, return its contents) is unchanged.

If a build is interrupted mid-flight, `metadata.json` is absent and the next call falls through to rebuild from scratch. `OVERWRITE_OR_IGNORE` on each phase tolerates pre-existing partial files. True per-h0 resumability (skipping h0 partitions already written) is out of scope for this iteration.

## Implementation footprint

Files changed:

- `tiles/pyramid.py` — `build_pyramid_sql` returns a list of statements instead of a single string; `register_hex_tiles` executes them sequentially with the existing `con.sql(...)` loop. The post-build stats query (per-resolution min/max) keeps its current shape since the output paths and column names haven't changed for non-AVG modes.
- `tiles/tile_math.py` — `_LAYOUT_VERSION` bump.
- `tests/test_tile_pyramid.py` — assert Phase 1 SQL has finest-only `GROUP BY 1, 2`, Phase 2 SQLs reference `read_parquet('.../res={N+1}/**/*.parquet')` and the correct per-agg parent expression. Existing `test_finest_level_is_ungrouped` for AVG mode flips to `test_finest_level_is_grouped_in_all_modes`.
- `tests/test_tile_math.py` — new collision-check test for `v3-iterative` vs `v2-h0`.

Files unchanged:

- `tiles/endpoint.py` — tile-serving SQL is unaffected; it still reads `res=N/**/*.parquet` and joins to `bbox_h0`.
- `server.py` — `register_hex_tiles` public docstring and signature unchanged.

## Testing

- **Unit:** Phase 1 SQL shape per agg mode (COUNT, SUM, MIN, MAX, AVG). Phase 2 SQL shape per agg mode, including the weighted-average expression for AVG.
- **End-to-end correctness:** register a multi-h0 synthetic dataset (the existing `test_writes_h0_subpartitions` fixture suffices). Assert mathematical invariants directly rather than diffing against the old output:
  - COUNT: `SUM(parent.count) == COUNT(*)` from source for each h0.
  - SUM: `SUM(parent.v) == SUM(child.v)` between adjacent resolutions.
  - MIN/MAX: parent `MIN` equals `MIN` of children's `MIN`; parent `MAX` equals `MAX` of children's `MAX`.
  - AVG: parent `v` equals the source-rows weighted mean within that parent cell, computed independently.
- **No memory benchmark required for unit tests.** The OOM evidence will come from a real GBIF rebuild in dev after deploy.

## Out of scope

- Per-h0 outer loop at Phase 1. If GBIF still OOMs after this change, revisit by chunking Phase 1 by h0 — but the expected output cardinality of Phase 1 (~50M cells for GBIF) is bounded enough that a single streaming `COPY` should fit.
- Resumable builds (detect and skip already-written h0 partitions on restart).
- Any change to the tile endpoint or the `register_hex_tiles` public API.
- Geo-agent client timeout (boettiger-lab/geo-agent#205, already open).
