# DGX Spark GPU benchmark — handoff runbook

Runbook for continuing the GPU query-engine test on an **NVIDIA DGX Spark**
(GB10 Grace Blackwell). Written for a Claude agent on the Spark to pick up after
`git pull` on branch **`gpu-query-engine`**. Background/design:
[gpu-query-engine.md](gpu-query-engine.md); prior results: `benchmarks/README.md`.

**Status: run to completion (2026-07) on node `nimbus`.** Result: the
hypothesis holds — networked, the Spark GPU is worse than ever (extra network
hop on top of the transfer cost); but with the network removed (data staged to
local disk), **GPU wins 2–4× over DuckDB**, the first hardware point where
this workload favours the GPU engine. Full numbers in
`benchmarks/README.md` ("DGX Spark, node `nimbus`"). A standing deployment
(`k8s/nimbus-gpu-deployment.yaml`) is live at `gpu-mcp-nimbus.carlboettiger.info`.
The step-by-step below is kept for reproducing or extending the test; each
step notes what actually happened.

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
- `.github/workflows/docker-gpu.yml` — matrix build on native `ubuntu-latest`
  (amd64) and `ubuntu-24.04-arm` (arm64) runners, no QEMU. amd64 keeps its
  original tags (`:gpu`, `:gpu-dev`, `:gpu-<sha>`); arm64 publishes the
  parallel `:gpu-arm64`, `:gpu-arm64-dev`, `:gpu-arm64-<sha>` family (kept
  separate from a single multi-arch manifest — see gotcha 2 below). The image
  currently live at `gpu-mcp-nimbus.carlboettiger.info` was still built locally
  and pushed by hand (CI wiring landed after that deploy); a push to this
  branch will now produce `:gpu-arm64-dev` via CI going forward.
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
6. **The Spark (`nimbus`) is itself a k3s node — don't `docker run -p` on it.**
   A raw `docker run -d -p 8000:8000 ...` binds the container to *both*
   `0.0.0.0:8000` and `[::1]:8000`, but k3s/kube-proxy's own iptables rules
   intercept new IPv4 connections to that port first, silently routing them to
   a different (or no) backend — you'll get a generic FastAPI-shaped 404 from
   something else entirely, while IPv6 to the same port reaches your container
   fine (`curl -4` vs `curl -6` will disagree). Deploy as a real Kubernetes
   Deployment/Service instead — see `k8s/nimbus-gpu-deployment.yaml`, following
   the existing `nimbus-carbon-api` / `vllm-nimbus` pattern (namespace
   `default`, `runtimeClassName: nvidia`, `<app>-nimbus.carlboettiger.info`
   ingress). `docker-gpu.yml` now builds `:gpu-arm64*` natively on every push
   to this branch, so a fresh deploy can just `kubectl rollout restart` after
   CI finishes; the first deploy predated that CI wiring and pushed a
   locally-built image by hand instead (still useful if you need an
   uncommitted local change tested before CI runs):
   `docker tag mcp-data-server:gpu-arm64 ghcr.io/boettiger-lab/mcp-data-server:gpu-arm64 && docker push ...`
   (this box is already `docker login`'d to `ghcr.io`).
7. **`h0` is a partition key, not a row-level unique key.** Every row in a
   given `h0=<id>/data_0.parquet` file shares the same `h0`. A join like
   `... JOIN ... ON c.h0 = g.h0` **before aggregating** is a near-Cartesian
   product (every carbon row × every gbif row sharing that `h0`) and will run
   away — DuckDB's own progress bar climbing past 20+ minutes with no sign of
   finishing is the tell. Aggregate each side down to one row per `h0` first
   (`WITH cc AS (SELECT h0, SUM(...) FROM ... GROUP BY h0), gg AS (...) SELECT
   ... FROM cc JOIN gg ON cc.h0 = gg.h0`), *then* join. If you do launch a
   runaway query via `kubectl exec`, killing the local `kubectl exec` process
   does **not** kill the remote process in the pod — `kubectl exec ... ps aux`
   and `kill -9` the PID directly, or the pod will keep burning CPU/GPU memory.

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

## Step 3 — networked case (data stays on cirrus MinIO) — done

Deployed via `k8s/nimbus-gpu-deployment.yaml` (Deployment + Service + Ingress,
namespace `default`, per gotcha 6 above — **not** a raw `docker run -p`):

```bash
docker tag mcp-data-server:gpu-arm64 ghcr.io/boettiger-lab/mcp-data-server:gpu-arm64
docker push ghcr.io/boettiger-lab/mcp-data-server:gpu-arm64   # this box is already `docker login`'d to ghcr.io
kubectl apply -f k8s/nimbus-gpu-deployment.yaml
kubectl wait --for=condition=Ready pod -n default -l app=mcp-gpu-nimbus --timeout=270s
curl -s https://gpu-mcp-nimbus.carlboettiger.info/healthz
```

Then the harness (CPU = cirrus's public DuckDB server; GPU = this Spark):

```bash
BENCH_CPU_URL=https://duckdb-mcp.carlboettiger.info \
BENCH_GPU_URL=https://gpu-mcp-nimbus.carlboettiger.info \
BENCH_REPS=3 uv run --with requests benchmarks/cpu-vs-gpu-bench.py
```

**Result: GPU lost badly** — worse than cirrus's own discrete-VRAM GPU (see
`benchmarks/README.md` "DGX Spark" networked table: 0.03–0.30× speedup, i.e.
3–33× *slower* than CPU). **Fairness caveat confirmed as decisive, not
marginal:** the Spark reads MinIO over the public internet while cirrus's CPU
server reads it loopback — the extra network hop dominates everything else.
This is not the test that isolates compute; proceed to Step 4.

## Step 4 — direct-read case (remove the network) — done

Staged carbon (and gbif, for the join case) partitions onto the pod's local
disk via `kubectl exec`, then timed DuckDB vs `GPUEngine` reading the same
local files — no network, unified memory in play:

```bash
kubectl exec -n default deploy/mcp-gpu-nimbus -- python3 -c "
import duckdb, os
c = duckdb.connect()
c.execute(\"INSTALL httpfs; LOAD httpfs; SET s3_endpoint='minio.carlboettiger.info'; SET s3_use_ssl=true; SET s3_url_style='path';\")
# COPY (SELECT * FROM read_parquet('s3://public-carbon/irrecoverable-carbon-2024/hex/h0=<id>/data_0.parquet')) TO '/tmp/carbon/<id>.parquet' (FORMAT PARQUET)
"
# then, per engine, GROUP BY h5, SUM(carbon) over read_parquet('/tmp/carbon/*.parquet')
# (DuckDB) vs pl.scan_parquet(...).collect(engine=GPUEngine(raise_on_fail=True)) (GPU).
```

**Result: flips completely.** GPU wins 2–4× at every scale tested (4 and 20
partitions, plus a join-of-aggregates) — see `benchmarks/README.md` "DGX
Spark" direct-read table. This confirms the hypothesis: unified memory removes
the host→VRAM transfer that dominated the loss on cirrus's discrete Turing
card. Also surfaced two dialect gaps along the way, now enforced in
`sql_translate.guard_unsupported`: `RANK()`/`ROW_NUMBER()`/etc. have no Polars
equivalent at all (CPU or GPU), while `SUM(...) OVER (...)` works fine on
`polars-cpu` and only fails on `GPUEngine`'s collect — see
gpu-query-engine.md's dialect-subset section. Also see gotcha 7 above for the
`h0`-join pitfall hit while building the join test case.

## Step 5 — record results — done

Results are recorded in `benchmarks/README.md` ("DGX Spark, node `nimbus`")
and summarized at the top of this file. **GPU wins on the Spark** — this
reframes the effort: a Blackwell/unified-memory deploy target is a genuine win
for this workload, not just a discrete-GPU dead end. The standing deployment
(`gpu-mcp-nimbus.carlboettiger.info`) stays up for further testing. The two
dialect gaps found (ranking functions unconditionally, aggregate window
functions on GPU only) are now enforced in `sql_translate.guard_unsupported`,
documented in gpu-query-engine.md's SQL subset section, tested in
`tests/test_engines.py`, and surfaced to the model via the engine-aware
`query` tool docstring (`server._engine_dialect_note`).

## CI now builds the arm64 image

`.github/workflows/docker-gpu.yml` runs a matrix over `ubuntu-latest` (amd64)
and `ubuntu-24.04-arm` (arm64, GA 2025, free for public repos, no QEMU),
building `Dockerfile.gpu` on each and pushing `:gpu-arm64` / `:gpu-arm64-dev` /
`:gpu-arm64-<sha>` alongside the untouched amd64 `:gpu*` tags — kept as a
separate tag family rather than a single multi-arch manifest (gotcha 2). This
was left undone during the initial design because it couldn't be
runtime-verified without Blackwell hardware; it's now verified end-to-end
against the real Spark deploy in `k8s/nimbus-gpu-deployment.yaml`.
