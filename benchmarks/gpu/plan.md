# H3 Join Benchmark Plan: GPU vs CPU DuckDB

## Servers

| Server | URL | Backend |
|---|---|---|
| `gpu-data-server` | `https://gpu-mcp.nrp-nautilus.io/mcp` | DuckDB + RAPIDS/cuDF GPU |
| `duckdb-geo` | `https://duckdb-mcp.nrp-nautilus.io/mcp` | DuckDB CPU-only |

Both servers expose the same three MCP tools: `list_datasets`, `get_dataset`, `query`.
Both read from the same S3 paths (`s3://public-*/...`).

---

## Datasets Used

All joins are on H3 integer index columns (equi-join on `h8` or `h0`).

| Dataset | S3 Path | Size | H3 res |
|---|---|---|---|
| IUCN combined_sr | `s3://public-iucn/hex/combined_sr/**` | 0.06 GiB | 8 |
| IUCN birds_sr | `s3://public-iucn/hex/birds_sr/**` | 0.05 GiB | 8 |
| IUCN mammals_sr | `s3://public-iucn/hex/mammals_sr/**` | 0.05 GiB | 8 |
| NCP biodiversity | `s3://public-ncp/hex/ncp_biod_nathab/**` | 0.14 GiB | 8 |
| WDPA Dec 2025 | `s3://public-wdpa/hex/**` | unknown | 8 |
| Irrecoverable Carbon 2024 | `s3://public-carbon/irrecoverable-carbon-2024/hex/**` | 9.90 GiB | 8 |
| GBIF taxonomy counts | `s3://public-gbif/taxonomy/**` | small | 0 |
| GBIF occurrences 2025-06 | `s3://public-gbif/2025-06/hex/**` | 119.74 GiB | 8 |

---

## Benchmark Queries

### Q1 — Small × Small (baseline)
```sql
SELECT a.h8, a.combined_sr, b.birds_sr
FROM read_parquet('s3://public-iucn/hex/combined_sr/**') a
JOIN read_parquet('s3://public-iucn/hex/birds_sr/**') b ON a.h8 = b.h8 AND a.h0 = b.h0
```
~0.06 GiB × 0.05 GiB. Establishes overhead baseline; GPU unlikely to win here.

---

### Q2 — Small × Small × Small (3-way join)
```sql
SELECT a.h8, a.combined_sr, b.birds_sr, c.mammals_sr
FROM read_parquet('s3://public-iucn/hex/combined_sr/**') a
JOIN read_parquet('s3://public-iucn/hex/birds_sr/**') b ON a.h8 = b.h8 AND a.h0 = b.h0
JOIN read_parquet('s3://public-iucn/hex/mammals_sr/**') c ON a.h8 = c.h8 AND a.h0 = c.h0
```
Tests multi-join planning overhead at small scale.

---

### Q3 — Medium × Small (first real GPU test)
```sql
WITH carbon AS (
  SELECT h8, h0, SUM(carbon) AS total_carbon
  FROM read_parquet('s3://public-carbon/irrecoverable-carbon-2024/hex/**')
  GROUP BY h8, h0
)
SELECT a.h8, a.total_carbon, b.combined_sr
FROM carbon a
JOIN read_parquet('s3://public-iucn/hex/combined_sr/**') b ON a.h8 = b.h8 AND a.h0 = b.h0
```
9.90 GiB × 0.06 GiB. Carbon is a raster (pre-aggregated with SUM). Hash join with large build side; GPU parallelism should help.

---

### Q4 — Medium × Medium with GROUP BY (aggregation pipeline)
```sql
WITH carbon AS (
  SELECT h8, h0, SUM(carbon) AS total_carbon
  FROM read_parquet('s3://public-carbon/irrecoverable-carbon-2024/hex/**')
  GROUP BY h8, h0
)
SELECT b.h8, b.total_carbon, COUNT(DISTINCT a.SITE_ID) AS n_protected_areas
FROM read_parquet('s3://public-wdpa/hex/**') a
JOIN carbon b ON a.h8 = b.h8 AND a.h0 = b.h0
GROUP BY b.h8, b.total_carbon
```
Carbon pre-aggregated (raster); WDPA has multiple rows per h8 (overlapping protected areas). Tests join + GROUP BY pipeline.

