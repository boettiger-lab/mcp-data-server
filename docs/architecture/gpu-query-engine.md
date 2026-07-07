# GPU query engine: an optional, deploy-time backend

Design note for folding the GPU-accelerated query path into this repo as an
**optional backend selected at deploy time**, replacing the diverging hard fork
at [`boettiger-lab/mcp-gpu-data-server`](https://github.com/boettiger-lab/mcp-gpu-data-server).
Tracks [#227](https://github.com/boettiger-lab/mcp-data-server/issues/227)
(supersedes #64) and closes the loop on the benchmark work in
[#42](https://github.com/boettiger-lab/mcp-data-server/issues/42).

Status: **design / in progress.** This is the north star for the branch; the
implementation lands as a short PR series (see [PR plan](#pr-plan)).

## Goals and non-negotiables

- **DuckDB stays the default, byte-for-byte.** `QUERY_ENGINE` is unset →
  identical behaviour to today. The DuckDB code moves modules but nothing about
  what it does changes. Zero risk to existing deployments.
- **GPU dependencies are optional.** `cudf-polars` / `kvikio` are never in the
  base image or `requirements.txt`; only a separate GPU image installs them.
- **Single source of truth.** One codebase for STAC, the docs, and the h3/SQL
  surface. End the two-drifting-codebases problem — archive the fork once this
  reaches parity.
- **Open the door to a new experimental deployment** from this one codebase: a
  server tuned for large compute-bound joins on a GPU node, fed by on-node
  MinIO (the cirrus hairpin removes the S3 read ceiling that made a GPU node
  pointless — see the sibling CPU deploy and the k8s `node-placement` note).

## Why an engine abstraction, not a 13-line hook

#64 proposed a thin `try: import query_backend` hook. It was never adopted, and
the reason is instructive: the real coupling between the two paths is **not** the
`execute()` call. It is the **SQL dialect + the S3 credential model**.

- The fork carries a whole `sql_rewriter.py` because Polars `SQLContext` + custom
  h3 handling is a *different SQL surface* than DuckDB + the community `h3`
  extension. `read_parquet('s3://…')` is an inline table function in DuckDB; in
  Polars every source must be pre-registered as a `LazyFrame`.
- This repo's `get_isolated_db()` has a rich per-request S3 model (a
  deployment-default secret, per-source scoped secrets, and a per-request
  `client_s3` for bring-your-own bucket / anonymous mirror with `s3_scope`
  longest-prefix routing). The fork threw all of that away for a single flat
  `storage_options` dict.

A 13-line import hook can't bridge a dialect and a credential model. An explicit
engine seam can — and, done right, it lets the Polars path *keep* this repo's
BYO-bucket / scope semantics rather than regress them.

## Module layout

```
engines/
  __init__.py        select_engine() reads QUERY_ENGINE once, returns a singleton
  base.py            QueryEngine ABC, S3Request dataclass, shared result rendering
  duckdb_engine.py   today's get_isolated_db() + query() body, moved verbatim
  polars_engine.py   Polars SQLContext + cudf-polars GPU + kvikio (new; fork ideas)
  sql_translate.py   DuckDB→Polars dialect translation + per-path S3 resolution + DPP
```

`server.py` keeps everything engine-agnostic: MCP tool registration, the
`query` docstring (with the existing `TOOL_INJECTED_CONTEXT` hook), the async
offload onto a worker thread, and the `_QUERY_LIMITER` `CapacityLimiter`. The
tool body becomes, in essence, `engine.run(sql, s3req)`.

### The seam

```python
@dataclass(frozen=True)
class S3Request:
    """Per-request S3 intent, engine-agnostic."""
    s3_key: str | None = None
    s3_secret: str | None = None
    s3_endpoint: str | None = None
    s3_scope: str | None = None
    # the source registry (s3config.get_sources) is read by each engine

class QueryEngine(ABC):
    name: str
    @abstractmethod
    def run(self, sql_query: str, s3: S3Request) -> str:
        """Execute and return a markdown preview (or 'SQL Error: …')."""
```

**Interchange:** engines produce an Arrow table already limited to 51 rows;
`base.py` does geometry-column drop + markdown + the truncation notice **once**.
(DuckDB keeps its geometry drop *before* Arrow conversion, since
`GEOMETRY('OGC:CRS84')` still crashes pandas/Arrow conversion — that stays
engine-local. Polars/cuDF carry geometry as WKB and are dropped by the shared
name/dtype heuristic.)

The engine-agnostic plumbing that wraps *any* engine: the async offload +
`CapacityLimiter`, the 50-row preview limit + truncation detection, markdown
rendering, and the credential / `s3_scope` handling described below.

## Engine modes (`QUERY_ENGINE`)

| Value | Reader | Compute | GPU? | Notes |
|-------|--------|---------|------|-------|
| `duckdb` *(default)* | DuckDB httpfs | DuckDB | no | Unchanged. Hex tiles available. |
| `polars-cpu` | Polars `scan_parquet` | Polars CPU | no | Runs in ordinary CI. Fallback target. |
| `polars-gpu` | Polars lazy `scan_parquet` | cudf-polars `GPUEngine` | yes | Isolates whether kvikio is the win. |
| `polars-gpu-cudf` | kvikio parallel `pread` (large files) + Polars parse | cudf-polars `GPUEngine` | yes | GPU-direct S3 I/O + explicit partition pruning. |

All three Polars modes share `polars_engine.py` + `sql_translate.py`; they differ
only in the I/O path and whether `GPUEngine()` is used for `collect()`.
`polars-cpu` is the workhorse for CI parity tests — no GPU required.

## Hard problems and proposed resolutions

### 1. S3 credential model (the biggest piece of work)

DuckDB's scoped-SECRET model (longest-prefix match) has no Polars analogue.
But Polars registers each `read_parquet` path as its own `LazyFrame`, which can
take **per-path** `storage_options`. So `sql_translate` resolves each extracted
path against `{deployment default endpoint, source registry, per-request
client_s3 + s3_scope}` and hands each `LazyFrame` the correct options. This
**preserves** the repo's BYO-bucket / anonymous-mirror / scope semantics that
the fork dropped — it is the real cost of the port, and where most correctness
tests will live.

### 2. SQL dialect — GPU mode is an explicit subset

Polars SQL is a narrower surface than DuckDB. Known gaps (some already handled
by the fork): the community `h3` extension functions (`h3_cell_to_parent`,
`h3_h3_to_string`, …) are unavailable; `APPROX_COUNT_DISTINCT` rewrites to
`COUNT(DISTINCT …)`; no `GEOMETRY`/spatial ops. Confirmed on the DGX Spark
(RAPIDS 25.10 / cudf-polars 25.10 / polars 1.32.3, see
[gpu-spark-handoff.md](gpu-spark-handoff.md)): **window functions are also
unsupported** by `GPUEngine` — `RANK() OVER (...)` fails with
`SQLInterfaceError: unsupported function 'rank'`, and even a plain
`SUM(...) OVER (...)` fails with `NotImplementedError: No support for ...
UnaryFunction in groupby/rolling`. Both fail loudly (no silent CPU fallback
inside the GPU engine itself), consistent with the failure policy below.

**Contract:** GPU mode supports a documented subset — pre-computed `h0…h11`
columns only, no `h3_*` functions, no window functions, no spatial. Surfaced to
the model through the engine-aware `query` docstring so the LLM writes
GPU-compatible SQL up front.

### 3. Failure policy — two distinct classes *(proposed; open for discussion)*

- **Dialect / capability miss** (SQL uses something GPU mode can't express, e.g.
  `h3_cell_to_parent`): **fail loudly** with an actionable message ("use
  pre-computed h3 columns; for cross-resolution joins pick the coarser shared
  column"). Falling back to DuckDB here would mean shipping full DuckDB + httpfs
  in the GPU image and re-running the whole query against a second S3 setup —
  defeating the point. A loud error teaches the model to rewrite.
- **GPU-runtime failure** (VRAM OOM, a plan node `cudf-polars` can't execute):
  **fall back to `polars-cpu`** — same dialect, correctness-preserving, just
  slower — behind `ALLOW_CPU_FALLBACK` (default on; set false to surface errors
  when benchmarking). This mirrors the fork's `ALLOW_CPU_FALLBACK`, but the
  fallback target is CPU-Polars (same dialect), **not** DuckDB.

### 4. Hex tiles — a deploy flag, not a hard-wired answer

Hex-tile tools (`register_hex_tiles`, `get_hex_tile_status`, the MVT endpoint)
run on a **separate** DuckDB connection (`tiles/db.build_tile_connection()`) and
depend on DuckDB's `h3` extension + MVT generation that Polars/cuDF can't
provide. `QUERY_ENGINE` therefore only ever touches the `query` tool. Rather
than bake in #227's open question, gate the tile tools behind
`ENABLE_HEX_TILES` (default: true for `duckdb`, false for GPU). The GPU node
ships **query-only**; a deployment that wants DuckDB-backed tiles can still
enable them (DuckDB stays a dependency regardless).

## kvikio / cuDF I/O notes (carried from the fork's hard-won lessons)

- `kvikio.defaults.set_num_threads()` / `set_task_size()` are **broken in 25.02**
  — they no-op. Set `KVIKIO_NTHREADS=64` and `KVIKIO_TASK_SIZE=16777216` in the
  deployment env *before* library init.
- **Small files (< ~5 MB avg) route through Polars**, not kvikio — kvikio's
  per-connection overhead dominates below that; kvikio wins on large files
  (benchmarked ~6× on 78 MB avg parquet).
- `pl.scan_parquet(..., hive_partitioning=True)` produces a scan node
  `cudf-polars` can't execute (pola-rs/polars#20577) and silently falls to CPU.
  Read files individually and inject the `h0` partition value from the path.
- Use `pl.read_parquet(BytesIO)` (CPU parse) **not** `cudf.read_parquet` for the
  kvikio path: cuDF materialises the whole table in VRAM eagerly and OOMs on
  large datasets; Polars keeps it in CPU RAM and only the filtered result goes
  to VRAM via `GPUEngine()`.
- `gpu-cudf` mode needs **explicit partition pruning** (extract `h0 IN (…)` /
  `h0 = …` from the SQL, filter the file list before reading). The lazy Polars
  path gets this for free.
- Free VRAM after every query (`gc` + `cupy` pool `free_all_blocks()`).

## Deps, image, CI

- `polars` lives in the **base** `requirements.txt`: it is a pure-CPU dependency
  that powers `polars-cpu` (CI parity + the GPU-failure fallback), so it belongs
  everywhere. `requirements-gpu.txt` holds only the true GPU deps —
  `cudf-polars`, `kvikio`, `s3fs` — and is **never** installed by the base image.
- `Dockerfile.gpu`: `FROM nvcr.io/nvidia/rapidsai/base:<cuda12>` , conda-install
  `kvikio` pinned to the matching CUDA/py build, then `pip install` the repo's
  base requirements + `requirements-gpu.txt`. The default `Dockerfile` and the
  base image are untouched.
- A **separate** CI workflow builds/publishes the GPU image on this branch and
  tags. The default image pipeline does not change.

## Testing

- **Unit** (`sql_translate`): dialect rewrites, `read_parquet` path extraction,
  per-path **S3 scope resolution**, `h0` DPP predicate extraction. Pure, no GPU.
- **Parity:** a representative query corpus run through `duckdb` and
  `polars-cpu`, asserting equal results (modulo row order / the 50-row preview).
  Runs in ordinary CI — **no GPU needed**, and it's the main correctness net.
- **GPU smoke + benchmark (#42):** on a cirrus GPU slice against on-node MinIO,
  head-to-head vs DuckDB. Gated off PR CI (self-hosted / manual). This produces
  the numbers that justify the effort and settle the read-path questions
  (kvikio vs object_store; internal `minio-svc` vs the hairpin endpoint).

## Deployment (follow-up, mirrors the CPU deploy)

New `k8s/cirrus-gpu-*.yaml`, mirroring the live CPU deploy: namespace `mcp`,
`nodeSelector: cirrus`, `runtimeClassName: nvidia`, in-cluster MinIO endpoints,
`QUERY_ENGINE=polars-gpu-cudf`, the kvikio env vars, `ENABLE_HEX_TILES=false`,
and a low `MCP_QUERY_CONCURRENCY`. Reuse the existing
`gpu-mcp.carlboettiger.info` ingress.

**GPU slices:** cirrus has 2× Quadro RTX 8000 (48 GB, Turing) time-sliced into
16 slots (`device-plugin.config: timeslice`). Time-slicing here is used only to
**cap how many slots k8s hands this deployment** — request a few, leave headroom.
There is no VRAM isolation between slices, but for this experimental single-user
node the collision probability is low and not a concern; we do not design around
contention. (If that changes, MPS or a different sharing config would be the
lever — out of scope here.)

## Multi-arch / NVIDIA DGX Spark — done, GPU wins here

A GPU image that runs on an **NVIDIA DGX Spark** (GB10 Grace Blackwell,
node `nimbus`) was the later goal; it's now built, deployed, and benchmarked.
Note the DGX Spark is **aarch64** (Grace ARM CPU) with a Blackwell GPU (sm_12x)
and unified memory — so this means an **arm64** image, not amd64 (an x86 image
will not run on it). Implications encountered:

- The RAPIDS base must have an **arm64** tag *and* support Blackwell — i.e. a
  CUDA-13-class / recent RAPIDS release. Verify both before picking a tag.
- `docker-gpu.yml` would build multi-arch (`linux/amd64,linux/arm64`). GitHub now
  has **native arm64 hosted runners** (`ubuntu-24.04-arm`, GA 2025, free for
  public repos), so build the arm64 variant natively there — no slow QEMU
  emulation. Likely a matrix over runner+platform, each pushing its arch, joined
  by a manifest, or the arm build publishing an `-arm64` tag.
- Unified memory changes the VRAM calculus (no separate device memory ceiling),
  which may relax the concurrency/VRAM guards we set for the discrete RTX 8000.

The RAPIDS base (`25.10-cuda12.9-py3.12`) is **already multi-arch (amd64 +
arm64)**, so `Dockerfile.gpu` builds on an arm64 host unchanged. The DGX Spark's
**unified memory** is the reason it was worth testing: on cirrus the GPU loss
is dominated by host→VRAM transfer, which unified memory removes. **Result:
confirmed** — networked (reading remote MinIO over the internet) the Spark GPU
is worse than ever, but with the network removed (data staged to local disk),
GPU wins 2–4× over DuckDB at every scale tested. This is the first hardware
point where the GPU engine wins outright on this workload. Deployed at
`gpu-mcp-nimbus.carlboettiger.info` via `k8s/nimbus-gpu-deployment.yaml`; full
results in `benchmarks/README.md`, full runbook and gotchas (including a k3s
node/`docker run -p` networking trap and an `h0`-join Cartesian-product
footgun) in **[gpu-spark-handoff.md](gpu-spark-handoff.md)**. `docker-gpu.yml`
still doesn't build arm64 natively — the deployed image was built locally on
`nimbus` and pushed to `ghcr.io/boettiger-lab/mcp-data-server:gpu-arm64` by
hand; wiring CI (native arm64 runner or buildx multi-arch) is still open.

## PR plan

1. **Engine seam + `polars-cpu`.** Introduce `engines/`, move DuckDB into
   `duckdb_engine.py` (behaviour-preserving), add `sql_translate.py` +
   `polars_engine.py` in CPU mode, wire `QUERY_ENGINE`. Ships with the unit +
   parity test suite. No GPU deps, fully CI-testable. Default path unchanged.
2. **GPU modes + image.** `polars-gpu` / `polars-gpu-cudf`, `requirements-gpu.txt`,
   `Dockerfile.gpu`, the separate GPU image CI workflow. GPU smoke test gated.
3. **Deploy + benchmark.** `k8s/cirrus-gpu-*.yaml`, head-to-head benchmark
   harness and standing results (#42). Archive the fork once parity is verified.
