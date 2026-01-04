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
