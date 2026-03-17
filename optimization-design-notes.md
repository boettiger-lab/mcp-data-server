# Query Optimization Design Notes

> **These are internal design notes for developers, not LLM agent instructions.**
> For LLM-facing guidance, see `query-optimization.md` and `query-setup.md`.

---

## Background: What We Learned

This document records the findings from a systematic investigation (March 2026) into
DuckDB query performance on hive-partitioned parquet data stored on NRP S3 (Ceph/Rook).

---

## 1. Dynamic Partition Pruning (DPP): Local vs. S3

### What DPP is

DuckDB's Dynamic Partition Pruning eliminates partitions at runtime based on join
filter values materialized from the build side of a hash join. For a hive-partitioned
dataset like `hex/h0={ubigint}/data_0.parquet`, DPP should ideally skip files whose
`h0` partition value does not appear in the join's build side.

### Two levels of pruning

| Level | What gets skipped | When it works |
|---|---|---|
| **File-level** | Entire parquet files never opened | When file list can be filtered *after* build side materializes |
| **Row-group level** | Row groups within open files (via parquet footer stats) | Always, once file is open |

### The S3 limitation

For local files, DuckDB 1.5.0 achieves **file-level DPP** (PR #19888, merged Dec 2025).
The file list is enumerated from the filesystem, then filtered at execution time once
the build side is materialized. Result: only matching partition files are opened.

For S3, this does **not** work. The S3 file list is enumerated at **planning time** via
`S3 LIST`. By execution time (when the build side is materialized and filter values are
known), the file list is locked in. All files get opened for parquet footer reads.

**Benchmarked (DuckDB 1.5.0, apples-to-apples on same carbon-2024 data):**

| Storage | Files opened | HTTP GETs | Data scanned | Time |
|---|---|---|---|---|
| Local disk | 1 / 94 | n/a | ~105 MB | 1.13s |
| NRP S3 | 94 / 94 | 3,714 | 902 MiB | 4.53s |

Both queries use the same join structure: a small reference table drives a join on `h8`
with `h0` as the partition key. Locally, DPP fires and prunes 93/94 files. On S3, DPP
operates at row-group level only — all 94 files are opened, all footers are fetched.

### Why the partition key design matters

Our parquet files store `h0` **both** in the directory path (`h0={value}/`) **and** as
a column inside each file. DuckDB uses the **in-file column** for DPP (row-group stats),
not the path. This means even with file-level DPP available (local), if the column is
in the file, DuckDB can still do file-level DPP by matching derived filter values against
the known path values. But on S3, the path matching happens too late.

If `h0` were stored **only** in the path (not in the file), DuckDB could potentially
use path-string matching for file-level pruning on S3 too — but this is not the current
behavior and would require a DuckDB change.

### Open DuckDB issue

See `duckdb-s3-dpp-issue.md` for a draft issue report requesting that DuckDB use
hive partition path encoding for file-level DPP on S3, matching derived filter values
against path strings before making any HTTP requests.

---

## 2. Cardinality Estimation Bug (DuckDB 1.5.0)

### The bug

DuckDB 1.5.0 uses only the **first parquet file** to estimate total dataset cardinality
for glob scans (`read_parquet('s3://bucket/hex/**')`). The first file (sorted by S3 LIST
response) may be a tiny ocean-hex partition with very few rows. This leads to wildly
inaccurate cardinality estimates — sometimes 100x off — which causes the optimizer to
choose the wrong join order.

**Observed effect:** A query joining PADUS (~1.46M rows for CA NPs) with carbon-2024
(~2.1B rows total, but filtered to 1 h0 partition) was estimated as PADUS ~447M rows.
The optimizer chose carbon as the hash build side (estimated small) → actual 2.1B-row
hash table → OOM at 16 GiB and 32 GiB.

### The fix

PR #21374 (merged 2026-03-14, not yet in a release as of writing) fixes this by using
**file sizes from the S3 LIST response** (already available during glob expansion) to
estimate per-file row counts via `bytes_per_row` derived from the first file, applied
proportionally to all other file sizes. This gives much better estimates without extra
HTTP requests.

### Implications for query design

Until PR #21374 ships in a release, any query that:
1. Scans a large partitioned dataset with a glob
2. Applies non-partition filters that reduce cardinality significantly
3. Joins to another large dataset

...is at risk of wrong join order and OOM. The two-step approach (Section 4) sidesteps
this entirely.

---

## 3. Benchmark Summary

All benchmarks run on NRP k8s cluster, DuckDB 1.5.0, `biodiversity` namespace.
Datasets: PADUS-4.1 (7 GiB, 21 h0 files), carbon-2024 (9.9 GiB, 94 h0 files).
Query: sum carbon in CA National Parks.

| Test | Approach | Result |
|---|---|---|
| PADUS scan alone (CA NP filter) | `WHERE State_Nm='CA' AND Des_Tp='NP'` | 2.29s |
| PADUS h0 lookup | Distinct h0 from above | 2.02s → `577199624117288959` |
| Default join | PADUS → carbon, no h0 hint | OOM (cardinality bug → wrong join order) |
| Forced join order | MATERIALIZED CTE | OOM (correct order but no file-level DPP → 9.9 GiB in memory) |
| **Two-step (static h0 literal)** | Pre-compute h0, embed as literal | **6.91s ✓** |
| **Regions JOIN style** | Overture regions drives h0 discovery | **6.62s ✓** |
| **Two-step from regions** | Regions → static h0 IN list → both joins | **5.54s ✓** |

The two-step approach avoids both the cardinality bug and the S3 DPP limitation.

---

## 4. The CTE Driving Principle

### The key insight

The performance of a multi-table join on partitioned S3 data depends critically on
**which CTE or subquery drives the join**. The driving CTE must be:

1. **Small in total bytes** — fast to scan in full
2. **Spatially concentrated** — covers only a few h0 partition cells
3. **Partition-key-anchored** — produces h0 values as output, not just h8

When the driving CTE satisfies these criteria, subsequent joins on large partitioned
datasets can use those h0 values as static filter literals — enabling file-level partition
pruning at planning time (even on S3) and correct join order estimation.

### Why regions and countries work as drivers

The Overture `regions/**` dataset (437 MiB, 94 files) contains pre-computed h8/h0 cell
assignments for every administrative region. A query like
`WHERE region = 'US-CA'` returns only the cells within California — typically covering
1-2 h0 partition cells for a US state.

Similarly, `countries.parquet` (single flat file, fast to scan) provides country-level
h0 coverage.

These datasets are **designed** to be small drivers. They return the minimal set of h0
values needed to prune subsequent joins on large datasets.

### Why PADUS fails as a driver

PADUS is 7 GiB with 21 partition files. A filter like `WHERE State_Nm='CA' AND Des_Tp='NP'`
requires scanning all 21 files (no geographic partition filter on h0 available before
the scan). The result is spatially concentrated (1 h0 value), but getting there requires
reading the full dataset first. You cannot use PADUS to drive a join that would benefit
from partition pruning — instead, you need to use a geographic reference dataset to
establish the h0 scope, then join to PADUS within that scope.

### The two-step pattern in practice

```sql
-- Step 1: Get h0 values from a fast reference dataset
-- (run as a separate Python query, embed results as literals)
SELECT DISTINCT h0
FROM read_parquet('s3://public-overturemaps/hex/regions/**')
WHERE region = 'US-CA'
-- → [577199624117288959, 577762574070710271]

-- Step 2: Use h0 literals to prune all large dataset scans at planning time
WITH parks AS (
  SELECT DISTINCT h8, h0
  FROM read_parquet('s3://public-padus/padus-4-1/fee/hex/**')
  WHERE Des_Tp = 'NP'
    AND h0 IN (577199624117288959, 577762574070710271)  -- ← file-level pruning
)
SELECT SUM(c.carbon)/1e6
FROM parks p
JOIN read_parquet('s3://public-carbon/vulnerable-carbon-2024/hex/**') c
  ON p.h8 = c.h8
WHERE c.h0 IN (577199624117288959, 577762574070710271)  -- ← file-level pruning
```

Static h0 literals in WHERE clauses allow DuckDB to apply file-level pruning at
**planning time** on S3 (unlike join-derived DPP which is too late). This is the
recommended pattern until DuckDB fixes S3 DPP at the file level.

### The JOIN-CTE pattern (wetland app style)

The wetland app uses a slightly different pattern — driving the join with regions
directly as a CTE without pre-extracting h0:

```sql
WITH ca_hexes AS (
  SELECT DISTINCT h8, h0
  FROM read_parquet('s3://public-overturemaps/hex/regions/**')
  WHERE region = 'US-CA'
),
wetlands AS (
  SELECT w.h8, w.h0, w.area_m2
  FROM ca_hexes ca
  JOIN read_parquet('s3://public-wetlands/glwd/hex/**') w
    ON ca.h8 = w.h8 AND ca.h0 = w.h0
)
...
```

This works because: (a) regions is small enough to scan quickly, (b) the join on h0
propagates a row-group-level filter via DPP, and (c) regions' cells are spatially
concentrated (few h0 cells). The `AND ca.h0 = w.h0` condition is what enables the
h0 filter to propagate.

The two-step approach extracts h0 values as Python-side literals first, which is
slightly faster because it enables file-level pruning at planning time rather than
row-group pruning at execution time.

---

## 5. Communicating This to the LLM Agent

### Current query-optimization.md issues

Section 2 of `query-optimization.md` states:
> "ALWAYS Include h0 in Joins — Enables partition pruning → 5-20x faster"

This is **factually incorrect** for S3. Including h0 in a join does NOT enable
partition pruning on S3 (only row-group pruning). The 5-20x claim is unsubstantiated.
This section needs to be rewritten.

### What the LLM needs to know

The LLM agent needs actionable rules, not implementation details. The key rules are:

1. **Use small geographic drivers**: Start queries with regions or countries data to
   establish the h0 scope. Don't start with large thematic datasets like PADUS.

2. **Always include h0 in join conditions**: Even though it doesn't enable file-level
   DPP on S3, it enables row-group pruning and is necessary for correctness.

3. **For maximum performance, use the two-step pattern**: Run a quick h0 lookup first,
   embed as static IN list, then run the main query with those literals in WHERE clauses.

4. **Check dataset sizes before choosing a join driver**: A dataset with 94 partition
   files at 100 MiB each is very different from one at 100 KB each.

### Using STAC metadata to guide the LLM

The cleanest generalization is a single `duckdb:join_role` field in each STAC
collection's `extra_fields`. This encodes whether a dataset is a good join driver
without requiring the LLM to reason about sizes or partition counts:

```json
{
  "id": "overture-regions",
  "extra_fields": {
    "duckdb:join_role": "spatial-reference",
    "duckdb:size_gib": 0.43
  }
}
```

```json
{
  "id": "padus-4-1",
  "extra_fields": {
    "duckdb:join_role": "thematic",
    "duckdb:size_gib": 7.0
  }
}
```

**`spatial-reference`**: Small dataset whose primary purpose is mapping geographic
names/identifiers to h8/h0 cells. Good driving CTE candidates. Currently:
- `overture-regions` — regions/states/provinces by name → h8/h0 (437 MiB, 94 files)
- `overture-countries` — country polygons → h8/h0 (single flat file, very fast)

**`thematic`**: Substantive datasets about physical or policy attributes. Large,
not suitable as join drivers for geographic filtering. Everything else falls here:
PADUS, carbon, wetlands, species ranges, etc.

The `duckdb:size_gib` field helps the LLM estimate scan cost when choosing between
multiple spatial-reference options (countries is faster than regions for country-level
queries).

### LLM prompt steering

With this metadata in place, `query-optimization.md` can instruct:

> "When a query has a geographic scope (country, state, region), check the STAC
> catalog for datasets with `duckdb:join_role: spatial-reference` and use one as
> the first CTE. Join thematic datasets to the result of that CTE, always including
> h0 in join conditions."

This steers the LLM toward the correct pattern without exposing DPP internals,
and generalizes automatically as new spatial-reference datasets are added to the
catalog.

### Risks of overly generic guidance

Guidance like "always filter small tables first" can be misleading because:
- "Small" is relative — regions is 437 MiB, which is small vs. carbon (9.9 GiB) but
  large vs. countries.parquet (single file)
- The issue isn't table size per se but **spatial concentration** (how many h0 cells)
- A non-geographic filter on a large dataset doesn't help establish h0 scope

Better framing: "Start with geographic scope (regions or countries), then apply
thematic filters within that scope."

---

## 6. S3 Is Not a Bottleneck for This Pattern

The benchmarks confirm that using regions and countries directly from S3 as driving
CTEs is already fast enough — 6.62s for the regions JOIN style, without any local
caching. The gains come from the driving CTE being small and spatially concentrated,
not from where it is stored.

Row-group DPP still works on S3: once the regions CTE is materialized (~437 MiB scan),
DuckDB propagates the resulting h8/h0 set as a filter into subsequent large dataset
scans. Files are still all opened for footer reads, but matching row groups are skipped
within each file. For driving CTEs that cover 1-2 h0 cells, this is effective.

The file-level DPP gap (Section 1) matters most when the driving CTE is large or
spans many h0 cells — which is exactly the failure mode we avoid by steering queries
toward spatial-reference datasets. With good query structure, the S3 overhead is
acceptable.

Local volumes or caching would add complexity without meaningful benefit given the
current query patterns and dataset sizes.
