# GPU vs CPU query engine: the standing record

Canonical record for the DuckDB-CPU vs Polars/cuDF-GPU comparison. It exists because
the findings were spread over five issues in two repos, each asserting a conclusion at
the top that a later comment invalidated, and because the GPU results live in a fork
that #227 plans to archive. **Read this file first; use the issues for detail only.**

Related: #227 (fold the GPU path in as an optional `QUERY_ENGINE` backend — owns this
deliverable), #250 (DuckDB-side benchmark methodology), #42 (closed, superseded by #227).

## Where to look, and what not to trust

| Source | Holds | Caveat |
|---|---|---|
| `mcp-gpu-data-server#12` | **The only real GPU-vs-CPU results** (cirrus, 2× RTX 8000) | Titled "Local k3s deployment…", so it doesn't read like a benchmark issue |
| `mcp-gpu-data-server#5` | The GPU investigation narrative | **Title and body are superseded.** Its closing "GPU compute was never used" is true only of the runs *in that issue* |
| `mcp-gpu-data-server#4`, `#3`, `#1` | DPP + kvikio implementation detail | Closed, accurate |
| this repo #250 | DuckDB CPU methodology, invalid-probe list | Its "compute-bound" TL;DR is reversed by the 2026-07-01 comment for the NRP/Ceph path |
| `benchmarks/gpu/*.csv` | Raw numbers, migrated here from the fork | See generation table below for which are valid |

## Three benchmark generations

| # | Date | Hardware / storage | GPU actually engaged? | Verdict |
|---|---|---|---|---|
| 1 | 2026-03-24/25 | NRP RTX 4000 Ada (20 GB), NRP Ceph RGW | **No** — `GPUEngine` silently fell back to CPU on hive-partitioned scans | Invalid as a GPU measurement. Real finding: an S3-transport comparison (`results-full.csv`, `results-kvikio.csv`) |
| 2 | 2026-04-15 | cirrus, 2× Quadro RTX 8000 (48 GB), on-node MinIO/NVMe | **Yes** — verified with `ALLOW_CPU_FALLBACK=false` | Valid GPU numbers (`results_rtx8000.csv`). No matched CPU run |
| 3 | 2026-08-21 | cirrus, `duckdb-mcp` CPU deploy, same on-node MinIO | n/a (CPU) | Valid CPU numbers (`results-cirrus-cpu-2026-08-21.csv`), but on a drifted data model — see below |

What unblocked generation 2 was fork commit `Fix small-file path to return non-hive
LazyFrame for GPUEngine compatibility` (2026-03-26), which landed hours after #5's last
comment. The results were posted to #12, and #5 was never updated — the single biggest
source of confusion in this chain.

## Numbers

Generation 2 — GPU, cirrus, 3 runs, medians:

| Query | GPU median |
|---|---|
| Q3a — Americas carbon × IUCN | 23.5s |
| Q4a — Americas carbon × WDPA, `COUNT(DISTINCT SITE_ID)` | 205s cold / 120s warm |
| Q5a — Americas carbon × 2× IUCN | 24.5s |

(#12's prose calls Q5a "carbon × GBIF"; the suite's Q5a is carbon × combined_sr × birds_sr.
The CSV is authoritative.)

Generation 3 — CPU, cirrus, 3 runs, medians, via `gpu-cpu-cirrus-bench.py`:

| Query | CPU median | Rows returned |
|---|---|---|
| Q3a-c | **5.2s** | 1,153,131 |
| Q4a-c | **10.0s** | 21,847,557 |
| Q5a-c | **5.4s** | 1,148,328 |

## Why generation 3 is not a like-for-like rerun

The April suite **cannot be run as written** any more — the data model drifted:

- `s3://public-wdpa/hex/**` no longer exists; WDPA hex is now versioned
  (`wdpa/hex/**`, `wdpa-december-2025/hex/**`, `wdoecm-may-2026/hex/**`). `wdpa/hex`
  keeps `h8`/`h0` as integers, so Q4a is otherwise faithful.
- `s3://public-iucn/hex/*` was reprocessed and **lost `h8`** (now `h3`/`h4`/`h5`), so the
  original `h8` joins no longer bind. The h8-grain replacement is
  `s3://public-iucn/richness/hex/*`, which stores `h8`/`h0` as **h3 hex strings**, needing
  `h3_string_to_h3()` on the join keys and hex-string literals in the `h0` filter for
  partition pruning to survive. Same class of problem as the suite's `Q7` note.

So Q3a-c/Q5a-c join a different, smaller IUCN product than April's Q3a/Q5a and their
times are not comparable to 23.5s / 24.5s. **Q4a-c is the closest to like-for-like**
(same WDPA data, integer keys, same `COUNT(DISTINCT)` shape, 21.8M rows): 10.0s CPU vs
205s cold / 120s warm GPU on the same node against the same MinIO — a >10× CPU win,
consistent with the fact that cirrus serves the CPU deploy and the GPU deploy is down.

## What is actually established

1. **GPU compute works on this stack** once hive-partitioned scans are kept out of the
   plan (per-file read + manual `h0` injection). Generation 2 proves it.
2. **CPU (DuckDB) wins by a wide margin on these S3-backed join queries**, on every
   generation measured, on both NRP/Ceph and cirrus/MinIO.
