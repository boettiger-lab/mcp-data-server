# Query Optimization Essentials

## 1. Always start with a geographic reference dataset

Use `regions/hex/**` or `countries.parquet` as the first CTE to establish the h0 scope.
Never start with a large thematic dataset (PADUS, carbon, wetlands, species) as the
join driver — they require a full scan to apply non-geographic filters.

Datasets with `duckdb:join_role: spatial-reference` in the STAC catalog are pre-built
for this purpose. Currently: `overture-divisions` (regions and countries).

```sql
-- Good: regions drives the query, h0 scope known upfront
WITH ca_hexes AS (
  SELECT DISTINCT h8, h0
  FROM read_parquet('s3://public-overturemaps/regions/hex/**')
  WHERE region = 'US-CA'
),
parks AS (
  SELECT DISTINCT p.h8, p.h0
  FROM ca_hexes ca
  JOIN read_parquet('s3://public-padus/padus-4-1/fee/hex/**') p
    ON ca.h8 = p.h8 AND ca.h0 = p.h0
  WHERE p.Des_Tp = 'NP'
)
SELECT SUM(c.carbon)/1e6
FROM parks p
JOIN read_parquet('s3://public-carbon/vulnerable-carbon-2024/hex/**') c
  ON p.h8 = c.h8 AND p.h0 = c.h0
```

## 2. Always include h0 in join conditions

```sql
-- Required: always join on BOTH h8 and h0
JOIN table2 ON table1.h8 = table2.h8 AND table1.h0 = table2.h0
```

This enables row-group pruning within each file and is required for correctness.
Note: on S3, this does **not** skip entire partition files (file-level pruning requires
a static `WHERE h0 = X` literal — see Section 3).

## 3. For maximum performance: use static h0 literals (two-step pattern)

When you know the h0 values in advance, embed them as static WHERE clause literals.
This allows DuckDB to prune partition files at planning time on S3, which join-derived
filters cannot do.

```sql
-- Step 1 (run first, embed result as literal in step 2):
SELECT DISTINCT h0
FROM read_parquet('s3://public-overturemaps/regions/hex/**')
WHERE region = 'US-CA'
-- → [577199624117288959, 577762574070710271]

-- Step 2: static IN list enables file-level pruning on S3
WITH parks AS (
  SELECT DISTINCT h8, h0
  FROM read_parquet('s3://public-padus/padus-4-1/fee/hex/**')
  WHERE Des_Tp = 'NP'
    AND h0 IN (577199624117288959, 577762574070710271)
)
SELECT SUM(c.carbon)/1e6
FROM parks p
JOIN read_parquet('s3://public-carbon/vulnerable-carbon-2024/hex/**') c
  ON p.h8 = c.h8
WHERE c.h0 IN (577199624117288959, 577762574070710271)
```

**Note:** `rook-ceph-rgw-nautiluss3.rook` is an internal endpoint that only your tool running on k8s can access. The publicly accessible external endpoint is `s3-west.nrp-nautilus.io`, which requires `USE_SSL true` and `SET THREADS=2`. Always use the internal endpoint to run queries.

You must read parquet datasets from S3 using read_parquet(). There are no local tables.
