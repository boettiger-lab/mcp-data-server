# Query Optimization Essentials

## 1. Always include h0 in joins

Most datasets are hive-partitioned by h0. When both sides of a join have h0, always include it in the join condition:

```sql
JOIN table2 ON table1.hX = table2.hX AND table1.h0 = table2.h0
```

where `hX` is the finest resolution shared by both datasets (h8, h9, etc. — check the
schema). Omitting `AND t1.h0 = t2.h0` causes DuckDB to open every partition file on S3
instead of only the matching ones (10-100x slower).

## 2. Start with a small geographic reference dataset

Use `regions/hex/**` or `countries.parquet` as the first CTE to establish geographic
scope before joining large thematic datasets (PADUS, carbon, wetlands, species).

```sql
WITH scope AS (
  SELECT DISTINCT h8, h0
  FROM read_parquet('s3://public-overturemaps/regions/hex/**')
  WHERE region = 'US-CA'
),
parks AS (
  SELECT DISTINCT p.h8, p.h0
  FROM scope s
  JOIN read_parquet('s3://public-padus/padus-4-1/fee/hex/**') p
    ON s.h8 = p.h8 AND s.h0 = p.h0
  WHERE p.Des_Tp = 'NP'
)
SELECT SUM(c.carbon)/1e6
FROM parks p
JOIN read_parquet('s3://public-carbon/vulnerable-carbon-2024/hex/**') c
  ON p.h8 = c.h8 AND p.h0 = c.h0
```

**Note:** `rook-ceph-rgw-nautiluss3.rook` is an internal endpoint only accessible from k8s. Always use it — not the public endpoint — to run queries.

You must read parquet datasets from S3 using read_parquet(). There are no local tables.