---

### Q5 — Medium × Multi (4-way join)
```sql
WITH carbon AS (
  SELECT h8, h0, SUM(carbon) AS total_carbon
  FROM read_parquet('s3://public-carbon/irrecoverable-carbon-2024/hex/**')
  GROUP BY h8, h0
)
SELECT a.h8, a.total_carbon, b.combined_sr, c.birds_sr, d.RPL_THEMES
FROM carbon a
JOIN read_parquet('s3://public-iucn/hex/combined_sr/**') b ON a.h8 = b.h8 AND a.h0 = b.h0
JOIN read_parquet('s3://public-iucn/hex/birds_sr/**') c ON a.h8 = c.h8 AND a.h0 = c.h0
JOIN read_parquet('s3://public-social-vulnerability/svi-2022-tract/hex/h0=*/data_0.parquet') d ON a.h8 = d.h8 AND a.h0 = d.h0
```
4-way join across carbon (pre-aggregated), biodiversity, and social vulnerability data.

---

### Q6 — Large × Small with aggregation (GPU memory pressure)
```sql
SELECT a.h8, COUNT(*) AS gbif_obs, b.combined_sr
FROM read_parquet('s3://public-gbif/2025-06/hex/**') a
JOIN read_parquet('s3://public-iucn/hex/combined_sr/**') b ON a.h8 = b.h8 AND a.h0 = b.h0
GROUP BY a.h8, b.combined_sr
```
119.74 GiB fact table. Primary GPU stress test; may require chunked execution or hit GPU memory limits.

---

### Q7 — Coarse resolution join (h0 cross-scale)
```sql
SELECT CAST(a.h0 AS BIGINT) AS h0, SUM(a.n) AS gbif_count, AVG(b.combined_sr) AS mean_richness
FROM read_parquet('s3://public-gbif/taxonomy/**') a
JOIN read_parquet('s3://public-iucn/hex/combined_sr/**') b
  ON CAST(a.h0 AS BIGINT) = b.h0
GROUP BY CAST(a.h0 AS BIGINT)
```
Joins at H3 resolution 0 (coarse). h0-only join so single key is correct here; no h8 to include.

---

## Setup SQL (both servers)

```sql
SET s3_allow_recursive_globbing=false;   -- avoid DuckDB 1.5.0 regression (#21347)
SET preserve_insertion_order=false;
SET enable_object_cache=false;
```

The `s3_allow_recursive_globbing=false` setting is critical: without it, DuckDB 1.5.0 recursively
lists all sub-prefixes before applying hive partition filters, reading all ~94 partition files
instead of the 1 that passes the filter. Established in prior benchmark work in `mcp-data-server`.

## Join Pattern

All joins **must include `h0` in the join condition** alongside `h8`:

```sql
-- CORRECT: DPP prunes partition files; only matching h0 partitions opened
JOIN ... ON a.h8 = b.h8 AND a.h0 = b.h0

-- WRONG: DuckDB opens all partition files at planning time (no DPP possible)
JOIN ... USING (h8)
```

Alternatively, use a static `WHERE c.h0 = X` literal on the probe side — this is ~9s faster and
~200 fewer S3 GETs than join-driven DPP for single-partition queries (prior benchmark result), but
requires knowing the h0 value up front. For global/multi-partition queries, use the join form.

## Execution Protocol

