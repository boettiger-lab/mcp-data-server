# RAPIDS Bug Reports — GPU vs DuckDB S3 Analytics Gap

Context: benchmarking a GPU-accelerated MCP data server (Polars + cudf_polars GPUEngine) against
DuckDB (CPU) on H3-partitioned parquet datasets served from S3-compatible MinIO.
Hardware: Quadro RTX 8000 (48 GB VRAM), local MinIO on same node (NVMe-backed).
Versions: cudf 25.02, kvikio 25.02.01, polars (latest), python 3.12.

---

## Issue 1: `cudf.to_arrow()` does a full GPU→CPU copy — no zero-copy interchange with Polars

**Component:** cudf / cudf_polars / Arrow C Data Interface

**Impact:** Critical. Makes cudf.read_parquet unusable as a fast path into Polars SQLContext.
Even when parquet data is parsed directly into GPU VRAM with cudf, converting back to a Polars
LazyFrame costs ~7 seconds for 18 GB (885M rows), erasing all parse-time gains.

**Observed behavior:**
```python
import cudf, polars as pl, time

# 885M row carbon DataFrame already in GPU VRAM (18.1 GB)
combined = cudf.concat(cudf_dfs, ignore_index=True)  # 18.1 GB in VRAM

t0 = time.perf_counter()
lf = pl.from_arrow(combined.to_arrow())   # 7.26s — full GPU→CPU copy
print(time.perf_counter() - t0)          # 7.26s for 18 GB @ ~2.5 GB/s (PCIe bottleneck)
```

Compared to the CPU path (no round-trip):
```python
df_cpu = pl.read_parquet(io.BytesIO(raw_bytes))  # 5.74s, stays in CPU RAM
lf = df_cpu.lazy()                                # 0s — zero-copy
```

Full parse-phase comparison on 28 × 115 MB carbon parquet files (885M rows):
| Path | Parse | Concat | GPU→CPU | Total |
|------|-------|--------|---------|-------|
| `pl.read_parquet` (CPU) | 5.74s | — | — | **5.74s** |
| `cudf.read_parquet` (GPU) | 5.25s | 0.19s | 7.26s | **12.70s** |

GPU parse is 9% faster, but Arrow conversion makes end-to-end 2.2× slower.

**Expected behavior:** `pl.from_arrow(cudf_df.to_arrow())` or an equivalent API should use the
Arrow C Data Interface or DLPack for zero-copy GPU↔CPU transfer. At PCIe 4.0 x16 bandwidth
(~32 GB/s theoretical, ~14 GB/s effective), 18 GB should transfer in ~1.3s, not 7.26s.
Alternatively, `cudf_polars` (GPUEngine) should be able to register a cuDF DataFrame with
`pl.SQLContext` directly without any CPU round-trip.

**Workaround:** Use `pl.read_parquet(BytesIO)` (CPU parse) and let GPUEngine handle the transfer
lazily when it materializes the query result. This avoids the eager full-table transfer.

**Reproducer:**
```python
import cudf, polars as pl, io, time, concurrent.futures
import kvikio, kvikio.defaults

kvikio.defaults.set_num_threads(64)
endpoint = "http://<your-s3-endpoint>"
# Download a large parquet file into CPU RAM
f = kvikio.RemoteFile.open_http(f"{endpoint}/bucket/large.parquet")
raw = bytearray(f.nbytes())
f.pread(raw, size=f.nbytes(), file_offset=0).get()

# GPU path
t0 = time.perf_counter()
df_gpu = cudf.read_parquet(io.BytesIO(raw))
parse_time = time.perf_counter() - t0

t0 = time.perf_counter()
lf = pl.from_arrow(df_gpu.to_arrow())   # measure this
conv_time = time.perf_counter() - t0

print(f"GPU parse: {parse_time:.2f}s, Arrow conv: {conv_time:.2f}s, rows={len(df_gpu):,}")
# Expected: conv_time << parse_time
# Observed: conv_time >> parse_time for large DataFrames
```

---

## Issue 2: `GPUEngine` silently falls back to CPU when `hive_partitioning=True` — no warning logged

**Component:** cudf_polars / Polars GPUEngine
**Upstream Polars issue:** pola-rs/polars#20577

**Impact:** Critical. Every benchmark run on hive-partitioned S3 data silently executes on CPU.
We ran weeks of "GPU benchmarks" before discovering via `GPUEngine(raise_on_fail=True)` that
GPU utilization was 0% throughout.

