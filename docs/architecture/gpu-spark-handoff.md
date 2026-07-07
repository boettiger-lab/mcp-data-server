# DGX Spark GPU benchmark — handoff runbook

Runbook for continuing the GPU query-engine test on an **NVIDIA DGX Spark**
(GB10 Grace Blackwell). Written for a Claude agent on the Spark to pick up after
`git pull` on branch **`gpu-query-engine`**. Background/design:
[gpu-query-engine.md](gpu-query-engine.md); prior results: `benchmarks/README.md`.

## Why we're doing this (the hypothesis)

On cirrus (Quadro RTX 8000, Turing, discrete VRAM) the GPU engine
(`polars-gpu-cudf`) is **3–6× slower than DuckDB** on hex-aggregation queries —
even reading local NVMe files, even page-cached. The penalty is dominated by
**host→VRAM transfer + per-query cudf-polars setup**, not the read. (GPU-Direct
Storage isn't available: `nvidia-fs` absent, and Turing's GDS support is limited.)

The DGX Spark attacks exactly that bottleneck:
- **Unified memory** (CPU+GPU share LPDDR5X) → little/no host→VRAM copy.
- **Blackwell** GPU, far newer/faster than the 2018 Turing.

So the Spark is the real test of whether the GPU path is *ever* worth it for this
workload. Run **networked first, then direct read** (below).

## What already exists (all on branch `gpu-query-engine`)

- `engines/` — the `QUERY_ENGINE` seam: `duckdb` (default), `polars-cpu`,
  `polars-gpu`, `polars-gpu-cudf`. GPU reads via `_scan_cudf` (s3fs glob → h0
  DPP → kvikio pread → Polars parse → GPU compute); falls back to host read.
- `Dockerfile.gpu` — `FROM nvcr.io/nvidia/rapidsai/base:25.10-cuda12.9-py3.12`
  (**verified multi-arch: amd64 + arm64** — builds on the Spark unchanged),
  conda-installs `kvikio=25.10.*`, pip-installs app deps (minus polars) + s3fs.
- `.github/workflows/docker-gpu.yml` — builds the amd64 GPU image (`:gpu-dev`,
  `:gpu-<sha>`). arm64 is **not** wired yet — see "Building on the Spark".
- `benchmarks/cpu-vs-gpu-bench.py` — the CPU-vs-GPU harness (carbon/gbif suite).
- `k8s/cirrus-gpu-*.yaml` — the cirrus (amd64) GPU deploy manifests.

## Arch-management gotchas (read before starting)

1. **Endpoints differ from the cirrus manifests.** Those use the in-cluster
   MinIO (`minio-svc.minio.svc.cluster.local:9000`, plain http) — only resolvable
   *inside* the k3s cluster. A standalone Spark must use the **public** MinIO:
   `https://minio.carlboettiger.info` (https, so no `S3_DEFAULT_USE_SSL=false`).
2. **Don't clobber the amd64 `:gpu` tag** with an arm64 image. Tag arm64 builds
   `:gpu-arm64` (or build a real multi-arch manifest with buildx). Keep them
   distinct until a multi-arch manifest is deliberately published.
3. **Verify Blackwell on-device** before trusting results (sm_12x is new). RAPIDS
   25.10 / CUDA 12.9 *should* support it; confirm with the sanity check below. If
   cudf errors on the GPU arch, try a newer RAPIDS (25.12/26.x) or a cuda13 base.
4. **kvikio on aarch64**: `kvikio=25.10.*` must resolve on the rapidsai conda
   channel for aarch64 (it should — RAPIDS ships Grace/ARM builds). If it doesn't,
   drop kvikio and use `polars-gpu` (host read) — unified memory makes the kvikio
   I/O optimization far less important anyway.
5. Unified memory changes the RAM story: the 32Gi host-RAM cap and
   `CUDA_VISIBLE_DEVICES` pin from the cirrus manifest are **not** needed on the
   single-GPU Spark.

## Step 1 — build on the Spark

```bash
git pull                              # branch gpu-query-engine
docker build -f Dockerfile.gpu -t mcp-data-server:gpu-arm64 .   # pulls arm64 base
```

## Step 2 — sanity checks (mirror what we did on Turing)

