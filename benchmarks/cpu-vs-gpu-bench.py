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

# Query suite. Keep within the GPU dialect subset; use datasets present on the
# target backend. Defaults are small + DPP-pruned so a run is RAM-safe; extend
# with the #42 carbon/IUCN/WDPA queries once those datasets are staged and the
# GPU node is sized for them.
MI = "s3://public-mappinginequality"
QUERIES = [
    ("count-single-file",
     f"SELECT COUNT(*) AS n FROM read_parquet('{MI}/mappinginequality.parquet')"),
    ("groupby-single-file",
     f"SELECT grade, COUNT(*) AS n FROM read_parquet('{MI}/mappinginequality.parquet') "
     "GROUP BY grade ORDER BY n DESC"),
    ("hex-dpp-2-partitions",
     f"SELECT h0, COUNT(*) AS n FROM read_parquet('{MI}/hex/h0=*/data_0.parquet') "
     "WHERE h0 IN (577164439745200127, 577199624117288959) GROUP BY h0 ORDER BY h0"),
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