**Observed behavior:**
```python
import polars as pl
from polars import GPUEngine

lf = pl.scan_parquet(
    "s3://bucket/dataset/hex/**",
    hive_partitioning=True,          # <-- this silently forces CPU
    storage_options={"endpoint_url": "..."}
)

# Silent CPU fallback — no warning, no log, GPU at 0%:
df = lf.collect(engine=GPUEngine())

# To detect fallback:
try:
    df = lf.collect(engine=GPUEngine(raise_on_fail=True))
except Exception as e:
    print(e)
# NotImplementedError: scan with hive partitioning
```

GPU utilization during a complete Q3a query (885M row carbon × IUCN join):
```
$ nvidia-smi dmon -s u
# GPU utilization: 0% throughout
# VRAM used: 1 MiB (idle)
```

**Workaround:** Read each hive-partitioned file individually with `pl.read_parquet(file)` (no
`hive_partitioning=True`), inject the partition column (e.g. h0) from the file path manually,
then `pl.concat(dfs).lazy()`. This produces a LazyFrame without hive scan nodes that GPUEngine
can execute. Extra code, but required to actually use the GPU.

```python
import re
_H0_RE = re.compile(r"/h0=(\d+)/")
dfs = []
for path in s3_file_list:
    df = pl.read_parquet(path, storage_options=opts)
    m = _H0_RE.search(path)
    if m and "h0" not in df.columns:
        df = df.with_columns(pl.lit(int(m.group(1))).alias("h0"))
    dfs.append(df)
lf = pl.concat(dfs, how="diagonal_relaxed").lazy()
df = lf.collect(engine=GPUEngine())   # NOW actually uses GPU
```

**Expected behavior:** Either support hive-partitioned scans in GPUEngine, or emit a visible
warning when falling back to CPU. Silent fallback makes GPU performance benchmarking unreliable.

---

## Issue 3: `cudf.read_parquet(storage_options=...)` routes through PyArrow S3, not kvikio

**Component:** cudf / libcudf S3 integration / kvikio

**Impact:** High. RAPIDS documentation implies kvikio is used for S3 reads, but benchmarks show
`cudf.read_parquet(files, storage_options={...})` achieves identical throughput to PyArrow S3
(~1 Gbps), not kvikio's 6+ Gbps.

**Observed behavior:**

Direct S3 throughput comparison on 28 × 115 MB parquet files (3.22 GB), local MinIO, 100G IB:

| Method | Throughput | Notes |
|--------|-----------|-------|
| `kvikio.RemoteFile.pread()` (64 threads, 16 MiB chunks) | **6.25 Gbps** | Correct kvikio path |
| `cudf.read_parquet(files, storage_options=opts)` | ~1 Gbps | Routes through PyArrow S3 |
| `pl.scan_parquet(path, storage_options=opts)` | ~1 Gbps | Polars Rust object_store |

```python
import cudf, time

files = ["s3://bucket/file1.parquet", "s3://bucket/file2.parquet"]
storage_options = {"endpoint_url": "http://...", "anon": True}

t0 = time.perf_counter()
df = cudf.read_parquet(files, storage_options=storage_options)
print(f"{time.perf_counter()-t0:.2f}s")
# Observed: ~26s for 3.22 GB = ~1 Gbps (PyArrow S3, not kvikio)
# Expected: ~4s = ~6 Gbps (kvikio transport)
```

Confirmed via profiling: `cudf.read_parquet(storage_options=...)` calls `pyarrow.fs.S3FileSystem`
internally. The kvikio thread pool (KVIKIO_NTHREADS=64) has no effect on this path.

**Workaround:** Use `kvikio.RemoteFile.open_http(url).pread(buf)` explicitly for each file, then
pass `io.BytesIO(buf)` to `pl.read_parquet()`. Requires manual file enumeration, HTTP URL
construction, and hive partition column injection. ~50 lines of infrastructure code.

**Expected behavior:** `cudf.read_parquet(s3_files, storage_options=opts)` should use kvikio's
S3 transport when kvikio is installed and `KVIKIO_NTHREADS` > 1. Or at minimum, document clearly
that this path uses PyArrow S3, not kvikio, so users know to use `RemoteFile.pread()` directly.

---

## Issue 4: Parquet column projection not available for remote S3 reads via kvikio

**Component:** libcudf parquet reader / kvikio S3 transport

