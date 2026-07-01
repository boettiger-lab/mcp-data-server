"""Local-filesystem read benchmark (issue #258) — DuckDB reading Parquet from a
mounted volume (LINSTOR/CephFS/local NVMe) instead of s3://, to test bypassing
the NRP RGW read ceiling. Same queries + methodology as s3-compute-bench.py
(valid probes, uncompressed GB/s + Mrow/s, explicit memory_limit).

Baselines to beat (from #250, NRP S3/RGW): mean-carbon/h0 T=48 = 77.8s;
single-pod ~7 Gb/s / 4-pod aggregate ~10 Gb/s; RGW ~120 ms GET latency.
cirrus on-box MinIO (near-zero latency) did mean-carbon/h0 T=48 = 5.1s.

  MRE_GLOB=/data/vulnerable-carbon-2024/hex/** MRE_THREADS=8,16,32,48 \
    uv run --with duckdb python3 local-read-bench.py
"""
import os
import time

import duckdb

GLOB = os.environ.get("MRE_GLOB", "/data/**/*.parquet")
THREADS = [int(x) for x in os.environ.get("MRE_THREADS", "8,16,32,48").split(",")]
MEM = os.environ.get("MRE_MEM", "8GB")


def con(t):
    c = duckdb.connect()
    c.execute("INSTALL h3 FROM community; LOAD h3")
    c.execute(f"SET threads={t}; SET preserve_insertion_order=false; "
              f"SET memory_limit='{MEM}'; SET temp_directory='/tmp'")
    return c


def main():
    print(f"glob={GLOB} mem={MEM} threads={THREADS}", flush=True)
    c = con(8)
    rows = c.execute(f"SELECT sum(num_rows) FROM parquet_file_metadata('{GLOB}')").fetchone()[0]
    unc, comp = c.execute(
        f"SELECT sum(total_uncompressed_size), sum(total_compressed_size) FROM parquet_metadata('{GLOB}')"
    ).fetchone()
    print(f"dataset: {rows/1e9:.2f}B rows | {comp/1e9:.1f} GB comp | {unc/1e9:.1f} GB unc\n", flush=True)

    queries = {
        "mean carbon / h0": f"SELECT h0, avg(carbon) FROM read_parquet('{GLOB}') GROUP BY h0",
        "mean carbon / h1 (h3 rollup)": f"SELECT h3_cell_to_parent(h9,1) h1, avg(carbon) FROM read_parquet('{GLOB}') GROUP BY 1",
        "sum(all 7 cols) [full wire]": f"SELECT sum(carbon),sum(h9),sum(h0),sum(h5),sum(h6),sum(h7),sum(h8) FROM read_parquet('{GLOB}')",
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


if __name__ == "__main__":
    main()
