# Benchmarks

DuckDB S3 query performance benchmarks for the mcp-data-server datasets.

## Files

### `benchmark-public.py`

Runs against the public NRP S3 endpoint (`s3-west.nrp-nautilus.io`). Tests two issues:

- **Issue A** (`s3_allow_recursive_globbing` regression, DuckDB 1.5.0 #21347): With the default setting, DuckDB recursively lists all sub-prefixes before applying hive partition filters, reading all 94 files instead of 1. Workaround: `SET s3_allow_recursive_globbing=false`.

- **Issue B** (DPP file-level pruning): Verifies that join-driven dynamic partition pruning correctly prunes carbon files on S3 when `h0` is included in the join condition. See `duckdb-s3-dpp-issue.md` for the investigation notes.

Run with:
```bash
uv run --with duckdb benchmark-public.py
```

### `duckdb-s3-dpp-issue.md`

Post-mortem on the DPP investigation. Conclusion: not a DuckDB bug — the original benchmark joined only on `h8` (missing `h0`), so there was nothing for DPP to propagate. When the join includes `AND p.h0 = c.h0`, DuckDB correctly prunes to 1/94 files on S3.

### `k8s/benchmark-job-static.yaml`

Kubernetes Job (opportunistic priority, no GPU nodes) that tests the `s3_allow_recursive_globbing` regression against the internal NRP endpoint (`rook-ceph-rgw-nautiluss3.rook`). Compares file counts and GET requests with and without the workaround setting.

### `k8s/benchmark-job.yaml`

Kubernetes Job testing multi-step join query patterns against the internal NRP endpoint:

- **Test 5**: Two-step join with static `h0` literal — the correct recommended pattern.
- **Test 6**: Regions-style lookup (derives h0 from `overturemaps/hex/regions`) with `h0` in join condition.
- **Test 7**: Two-step with `h0 IN (...)` list derived from regions rather than hardcoded.

Deploy with:
```bash
kubectl apply -f k8s/benchmark-job-static.yaml
kubectl apply -f k8s/benchmark-job.yaml
kubectl logs -l job-name=duckdb-benchmark -f
```

### `k8s/thread-sweep-job.yaml`

Phase 1 of the DuckDB thread-count tuning: sweeps `SET THREADS` across {8, 16, 32, 64, 100, 200, 400} for three representative query shapes (single-h0 SUM, two-step join, California-wide aggregation), 5 iterations each. Reports min/p50/mean/p95 wall time per (query, threads) cell. Internal endpoint, 16 CPU / 16 GiB pod, opportunistic priority. Used to find the per-query bandwidth-saturation point.

### `k8s/concurrency-grid-job.yaml`

Phase 2 of the thread-count tuning: holds the workload to the typical join query, varies thread count × concurrent client count (1, 2, 4, 8 concurrent fresh-connection queries). Reports per-cell wall time, throughput (qps), and p50/p95 query latency. Used to find where DuckDB internal parallelism stops helping under multi-client load. Phase 2's `THREAD_VALUES` should be retuned around the Phase 1 winner before re-running.

Deploy with:
```bash
kubectl apply -f k8s/thread-sweep-job.yaml       # ~20–30 min
kubectl logs -f job/duckdb-thread-sweep
kubectl apply -f k8s/concurrency-grid-job.yaml   # ~10–15 min
kubectl logs -f job/duckdb-concurrency-grid
```

## Key findings

1. Always include `h0` in join conditions (e.g., `ON p.h8 = c.h8 AND p.h0 = c.h0`) — this is what enables DPP file-level pruning.
2. Set `s3_allow_recursive_globbing=false` on DuckDB 1.5.0 when querying partitioned data on S3 to avoid reading all files at planning time.
3. A static `WHERE c.h0 = X` literal is ~9s faster and ~200 fewer GETs than join-driven DPP for single-partition queries, due to build-side materialization overhead — but both open only 1 file.

See `../query-optimization.md` for the actionable rules derived from these findings.
