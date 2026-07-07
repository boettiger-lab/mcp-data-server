"""CPU (DuckDB) vs GPU (Polars/cudf-polars) query benchmark over MCP — issue #42.

Runs the same query suite against two mcp-data-server deployments through the
`query` MCP tool and reports median wall-clock per query plus the CPU/GPU
speedup. Because both engines now live in one codebase (QUERY_ENGINE), a fair
comparison just means pointing this at a `duckdb` deployment and a `polars-gpu*`
deployment that share a node and S3 backend — e.g. the cirrus CPU and GPU
deploys (both `mcp` namespace, same in-cluster MinIO).

Usage:
    uv run --with requests benchmarks/cpu-vs-gpu-bench.py
    # or point at specific endpoints / run more repetitions:
    BENCH_CPU_URL=https://duckdb-mcp.carlboettiger.info \\
    BENCH_GPU_URL=https://gpu-mcp.carlboettiger.info \\
    BENCH_REPS=5 uv run --with requests benchmarks/cpu-vs-gpu-bench.py

Endpoints without an ingress (e.g. the GPU ClusterIP service) can be reached by
running this from inside the cluster, or via `kubectl port-forward` to a local
port and setting BENCH_GPU_URL=http://127.0.0.1:PORT.

Queries must stay within the GPU engine's SQL subset: pre-computed h0..h11 index
columns only, no h3_* functions, no spatial/GEOMETRY ops. Prefer DPP-friendly
h0 filters to keep host RAM bounded (the GPU/kvikio readers load whole files
into host memory).
"""
import json
import os
import statistics
import sys
import time
import urllib.request

CPU_URL = os.environ.get("BENCH_CPU_URL", "https://duckdb-mcp.carlboettiger.info").rstrip("/")
GPU_URL = os.environ.get("BENCH_GPU_URL", "https://gpu-mcp.carlboettiger.info").rstrip("/")
REPS = int(os.environ.get("BENCH_REPS", "3"))
TIMEOUT = float(os.environ.get("BENCH_TIMEOUT", "600"))

# Query suite over datasets present on the cirrus MinIO. Within the GPU dialect
# subset (pre-computed h0..h11, no h3_* functions) and DPP-pruned via h0 filters
# so host RAM stays bounded (carbon partitions are ~54 MB each). Scales data size
# (1→4 partitions) and compute (sum → multi-agg → higher-cardinality group) to
# map where GPU wins vs where S3 I/O dominates (issue #42).
CARBON = "s3://public-carbon/irrecoverable-carbon-2024/hex/h0=*/data_0.parquet"
GBIF = "s3://public-gbif/2025-06/hex/h0=*/data_*.parquet"
# RAM-safe h0 partitions (each carbon partition ~54 MB).
H0_1 = "579029211465908223"
H0_4 = "579029211465908223, 580612508209905663, 580260664489017343, 578712552117108735"

QUERIES = [
    ("carbon-sum-1part (~54MB)",
     f"SELECT h5, SUM(carbon) AS total FROM read_parquet('{CARBON}') "
     f"WHERE h0 = {H0_1} GROUP BY h5 ORDER BY total DESC LIMIT 20"),
    ("carbon-sum-4part (~216MB)",
     f"SELECT h5, SUM(carbon) AS total FROM read_parquet('{CARBON}') "
     f"WHERE h0 IN ({H0_4}) GROUP BY h5 ORDER BY total DESC LIMIT 20"),
    ("carbon-multiagg-4part",
     f"SELECT h5, COUNT(*) AS n, SUM(carbon) AS s, AVG(carbon) AS a, "
     f"MIN(carbon) AS lo, MAX(carbon) AS hi FROM read_parquet('{CARBON}') "
     f"WHERE h0 IN ({H0_4}) GROUP BY h5 ORDER BY s DESC LIMIT 20"),
    ("carbon-groupby-h7-4part (high-card)",
     f"SELECT h7, SUM(carbon) AS total FROM read_parquet('{CARBON}') "
     f"WHERE h0 IN ({H0_4}) GROUP BY h7 ORDER BY total DESC LIMIT 20"),
    ("gbif-count-4part",
     f"SELECT h0, COUNT(*) AS n FROM read_parquet('{GBIF}') "
     f"WHERE h0 IN ({H0_4}) GROUP BY h0 ORDER BY n DESC"),
]

_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def _mcp_query(base_url: str, sql: str) -> tuple[float, str]:
    """Run one `query` tool call; return (elapsed_seconds, first_result_line)."""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "query", "arguments": {"sql_query": sql}},
    }).encode()
    req = urllib.request.Request(base_url + "/mcp", data=payload, headers=_HEADERS)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read().decode()
    elapsed = time.time() - t0
    text = ""
    for line in body.splitlines():
        if line.startswith("data:"):
            msg = json.loads(line[5:].strip())
            text = msg.get("result", {}).get("content", [{}])[0].get("text", "")
            break
    return elapsed, text.strip().splitlines()[0] if text else "(no result)"


def _bench(base_url: str, sql: str) -> dict:
    """Warm once, then time REPS runs; return median + a sample result / error."""
    try:
        _, sample = _mcp_query(base_url, sql)  # warmup (JIT, connection, caches)
        if sample.startswith("SQL Error"):
            return {"median": None, "note": sample[:80]}
        times = [_mcp_query(base_url, sql)[0] for _ in range(REPS)]
        return {"median": statistics.median(times), "note": sample[:40]}
    except Exception as e:
        return {"median": None, "note": f"ERR {type(e).__name__}: {str(e)[:60]}"}


def main() -> int:
    print(f"CPU: {CPU_URL}\nGPU: {GPU_URL}\nreps: {REPS}\n", flush=True)
    rows = []
    for label, sql in QUERIES:
        cpu = _bench(CPU_URL, sql)
        gpu = _bench(GPU_URL, sql)
        speedup = ("—" if not (cpu["median"] and gpu["median"])
                   else f"{cpu['median'] / gpu['median']:.2f}×")
        rows.append((label, cpu, gpu, speedup))
        print(f"  {label}: CPU {cpu['median']} GPU {gpu['median']} ({speedup})", flush=True)

    def fmt(r):
        return f"{r['median']:.2f}s" if r["median"] is not None else f"n/a ({r['note']})"

    print("\n## CPU vs GPU (median of", REPS, "runs)\n")
    print("| Query | CPU (DuckDB) | GPU (cudf-polars) | Speedup |")
    print("|---|---|---|---|")
    for label, cpu, gpu, speedup in rows:
        print(f"| {label} | {fmt(cpu)} | {fmt(gpu)} | {speedup} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
