"""
Compute-scaling benchmark for the MCP DuckDB+httpfs read path (issue #250).

Measures representative MCP aggregate queries across a THREADS sweep, with the
CORRECT methodology established in #250:
  - real data-reading queries (avg/sum over columns) — NOT count(*)/count(COLUMNS(*)),
    which are parquet-metadata shortcuts that read ~no data;
  - reports uncompressed GB/s + Mrow/s (compressed-byte MB/s understates ~5x);
  - sets memory_limit explicitly (DuckDB sizes from host RAM -> cgroup OOM otherwise).

Rate-limiting read: if wall time scales ~linearly with THREADS, the query is
compute/decode-bound; if it flattens early, it's I/O/transport-bound.

NOTE on cores: a container's cgroup cpu.max caps real CPU regardless of
os.cpu_count()/sched_getaffinity (which report the node). Check the quota:
    cat /sys/fs/cgroup/cpu.max     # "1200000 100000" => 12 cores
Sweeping THREADS past the quota only buys CFS throttling. To sweep higher,
raise the pod's CPU limit (and restart) or run on a larger allocation.

Endpoint defaults to the cirrus internal MinIO (fixed loopback; the external
minio.carlboettiger.info hairpins out the 1 Gb/s NIC). Override via env.

Run:
  uv run --with duckdb python3 benchmarks/s3-compute-bench.py
  MRE_THREADS=1,4,8,16,32,48 MRE_ENDPOINT=minio-svc.minio.svc.cluster.local:9000 \
    uv run --with duckdb python3 benchmarks/s3-compute-bench.py
"""
import os, time, duckdb

ENDPOINT = os.environ.get("MRE_ENDPOINT", "minio-svc.minio.svc.cluster.local:9000")
USE_SSL = os.environ.get("MRE_SSL", "false")
GLOB = os.environ.get("MRE_GLOB", "s3://public-carbon/vulnerable-carbon-2024/hex/**")
THREADS = [int(x) for x in os.environ.get("MRE_THREADS", "1,4,8,16").split(",")]
MEM = os.environ.get("MRE_MEM", "8GB")


def con(t):
    c = duckdb.connect()
    c.execute("INSTALL httpfs; LOAD httpfs; INSTALL h3 FROM community; LOAD h3")
    c.execute(f"SET threads={t}; SET preserve_insertion_order=false; "
              f"SET memory_limit='{MEM}'; SET temp_directory='/tmp'")
    c.execute(f"SET s3_endpoint='{ENDPOINT}'; SET s3_url_style='path'; "
              f"SET s3_use_ssl={USE_SSL}; SET s3_access_key_id=''; SET s3_secret_access_key=''")
    return c


def main():
    # cgroup CPU quota (the real ceiling)
    try:
        q = open("/sys/fs/cgroup/cpu.max").read().split()
        quota = "unlimited" if q[0] == "max" else f"{int(q[0])/int(q[1]):.1f} cores"
    except Exception:
        quota = "?"
    print(f"endpoint={ENDPOINT} mem={MEM} cgroup-cpu-quota={quota} threads_sweep={THREADS}", flush=True)

    c = con(8)
    rows = c.execute(f"SELECT sum(num_rows) FROM parquet_file_metadata('{GLOB}')").fetchone()[0]
    unc, comp = c.execute(
        f"SELECT sum(total_uncompressed_size), sum(total_compressed_size) FROM parquet_metadata('{GLOB}')"
    ).fetchone()
    print(f"dataset: {rows/1e9:.2f}B rows | {comp/1e9:.1f} GB compressed | "
          f"{unc/1e9:.1f} GB uncompressed ({unc/comp:.1f}x)\n", flush=True)

    queries = {
        "mean carbon / h0 (122 grp, partition col)": f"SELECT h0, avg(carbon) FROM read_parquet('{GLOB}') GROUP BY h0",
        "mean carbon / h1 (h3_cell_to_parent(h9,1))": f"SELECT h3_cell_to_parent(h9,1) h1, avg(carbon) FROM read_parquet('{GLOB}') GROUP BY 1",
        "sum(carbon) [1 col, light]": f"SELECT sum(carbon) FROM read_parquet('{GLOB}')",
        "sum(all 7 cols) [heavy decode]": f"SELECT sum(carbon),sum(h9),sum(h0),sum(h5),sum(h6),sum(h7),sum(h8) FROM read_parquet('{GLOB}')",
    }
    for name, q in queries.items():
        print(f"## {name}", flush=True)
        base = None
        for t in THREADS:
            cc = con(t)
            t0 = time.perf_counter()
            cc.execute(q).fetchall()
            dt = time.perf_counter() - t0
            cc.close()
            sc = f" | {base/dt:.1f}x vs T={THREADS[0]}" if base else ""
            print(f"   T={t:<3} {dt:7.1f}s | {rows/1e6/dt:6.0f} Mrow/s | "
                  f"{unc/1e9/dt:5.2f} GB/s unc | {comp/1e9/dt:5.2f} GB/s comp{sc}", flush=True)
            if base is None:
                base = dt
        print(flush=True)


if __name__ == "__main__":
    main()
