# Query Optimization Essentials

## 1. Cross-dataset hex joins require explicit h0 path filtering

All hex datasets are hive-partitioned by `h0` (`hex/h0=<value>/data_0.parquet`).
DuckDB **does not** do dynamic partition pruning for join conditions at runtime —
joining two `hex/**` globs always scans every partition of both datasets, even
if only one h0 cell is relevant. This can scan billions of rows unnecessarily.

**The correct pattern** is a two-step approach: first get the distinct h0 values
from the smaller/filtered dataset, then construct explicit paths for the second:

```sql
-- Step 1: find which h0 cells are relevant (fast — filtered scan)
WITH area_h0s AS (
  SELECT DISTINCT h0
  FROM read_parquet('s3://public-padus/padus-4-1/fee/hex/**')
  WHERE State_Nm = 'CA' AND Des_Tp = 'NP'
),
-- Step 2: get h8 hexes from the filtered dataset
area_hexes AS (
  SELECT DISTINCT h8, h0
  FROM read_parquet('s3://public-padus/padus-4-1/fee/hex/**')
  WHERE State_Nm = 'CA' AND Des_Tp = 'NP'
)
-- Step 3: join to second dataset using explicit h0 paths via hive filter
SELECT SUM(c.carbon)/1e6 AS megatons
FROM area_hexes a
JOIN read_parquet('s3://public-carbon/vulnerable-carbon-2024/hex/**') c
  ON a.h8 = c.h8
WHERE c.h0 IN (SELECT h0 FROM area_h0s)
```

The `WHERE c.h0 IN (...)` with a subquery that materializes to a small literal set
allows DuckDB to apply static partition pruning on the second dataset, opening
only the matching h0 partition files. Without this, DuckDB scans all partitions.

**Benchmark results (DuckDB 1.5.0, 4 CPUs, internal S3):**

| Pattern | Time | Files read |
|---|---|---|
| `JOIN ... ON h0 = h0` (no WHERE filter) | ~440s | 94/94 |
| `WHERE h0 IN (SELECT DISTINCT h0 ...)` | ~288s | 94/94 |
| `WHERE h0 = <literal>` (static) | 4.5s | 1/94 |
| Explicit path list per h0 | 3.8s | 1/94 |

Note: the `WHERE h0 IN (subquery)` pattern still scans all files in 1.5.0 — this
is a known DuckDB issue (https://github.com/duckdb/duckdb/issues/21347). The
subquery needs to be materialized as a CTE so DuckDB evaluates it first and can
apply static pruning. A workaround is in place (`SET s3_allow_recursive_globbing=false`
in query-setup.md) — track https://github.com/boettiger-lab/mcp-data-server/issues/9
for when this can be removed.

## 2. Use Overturemaps hex/countries.parquet for country-level h0 lookup

For country-level queries, `s3://public-overturemaps/hex/countries.parquet` is a
single flat file (no partitioning) with columns `country, name, h8, h0`. Use it
to find h0 values for a country without scanning large partitioned datasets:

```sql
SELECT DISTINCT h0
FROM read_parquet('s3://public-overturemaps/hex/countries.parquet')
WHERE country = 'US'
```

## 3. Filter early, push predicates into the first scan

Always apply geographic and attribute filters in the CTE that reads the primary
dataset. DuckDB pushes `WHERE` clauses into the parquet scan and uses row-group
min/max statistics to skip non-matching row groups within each file.

```sql
-- Good: filter in the CTE, not after the join
WITH filtered AS (
  SELECT h8, h0
  FROM read_parquet('s3://public-padus/padus-4-1/fee/hex/**')
  WHERE State_Nm = 'CA' AND GAP_Sts IN ('1', '2')  -- filter here
)
SELECT ... FROM filtered JOIN ...
```
