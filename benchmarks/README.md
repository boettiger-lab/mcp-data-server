# Benchmarks

DuckDB S3 query performance benchmarks for the mcp-data-server datasets.

## Files

### `cpu-vs-gpu-bench.py`

CPU (DuckDB) vs GPU (Polars/cudf-polars) comparison over the MCP `query` tool
(issue #42). Points at two `mcp-data-server` deployments — a `duckdb` one and a
`polars-gpu*` one sharing a node + S3 backend (e.g. the cirrus CPU and GPU
deploys) — runs a query suite against each, and reports median wall-clock and
the CPU/GPU speedup. Queries must stay in the GPU dialect subset (pre-computed
`h0..h11`, no `h3_*` functions); prefer DPP-friendly `h0` filters to keep host
RAM bounded. Default queries are small/DPP-pruned (RAM-safe); extend with the
carbon/IUCN/WDPA suite once those datasets are staged and the GPU node is sized.

```bash
BENCH_CPU_URL=https://duckdb-mcp.carlboettiger.info \
BENCH_GPU_URL=https://gpu-mcp.carlboettiger.info \
uv run --with requests benchmarks/cpu-vs-gpu-bench.py
```

#### Standing results (2026-07, cirrus)

CPU = `duckdb-mcp`; GPU = `gpu-mcp-test` (polars-gpu-cudf, RTX 8000, 4 slices),
both reading the same in-cluster MinIO. Median of 3 warm runs; GPU execution
confirmed on-GPU (fallback off; DPP + kvikio engaged). Speedup = CPU ÷ GPU
(>1 means GPU faster).

| Query | CPU (DuckDB) | GPU (cudf-polars) | Speedup |
|---|---|---|---|
| carbon-sum-1part (~54MB) | 0.18s | 0.49s | 0.36× |
| carbon-sum-4part (~216MB) | 0.33s | 1.89s | 0.18× |
| carbon-multiagg-4part | 0.34s | 2.45s | 0.14× |
| carbon-groupby-h7 (high-card) | 0.36s | 1.90s | 0.19× |
| gbif-count-4part | 0.16s | 0.27s | 0.61× |

**DuckDB is 3–6× faster on every query** — consistent with #42's expectation that
S3-I/O-bound queries favour CPU. At this scale the GPU path's read → CPU parse →
host→VRAM transfer → per-query GPU setup dominates the (light) hash-aggregation
compute, which DuckDB does in a single streamed, pruned pass. **This does not yet
establish a GPU win.** A GPU advantage, if it exists, would need compute-bound
workloads (large multi-way joins, window functions, repeated ops on
already-resident data) or datasets large enough to amortise the transfer — not
tested here (host-RAM bounded, and the busy co-tenant card limits headroom).

#### Standing results (2026-07, NVIDIA DGX Spark, node `nimbus`)

Full runbook: [`docs/architecture/gpu-spark-handoff.md`](../docs/architecture/gpu-spark-handoff.md).
GPU = `mcp-gpu-nimbus` (polars-gpu-cudf, GB10 Blackwell, unified memory, arm64),
deployed via `k8s/nimbus-gpu-deployment.yaml`. Two very different results
depending on whether the network is in the loop:

**Networked** (GPU reads the remote cirrus MinIO over the internet; CPU =
`duckdb-mcp` on cirrus reading it loopback — same fairness caveat as above,
now compounded by an extra network hop for the GPU side):

| Query | CPU (DuckDB, cirrus) | GPU (cudf-polars, Spark) | Speedup |
|---|---|---|---|
| carbon-sum-1part (~54MB) | 0.17s | 0.98s | 0.17× |
| carbon-sum-4part (~216MB) | 0.32s | 9.40s | 0.03× |
| carbon-multiagg-4part | 0.34s | 5.90s | 0.06× |
| carbon-groupby-h7-4part (high-card) | 0.36s | 7.85s | 0.05× |
| gbif-count-4part | 0.15s | 0.49s | 0.30× |

GPU is *worse* than even cirrus's discrete-VRAM GPU here — the extra network
hop (Spark → cirrus MinIO) more than offsets Blackwell + unified memory.
Confirms the fairness caveat: this is not the test that isolates compute.

**Direct-read** (both engines reading the same partitions staged onto the
pod's local disk — no network, unified memory in play): the result **flips**.

| Test | Data | DuckDB (local) | GPU (local) | Speedup |
|---|---|---|---|---|
| carbon-sum, 4 partitions | ~732MB (recompressed) | 0.40s | 0.13s | **3.1×** |
| carbon-sum, 20 partitions | ~4.75GB (recompressed) | 2.51s | 1.10s | **2.3×** |
| carbon×gbif join-of-aggregates (4 h0) | ~732MB + ~34MB | 0.39s | 0.09s | **4.2×** |

GPU wins outright once the network is removed — confirming the hypothesis in
[gpu-query-engine.md](../docs/architecture/gpu-query-engine.md): on cirrus's
discrete Turing card the loss was dominated by host→VRAM transfer, and the
Spark's unified memory (CPU+GPU share LPDDR5X) removes that cost. This is the
first hardware point where the GPU engine wins on this workload.

**Dialect gaps found** (now enforced by `sql_translate.guard_unsupported`, with
tests in `tests/test_engines.py`): `RANK() OVER (...)` (and `ROW_NUMBER`,
`DENSE_RANK`, `LAG`, `LEAD`) have no Polars SQLContext equivalent at all — CPU
*or* GPU — same class of gap as `h3_*` functions. `SUM(...) OVER (...)` (and
`AVG`/`COUNT`) parse and run fine on `polars-cpu`, but fail loudly on
`GPUEngine`'s collect (`NotImplementedError: ... UnaryFunction in
groupby/rolling`) — a GPU-compute-only gap. Both fail loudly, no silent
fallback, consistent with the documented "dialect miss → fail loud" policy.
See gpu-query-engine.md's GPU dialect subset section.

Caveats: recompression during staging inflated file sizes (DuckDB's parquet
writer used different compression than the source), so "~732MB"/"~4.75GB" are
on-disk sizes at rest, not the original object sizes; row-level joins on `h0`
alone are **not** safe to benchmark (h0 is a partition key shared by every row
in a file, not a unique key — a naive `JOIN ... ON c.h0 = g.h0` before
aggregating is a near-Cartesian product and will run away; aggregate each side
by `h0` first, then join the aggregates, as done here).

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

### `source-coop-endpoint-bench.py`

Compares the two front doors to the *same* source.coop objects: the **direct AWS
S3 bucket** (`us-west-2.opendata.source.coop`, a CNAME straight to
`s3.us-west-2.amazonaws.com` — what the #260/#261 fallback maps to) vs the
**`data.source.coop` gateway** (Cloudflare-fronted CDN proxy over the same S3
origin, the host that appears in the source.coop STAC hrefs). Raw HTTP probes
(footer latency, single-stream MB/s, aggregate MB/s) isolate transport; DuckDB
`count(*)` and single-column `sum()` over the whole dataset show the query-path
impact. Cold and warm passes both reported (Cloudflare may edge-cache).

```bash
uv run --with 'duckdb>=1.1' --with requests python3 source-coop-endpoint-bench.py
```

Result (2026-07-03, run from **UC Berkeley campus egress — not NRP**; relative
comparison holds directionally, absolute numbers differ on-cluster over
Internet2/CENIC — re-run the k8s form for production numbers):

| metric | aws-direct | data.source.coop proxy |
|---|---|---|
| footer latency (median) | 133 ms | 138 ms |
| single-stream throughput (median) | **80.8 MB/s** | 64.5 MB/s |
| aggregate throughput (c=8) | 96.9 MB/s | 108.9 MB/s (n=1, noisy) |
| DuckDB `count(*)` (warm) | 2.7 s | 2.9 s |
| DuckDB `sum(1 col)` (warm) | **176 s** | 224 s |

Direct AWS is as-good-or-better on every robust metric and ~25–27% faster on the
throughput-bound cases (single-stream, full-column scan). Cloudflare edge caching
did **not** help — these large, rarely-requested files aren't hot, so the proxy is
just an extra hop to the same origin. Conclusion: **map to the direct AWS bucket**
(which the fallback already does); the proxy buys nothing here.

### `s3-compute-bench.py` + `k8s/s3-compute-job.yaml`

Compute-scaling benchmark for the DuckDB+httpfs read path (issue #250). Sweeps
representative MCP aggregates (`mean carbon / h0`, `mean carbon / h1` via h3
rollup, `sum(carbon)`, `sum(all cols)`) across a `THREADS` sweep and reports
**uncompressed GB/s + Mrow/s** (not compressed-byte MB/s). Near-linear thread
scaling ⇒ compute-bound; flat ⇒ I/O-bound. Sets `memory_limit` explicitly.

```bash
MRE_THREADS=4,8,16,32,48 MRE_MEM=8GB uv run --with duckdb python3 s3-compute-bench.py
# endpoint defaults to cirrus internal MinIO; override MRE_ENDPOINT / MRE_SSL for NRP Ceph
```

### `s3-decomp-bench.py` + `k8s/s3-decomp-job.yaml`

Decomposes throughput into transport vs range-requests vs decode: boto3 whole-file
vs DuckDB `read_blob` (httpfs transport, same work) vs `read_parquet sum(all cols)`
(parallel ranged read+decode) vs a local floor. Shows `read_blob` is slow/non-parallel
(~95 MB/s) while `read_parquet` parallelizes — i.e. the transport "gap" doesn't gate
real queries.

### `local-read-bench.py` + `k8s/linstor-read-job.yaml`

Local-filesystem read benchmark (issue #258): DuckDB reading Parquet off a mounted
volume (LINSTOR NVMe / CephFS) instead of `s3://`, to test bypassing the NRP RGW
read ceiling for hot datasets. Same #250 methodology (valid probes, uncompressed
GB/s + Mrow/s, explicit `memory_limit`). The k8s Job provisions a `linstor-sdsu`
PVC (RWO), an **initContainer copies the carbon hex S3→PVC once**, then the main
container runs the local-file thread-sweep. Point `storageClassName` at
`rook-cephfs` (RWX) for the multi-replica serving comparison. GPU nodes are fine
(tolerate `nvidia.com/gpu`, don't request one). Give the copy initContainer explicit
resources — the ns `LimitRange` defaults to 1Gi and OOM-kills it otherwise.

```bash
kubectl apply -f k8s/linstor-read-job.yaml
kubectl logs -l jn=linstor-read -f --request-timeout=30s
kubectl delete job linstor-read; kubectl delete pvc linstor-carbon
```

Result (SDSU `linstor-sdsu`, 4.84B-row carbon hex): **mean-carbon/h0 = 2.5s warm
vs 77.8s on NRP S3/RGW (~31×), beating cirrus on-box MinIO (5.1s)** — the ~120ms
RGW GET-latency wall and ~20 Gb/s ceiling vanish. Once removed, queries revert to
compute/decode-bound (h3 rollup / 7-col reads scale only 2–3× with threads). Caveat:
warm numbers are OS-page-cache-served (the hot-dataset steady state), not raw NVMe;
a clean cold-NVMe ceiling needs O_DIRECT / `drop_caches`.

`k8s/cephfs-read-job.yaml` is the same job on a `rook-cephfs` **RWX** PVC (the
multi-replica-serving comparison). Same node, **CephFS warm ≈ LINSTOR** (mean-carbon/h0
2.6s vs 2.5s; h1 11.8s vs 11.9s; sum-all 23.8s vs 24.1s) — no throughput penalty for the
RWX filesystem path. CephFS's only cost is a heavier **one-time cold first-touch**
(mean-carbon/h0 T=8 = 238.5s vs LINSTOR 66.6s — MDS metadata/cap negotiation over 122
files), which amortizes to zero on a long-lived pod. **Decision: CephFS-RWX for the
6-replica serve path** (mountable by all replicas; RWO can't be); LINSTOR only for a
dedicated single-node builder if cold-start ever matters.

## Benchmarking methodology (issue #250)

**Report uncompressed GB/s + Mrow/s** — never compressed-byte MB/s (hex parquet
compresses ~4–6×, so compressed rates understate decode ~5×). Use real
data-reading queries (`sum`/`avg`/materialize). Determine rate-limiting via
thread-scaling (scales→compute-bound) and local-vs-remote (equal→compute-bound).
**Always `SET memory_limit`** (DuckDB sizes from host RAM → cgroup OOM otherwise),
and check `cat /sys/fs/cgroup/cpu.max` for the real core count (`cpu_count()` reports
the node, not the pod quota). On NRP, `opportunistic` Jobs can be CPU-starved — a
compute-bound query that took 11s on dedicated CPU took 420s on a contended
opportunistic pod (`kubectl top` showed 0.19 of 16 requested cores).

**Invalid probes — never use to measure read throughput:**
- `count(*)` → parquet row-count metadata; reads ≈ no data.
- `count(COLUMNS(*))` → per-column null-count metadata; reads ≈ no data (produced a
  bogus "~180 MB/s ceiling" early on — it was reading footers, not data).
- `read_blob` as a query-path proxy → sequential whole-file transfer that doesn't
  parallelize across files; `read_parquet` is 13–23× faster over the same httpfs.

## Key findings

1. Always include `h0` in join conditions (e.g., `ON p.h8 = c.h8 AND p.h0 = c.h0`) — this is what enables DPP file-level pruning.
2. Set `s3_allow_recursive_globbing=false` on DuckDB 1.5.0 when querying partitioned data on S3 to avoid reading all files at planning time.
3. A static `WHERE c.h0 = X` literal is ~9s faster and ~200 fewer GETs than join-driven DPP for single-partition queries, due to build-side materialization overhead — but both open only 1 file.
4. Queryable hex datasets are 1 file per h0 (carbon 122, padus 21, gbif-2026 122), so a pruned query opens 1 footer and a global scan ≤122. The historical "~923 files / ~126-per-h0" was a since-fixed GBIF over-sharding bug, not steady state — large scans are bandwidth-bound, not open-latency-bound. This retires the per-object-latency framing in older notes.
5. For the source.coop mirror, read the **direct AWS bucket** (`us-west-2.opendata.source.coop` → AWS S3), not the `data.source.coop` Cloudflare proxy: direct is ~25–27% faster on throughput-bound reads and the CDN provides no caching benefit for these large, cold objects. The #260/#261 fallback already maps to the direct bucket.
6. **Real queries are compute/decode-bound, not network/httpfs-bound** — S3 ≈ local ≈ internal-MinIO for heavy aggregates; every shape (scans, joins, h3 rollups) scales ~linearly with threads. DuckDB local/httpfs parquet reads are GB/s-class uncompressed and competitive with Polars / faster than PyArrow.
7. **Column pruning is the ~10× lever**: `sum(carbon)` (1 col) ≈ 873 Mrow/s vs `sum(all 7)` ≈ 471 Mrow/s. Far bigger than thread/network tuning. Column selection is governed by the registration SQL — recipes should select only the h3 index + value column(s).
8. **`THREADS=48` is well-chosen**: 1-column queries saturate at ~32 threads; multi-column / h3 queries still benefit past 48. Pushing higher raises connection fan-out risk (#103) under concurrency. (Full 4.84B-row carbon hex: mean-carbon/h0 in ~5s, /h1 in ~7s at T=48 on a dedicated machine.)

See `../query-optimization.md` for the actionable rules derived from these findings.