```bash
docker run --rm --gpus all mcp-data-server:gpu-arm64 nvidia-smi
# cudf-polars GPU compute on Blackwell + kvikio state:
docker run --rm --gpus all mcp-data-server:gpu-arm64 python -c "
import polars as pl, cudf_polars, kvikio, kvikio.defaults
from polars import GPUEngine
lf = pl.LazyFrame({'g':['x','y','x'],'v':[1,2,3]}).group_by('g').agg(pl.col('v').sum())
print(lf.collect(engine=GPUEngine(raise_on_fail=True)))   # must run on GPU, not error
print('polars', pl.__version__, 'cudf_polars', cudf_polars.__version__)
print('kvikio', kvikio.__version__, 'compat_mode', kvikio.defaults.compat_mode())
"
```
If `GPUEngine(raise_on_fail=True)` succeeds, Blackwell is good. Also confirm a
bare `SELECT COUNT(*)` via SQLContext returns 1 row (polars 1.32 fixed the 1.21
bug, but re-verify the shipped version).

## Step 3 — networked case (data stays on cirrus MinIO)

Run the server pointed at the **public** MinIO, GPU mode, fallback OFF:

```bash
docker run -d --gpus all -p 8000:8000 --name gpu-spark \
  -e QUERY_ENGINE=polars-gpu-cudf \
  -e ENABLE_HEX_TILES=false \
  -e ALLOW_CPU_FALLBACK=false \
  -e STAC_ALLOW_DEGRADED_START=true \
  -e STAC_CATALOG_URL=https://minio.carlboettiger.info/public-data/stac/catalog.json \
  -e S3_ENDPOINT_URL=https://minio.carlboettiger.info \
  -e S3_DEFAULT_ENDPOINT=minio.carlboettiger.info \
  mcp-data-server:gpu-arm64
# check: curl -s localhost:8000/healthz ; docker logs gpu-spark | grep -i engine
```

Then run the harness (CPU = cirrus's public DuckDB server; GPU = this Spark):

```bash
BENCH_CPU_URL=https://duckdb-mcp.carlboettiger.info \
BENCH_GPU_URL=http://localhost:8000 \
BENCH_REPS=3 uv run --with requests benchmarks/cpu-vs-gpu-bench.py
```

**Fairness caveat:** the Spark reads MinIO over the network while cirrus's CPU
server reads it loopback — so the Spark GPU is *handicapped on read* here. If it
still wins (or ties), that's a strong signal; if it loses, the direct-read case
isolates compute.

## Step 4 — direct-read case (remove the network)

Stage the carbon partitions onto the Spark's local disk, then benchmark local
file reads (this is where unified memory + no-network should most favour the GPU):

```bash
# inside a container or a python env with s3fs+polars+duckdb+cudf-polars:
#   download s3://public-carbon/irrecoverable-carbon-2024/hex/h0=<id>/data_0.parquet
#   (a few partitions, ~54MB each) to /tmp/carbon/, then time the same
#   GROUP BY h5, SUM(carbon) with DuckDB vs pl.read_parquet(local)+GPUEngine.
```
Compare against the cirrus local-read numbers in `benchmarks/README.md`
(DuckDB 0.17s vs GPU 1.27s for 216MB). The question: does unified memory close
or flip that gap?

## Step 5 — record results

Append a "DGX Spark" results block to `benchmarks/README.md` (same table shape),
note the RAPIDS/polars/cudf versions and whether it was networked or direct, and
commit on `gpu-query-engine`. If GPU wins on the Spark, that reframes the whole
effort (a Blackwell/unified-memory deploy target); if it still loses, the
"DuckDB wins for this workload" conclusion is robust across three hardware points.

## If you want CI to build the arm64 image

Add a job to `.github/workflows/docker-gpu.yml` on a native arm64 runner
(`runs-on: ubuntu-24.04-arm` — GA 2025, free for public repos), building
`Dockerfile.gpu` and pushing `:gpu-arm64` / `:gpu-arm64-<sha>`. Or use buildx
`--platform linux/amd64,linux/arm64` to publish a single multi-arch `:gpu`
manifest. Left undone here because it can't be runtime-verified without Blackwell
hardware — build it once the Spark confirms the image runs.