1. **Order**: Run gpu-data-server first on all queries, then duckdb-geo — so CPU doesn't warm S3 caches for GPU.
2. **Repetitions**: 3 runs per query per server; record all times, report median.
3. **Correctness**: Assert row counts match between servers for Q1–Q5 (Q6/Q7 may differ only if one errors).
4. **Record per run**: `(query_id, server, run_number, elapsed_ms, row_count, error_if_any)`

---

## What We're Looking For

- **Crossover point**: at what data size does GPU acceleration become net positive (offsetting PCIe transfer + kernel launch overhead)?
- **Multi-join scaling**: does GPU advantage compound with more joins?
- **Aggregation impact**: does GROUP BY after join amplify or diminish GPU benefit?
- **Memory limit**: Q6 (119 GiB) will probe whether GPU runs out of VRAM and falls back or errors.
- **Correctness parity**: results must be identical (modulo float rounding) between servers.

---

---

### Q3a / Q4a / Q5a — Americas subset (~2–4 GiB compressed, GPU-feasible)

Same queries as Q3/Q4/Q5 but with a `WHERE h0 IN (...)` filter on the carbon CTE that
restricts to 28 h0 cells covering the Americas (identified from the Overture regions dataset).

Estimated compressed size: 28/122 × 9.9 GiB ≈ 2.3 GiB, but tropical cells are
above-average density, so expect 3–5 GiB compressed → ~10–15 GB uncompressed.
Fits in RTX 4000 Ada 20 GB VRAM with reasonable margin.

Americas h0 values used:
```
576531121047601151, 576707042908045311, 576742227280134143, 576812596024311807,
576882964768489471, 576953333512667135, 576988517884755967, 577094071001022463,
577164439745200127, 577199624117288959, 577234808489377791, 577692205326532607,
577727389698621439, 577762574070710271, 578114417791598591, 578149602163687423,
578290339652042751, 578395892768309247, 578747736489197567, 578923658349641727,
578994027093819391, 579381055186796543, 579451423930974207, 579592161419329535,
579627345791418367, 579908820768129023, 580119927000662015, 580401401977372671
```

Q5a drops the social-vulnerability join (US-only dataset) from Q5 to keep it Americas-wide.

---

## GPU VRAM Constraint

The NRP GPU node uses an **NVIDIA RTX 4000 Ada (20 GB VRAM)**. Polars cuDF GPU engine
fully materialises each source table in VRAM; it does not stream or spill to CPU.

| Query | Approx in-memory size | GPU feasible? |
|---|---|---|
| Q1 (IUCN × IUCN) | ~0.5 GiB | ✓ |
| Q2 (3-way IUCN) | ~0.7 GiB | ✓ |
| Q3 (carbon 9.9 GiB × IUCN) | ~30 GiB uncompressed | ✗ OOM |
| Q4 (WDPA × carbon) | ~30 GiB | ✗ OOM |
| Q5 (4-way + carbon) | ~30 GiB | ✗ OOM |
| Q3a (Americas carbon × IUCN) | ~10–15 GiB | ✓ (borderline) |
| Q4a (Americas carbon × WDPA) | ~10–15 GiB | ✓ (borderline) |
| Q5a (Americas carbon × 2 IUCN) | ~10–15 GiB | ✓ (borderline) |
| Q6 (GBIF 119 GiB × IUCN) | >100 GiB | ✗ OOM |
| Q7 (taxonomy tiny × IUCN) | <0.1 GiB | ✓ |

Q3-Q6 are benchmarked CPU-only. A node with ≥40 GB VRAM (e.g. A100 80GB or
H100 80GB) would be needed for GPU to compete on the large-dataset queries.
The KvikIO/cuDF branch adds GPU-accelerated decompression which would still
benefit Q3-Q5 on a larger GPU.

## Known Issue: duckdb-geo STAC Catalog Empty

See `stac-bug.md` in this directory. The duckdb-geo server shows "No datasets loaded" at startup
due to a missing fix that was already applied to mcp-gpu-data-server. This does NOT block benchmarking
since both servers can still execute `read_parquet('s3://...')` queries directly.
