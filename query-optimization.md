# Query Optimization Essentials

## 1. Filter Small Tables First

```sql
-- Good: Filter country → then join to large dataset
WITH filtered AS (
  SELECT h8, h0 FROM read_parquet('s3://public-overturemaps/hex/countries.parquet')
  WHERE country = 'US'
)
SELECT ... FROM filtered JOIN read_parquet('s3://public-wetlands/glwd/hex/**') w 
ON filtered.h8 = w.h8 AND filtered.h0 = w.h0
```

## 2. ALWAYS Include h0 in Joins

```sql
-- Enables partition pruning → 5-20x faster
JOIN table2 ON table1.h8 = table2.h8 AND table1.h0 = table2.h0
```


**Note:** `rook-ceph-rgw-nautiluss3.rook` is an internal endpoint that only your tool running on k8s can access. The publicly accessible external endpoint is `s3-west.nrp-nautilus.io`, which requires `USE_SSL true` and `SET THREADS=2`. Always use the internal endpoint to run queries.

You must read parquet datasets with from S3 using read_parquet().  There are no local tables.