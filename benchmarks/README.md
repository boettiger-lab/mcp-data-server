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

### `s3-throughput-bench.py`

Engine-independent (raw boto3, no DuckDB) read-throughput benchmark comparing the
in-cluster Ceph West pool (`rook-ceph-rgw-nautiluss3.rook`), the public Ceph
gateway (`s3-west.nrp-nautilus.io`), and the AWS us-west-2 source.coop mirror
(`us-west-2.opendata.source.coop`, reached over Internet2/CENIC), all on the same
carbon hex dataset. Reports: LIST latency, single-stream MB/s, **aggregate
throughput vs concurrency {1,4,8,…}** (the headline — distinct objects per level
to avoid cache warming), and per-object open latency (warm vs cold connection) as
a secondary diagnostic.

Framing note: this replaced an earlier per-object-latency MRE premised on
"opening ~10³ Parquet footers". That premise was a now-fixed GBIF over-sharding
bug — every queryable hex dataset is 1 file per h0 (carbon 122, padus 21, gbif
2026 122), so a pruned query opens 1 footer and a global scan ≤122. The real open
question is **bandwidth**, not open-latency, hence this throughput benchmark.

```bash
uv run --with boto3 python3 s3-throughput-bench.py
MRE_ENDPOINTS=nrp-external,aws-sourcecoop uv run --with boto3 python3 s3-throughput-bench.py
```

### `k8s/s3-throughput-job.yaml`

Runs `s3-throughput-bench.py` on N distinct us-west nodes (Indexed Job +
podAntiAffinity on hostname → one pod per node) to check whether throughput is
consistent across the heterogeneous NRP fleet vs driven by a few slow nodes. Each
pod prints its node name. Fetches the script from `main` by raw URL, so commit
first. Pins `topology.kubernetes.io/region=us-west` so the in-cluster-vs-WAN
comparison is honest.

```bash
kubectl apply -f k8s/s3-throughput-job.yaml
kubectl logs -l job-name=s3-throughput -f --max-log-requests=12 --prefix
kubectl delete job s3-throughput
```

## Key findings

1. Always include `h0` in join conditions (e.g., `ON p.h8 = c.h8 AND p.h0 = c.h0`) — this is what enables DPP file-level pruning.
2. Set `s3_allow_recursive_globbing=false` on DuckDB 1.5.0 when querying partitioned data on S3 to avoid reading all files at planning time.
3. A static `WHERE c.h0 = X` literal is ~9s faster and ~200 fewer GETs than join-driven DPP for single-partition queries, due to build-side materialization overhead — but both open only 1 file.
4. Queryable hex datasets are 1 file per h0 (carbon 122, padus 21, gbif-2026 122), so a pruned query opens 1 footer and a global scan ≤122. The historical "~923 files / ~126-per-h0" was a since-fixed GBIF over-sharding bug, not steady state — large scans are bandwidth-bound, not open-latency-bound. This retires the per-object-latency framing in older notes.

See `../query-optimization.md` for the actionable rules derived from these findings.
