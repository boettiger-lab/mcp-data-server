# [CLOSED — not a bug] DPP file-level pruning on S3

Our original benchmark compared queries that were not semantically equivalent:
- Query A joined only on `h8` (no h0 equijoin → DuckDB has nothing to propagate)
- Query B had a static `WHERE c.h0 = X` literal

When the join includes `AND p.h0 = c.h0`, DPP correctly prunes to 1/94 files on S3.
This is the correct result and matches local behavior.

## Corrected benchmark (DuckDB 1.5.0, public S3 endpoint)

```python
# Query A: join on h8 AND h0 — proper DPP
# WITH parks AS (SELECT DISTINCT h8, h0 FROM padus WHERE ... AND h0 = H0)
# SELECT ... FROM parks p JOIN carbon c ON p.h8 = c.h8 AND p.h0 = c.h0
# → Total Files Read: 1, #GET: 737, in: 109 MiB, 38s

# Query B: static WHERE literal
# WITH parks AS (SELECT DISTINCT h8, h0 FROM padus WHERE ... AND h0 = H0)
# SELECT ... FROM parks p JOIN carbon c ON p.h8 = c.h8 WHERE c.h0 = H0
# → Total Files Read: 1, #GET: 546, in: 109 MiB, 29s
```

Both open 1 file. DPP is ~9s slower and ~200 GETs more than a static literal,
likely due to build-side materialization overhead before the filter is applied.
This is expected behavior, not a bug.

## Lesson

Always include the partition key (h0) in join conditions — see query-optimization.md.

## Related

- PR #19888 — file-level DPP (1.5.0)
- Issue #21347 — s3_allow_recursive_globbing regression (separate, still valid)