**Impact:** High. DuckDB reads only the column byte-ranges needed by the query (via HTTP range
requests against the parquet file's column chunk offsets). cudf/kvikio always downloads entire
files. For wide tables (e.g. 15-column carbon dataset where queries need 2 columns), DuckDB
downloads ~13% of the bytes vs cudf/kvikio downloading 100%. This is a fundamental ~7× I/O
disadvantage that cannot be closed by faster S3 throughput alone.

**Benchmark evidence:**
- DuckDB Q3a (carbon × IUCN join, 2 columns needed from 15): **14s**
- kvikio full-file pread + Polars GPUEngine: **24s**
- S3 throughput ratio (kvikio vs DuckDB): ~6× faster per byte
- But DuckDB downloads ~7× fewer bytes → still wins end-to-end

**DuckDB's approach:**
1. Fetch parquet footer (small HTTP range request) → get column chunk byte offsets
2. Issue HTTP range requests only for needed column chunks
3. Stream through join without materializing full table (streaming hash join)

**cudf/kvikio approach (current):**
1. `kvikio.RemoteFile.pread(url, buf)` — downloads 100% of each file
2. All columns are present in CPU/GPU RAM regardless of query

**Expected behavior:** `cudf.read_parquet(remote_url, columns=["h8", "h0", "carbon"])` should:
1. Fetch parquet footer to find column chunk byte offsets
2. Issue targeted `kvikio.RemoteFile.pread(offset, size)` calls for only those columns
3. Return only the requested columns

The kvikio `pread(buf, size=N, file_offset=M)` API already supports range requests — the missing
piece is the parquet-aware column selection layer on top.

**Reproducer:**
```python
import cudf, time

# Wide parquet file: 15 columns, query needs 2
url = "s3://public-carbon/irrecoverable-carbon-2024/hex/h0=.../data_0.parquet"
storage_options = {"endpoint_url": "http://..."}

t0 = time.perf_counter()
# Currently downloads ALL columns even though only 2 are needed:
df = cudf.read_parquet(url, columns=["h8", "carbon"], storage_options=storage_options)
elapsed = time.perf_counter() - t0
# Expected: ~13% of file transfer time (column-selective)
# Observed: ~100% of file transfer time (full file downloaded)
```

---

## Issue 5: `kvikio.defaults.set_num_threads()` silently fails in kvikio 25.02

**Component:** kvikio Python bindings

**Impact:** Medium. API appears to work but has no effect; must use env var instead.
Causes silent single-threaded operation if env var is not pre-set, giving misleadingly poor
benchmark results (and initially caused us to conclude kvikio had no S3 advantage).

**Observed behavior:**
```python
import kvikio.defaults

kvikio.defaults.set_num_threads(64)
print(kvikio.defaults.get_num_threads())  # Returns 1, not 64

# Workaround: set env var BEFORE library initialization:
# KVIKIO_NTHREADS=64 python3 script.py
```

Throughput impact on 768 MB file:
| Threads | Throughput |
|---------|-----------|
| 1 (default, set_num_threads broken) | 0.97 Gbps |
| 64 (via KVIKIO_NTHREADS env var) | 6.25 Gbps |

**Expected behavior:** `kvikio.defaults.set_num_threads(N)` should update the thread pool size
at runtime. The env var workaround is not viable for library users who can't control process
environment before import.

**Reproducer:**
```python
import kvikio.defaults
kvikio.defaults.set_num_threads(64)
assert kvikio.defaults.get_num_threads() == 64  # Fails: returns 1
```

---

## Summary table

| # | Issue | Component | Impact | Workaround exists? |
|---|-------|-----------|--------|-------------------|
| 1 | `cudf.to_arrow()` full GPU→CPU copy, no zero-copy Polars interchange | cudf / cudf_polars | Critical | Yes (CPU parse, slower) |
| 2 | GPUEngine silently falls back to CPU on hive-partitioned scans | cudf_polars / Polars | Critical | Yes (manual file-by-file read) |
| 3 | `cudf.read_parquet(storage_options=)` uses PyArrow not kvikio | cudf / libcudf | High | Yes (manual kvikio pread) |
| 4 | No parquet column projection for remote reads | libcudf / kvikio | High | No |
| 5 | `set_num_threads()` silently fails in kvikio 25.02 | kvikio Python | Medium | Yes (KVIKIO_NTHREADS env var) |

Issues 2, 3, and 5 are confirmed fixed in our workarounds (the server achieves actual GPU
execution). Issues 1 and 4 represent fundamental architectural gaps vs DuckDB that require
upstream changes to close.