3. **The mechanism is bytes transferred, not compute.** DuckDB range-requests only the
   column chunks it needs (~13% of bytes for a 2-of-15-column query) and streams the
   join; the kvikio path downloads whole files. `gpu/rapids_bug_reports.md` #4 records
   this as a "fundamental I/O gap vs DuckDB" — measured *with* the GPU engaged.
4. **Hive-partitioned scans are still unsupported upstream** (verified 2026-08-21):
   `NVIDIA/cudf` `main`, `python/cudf_polars/cudf_polars/dsl/translate.py` raises
   `NotImplementedError("Hive-partitioned scans are not supported")`. Tracked by
   `NVIDIA/cudf#17832` and `pola-rs/polars#20577`, both open; the cudf maintainer listed
   it as a pending follow-up on 2026-07-02, flagging the streaming case as the hard half.
   Always run benchmarks with fallback disabled, or a "GPU" number may be a CPU number.

## Picking this back up when a GPU is available

Everything needed to resume, so nothing depends on the fork (which #227 archives) or on
issue archaeology. Tracking issue: **#227** — this is its benchmark acceptance step.

### 1. Redeploy a GPU server

The last working config was `k8s/mcp/deployment.yaml` in the fork (cirrus, 2× Quadro
RTX 8000), reproduced here because an archived repo is easy to lose:

| Setting | Value | Why |
|---|---|---|
| `runtimeClassName` | `nvidia` | — |
| `nvidia.com/gpu` | `14` | Virtual slices; on cirrus this is what exposes *both* physical cards |
| `memory` limit | `128Gi` | Q4a peaked >64Gi materializing the join |
| `QUERY_ENGINE` | `gpu-cudf` | Beat `gpu` mode 31.7s vs 53.7s at matched threads |
| `POLARS_MAX_THREADS` | `64` | Sweet spot; >64 degrades end-to-end on 885M-row joins |
| `KVIKIO_NTHREADS` | `64` | Must be an **env var** — `set_num_threads()` silently no-ops |
| `KVIKIO_TASK_SIZE` | `16777216` | 16 MiB chunks |
| `ALLOW_CPU_FALLBACK` | **`false`** | Not in the old manifest (defaults `true`). Without it a "GPU" run may be a CPU run |
| `strategy` | `Recreate` | Rolling update deadlocks on a single-GPU node |
| `S3_ENDPOINT_URL` / `STAC_CATALOG_URL` | on-node MinIO | Keeps data traffic on-node, matching the CPU deploy |

The NRP variant additionally needs the `nautilus.io/issue` toleration and a GPU-node
`nodeSelector`; prefer a co-located store over NRP Ceph (~120ms RGW GET latency, #250).

### 2. Rewrite the queries first — the old suite will not bind

Non-optional, and the main reason a rerun isn't a one-liner. See the drift notes above:
WDPA hex moved to versioned prefixes, IUCN hex lost `h8` and its h8-grain replacement
stores h3 indices as hex strings. The rewrite must be executable by **both** engines —
Polars `SQLContext` forbids `CAST` in an `ON` clause, so casts belong in a CTE, and the
`h0` filter needs literals in each side's own key type or partition pruning dies.

### 3. Verify the GPU is actually computing, before trusting any number

1. `GPUEngine(raise_on_fail=True)` (or `ALLOW_CPU_FALLBACK=false`) so unsupported nodes
   raise instead of silently falling back.
2. Watch VRAM during a run — generation 1's tell was 0% utilization and 1 MiB VRAM.
3. Confirm no scan node carries `hive_parts`; that is still an upstream hard stop.

### 4. Run both sides and record

```bash
CPU_MCP_URL=<cpu endpoint> RUNS=3 python3 benchmarks/gpu-cpu-cirrus-bench.py
```

Add the GPU endpoint the same way (env-overridable — the fork's harness hardcoded the CPU
URL to NRP, which is why the matched baseline was missing for four months). Then update the
tables in this file. **Results belong here, not in an issue comment.**

### 5. If the box is a DGX Spark or similar unified-memory machine

That is the interesting case: with no PCIe hop and no separate VRAM budget, the
column-projection gap that decides every result above should mostly disappear. Treat it as
a distinct generation in the table rather than a rerun, and record the storage path
(on-box NVMe vs S3) since that, not the GPU, has dominated every result so far.

## Known gaps

- **No same-node, same-data GPU-vs-CPU pair.** Closing it needs either a re-run of the
  GPU deploy against the current data model, or a rewrite of the suite that both engines
  can run. Acceptance step under #227.
- **DGX Spark (unified memory)** was benchmarked but nothing was pushed to any
  boettiger-lab repo. That config is the one where the column-projection gap largely
  disappears, so it is the most interesting missing data point. If those numbers can be
  recovered from that host, add them as generation 4 (see the runbook's step 5).
- The fork's `benchmark.py` hardcodes `cpu` → `duckdb-mcp.nrp-nautilus.io`, which is why
  the cirrus CPU deploy added in #291 (explicitly "the CPU baseline … same node, same
  MinIO … for a fair head-to-head") never produced one. Any port under #227 must make
  both endpoints overridable.
