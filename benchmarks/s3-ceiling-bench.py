"""
S3 read-CEILING benchmark: isolate client (boto3/Python) vs RGW vs engine limits.

Context
-------
First-pass throughput bench (s3-throughput-bench.py) on a 2-CPU pod showed
single-stream ~120 MB/s and aggregate plateauing ~0.5-0.9 GB/s. The GPU team
independently measured (mcp-gpu-data-server#5): kvikio 64-thread 16-MiB-chunk
pread = 6.25 Gbps (~786 MB/s) on 3.2 GiB carbon, while Polars Rust object_store
(no GIL) got only 0.97 Gbps single-path — i.e. a non-Python client hit the SAME
~120 MB/s single-stream ceiling. That points at a per-connection RGW limit, not
boto3.

This bench separates the candidates, on a FAT-CPU pod so the client is never the
bottleneck:
  1. single-stream            - one whole file, one connection (per-connection ceiling).
  2. single-file chunked       - ONE file, M parallel 16-MiB range GETs (kvikio-style).
                                 Does a single file beat the per-connection ceiling?
  3. multi-file aggregate      - C distinct whole files, C workers (raw boto3 ceiling).
  4. DuckDB httpfs scan        - count(COLUMNS(*)) over all files at THREADS=N — our
                                 ACTUAL engine's wire throughput (C++, no GIL, the
                                 number that matters for tile builds / queries).

Comparing (1) vs (2): per-connection vs per-file limit.
Comparing (3) boto3 vs (4) DuckDB at matched parallelism: is the Python client the cap?
Comparing fat-pod (3) vs the earlier 2-CPU run: was client CPU the cap?

Run (in-cluster; internal Ceph endpoint only — the production path):
  uv run --with boto3 --with duckdb python3 s3-ceiling-bench.py
"""
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore import UNSIGNED
from botocore.config import Config

ENDPOINT = os.environ.get("MRE_ENDPOINT", "http://rook-ceph-rgw-nautiluss3.rook")
BUCKET = os.environ.get("MRE_BUCKET", "public-carbon")
PREFIX = os.environ.get("MRE_PREFIX", "vulnerable-carbon-2024/hex/")
GLOB = f"s3://{BUCKET}/{PREFIX}**"
CHUNK = int(os.environ.get("MRE_CHUNK", str(16 * 1024 * 1024)))  # 16 MiB, kvikio-style
CHUNK_WORKERS = [int(x) for x in os.environ.get("MRE_CHUNK_WORKERS", "8,16,32,64").split(",")]
AGG_CONC = [int(x) for x in os.environ.get("MRE_AGG_CONC", "1,8,16,32,64").split(",")]
DUCK_THREADS = [int(x) for x in os.environ.get("MRE_DUCK_THREADS", "16,48").split(",")]
USE_SSL = ENDPOINT.startswith("https")


def client(pool):
    cfg = Config(signature_version=UNSIGNED, s3={"addressing_style": "path"},
                 region_name="us-east-1", retries={"max_attempts": 1},
                 connect_timeout=10, read_timeout=300, max_pool_connections=pool)
    return boto3.client("s3", endpoint_url=ENDPOINT, use_ssl=USE_SSL, config=cfg)


def drain(body):
    n = 0
    for ch in body.iter_chunks(1 << 20):
        n += len(ch)
    return n


def get_whole(c, key):
    t0 = time.perf_counter()
    n = drain(c.get_object(Bucket=BUCKET, Key=key)["Body"])
    return time.perf_counter() - t0, n


def get_range(c, key, start, length):
    body = c.get_object(Bucket=BUCKET, Key=key, Range=f"bytes={start}-{start + length - 1}")["Body"]
    return drain(body)


def main():
    print(f"node={os.environ.get('NODE_NAME','?')} pod={os.environ.get('POD_NAME','?')} "
          f"cpus={os.cpu_count()} endpoint={ENDPOINT}", flush=True)
    import boto3 as _b
    print(f"boto3 {_b.__version__}", flush=True)

    c = client(max(AGG_CONC + CHUNK_WORKERS) + 4)
    pag = c.get_paginator("list_objects_v2")
    objs = []
    for pg in pag.paginate(Bucket=BUCKET, Prefix=PREFIX):
        for o in pg.get("Contents", []):
            if o["Key"].endswith(".parquet") and o["Size"] > 0:
                objs.append((o["Key"], o["Size"]))
    keys = [k for k, _ in objs]
    total_bytes = sum(s for _, s in objs)
    print(f"discovered {len(keys)} files, {total_bytes/1e9:.1f} GB, "
          f"median {statistics.median([s for _,s in objs])/1e6:.0f} MB\n", flush=True)

    # 1. single-stream (per-connection ceiling)
    dt, n = get_whole(c, keys[0])
    print(f"[1] single-stream whole-file : {n/1e6/dt:6.0f} MB/s  ({n/1e6:.0f} MB / {dt:.2f}s)", flush=True)

    # 2. single-file chunked (kvikio-style: M parallel 16-MiB ranges on ONE file)
    print(f"[2] single-file chunked ({CHUNK//1024//1024} MiB ranges, one file each):", flush=True)
    for i, M in enumerate(CHUNK_WORKERS):
        key, size = objs[i % len(objs)]
        ranges = [(off, min(CHUNK, size - off)) for off in range(0, size, CHUNK)]
        cc = client(M + 4)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=M) as ex:
            got = sum(ex.map(lambda r: get_range(cc, key, r[0], r[1]), ranges))
        dt = time.perf_counter() - t0
        cc.close()
        print(f"      workers={M:>3}: {got/1e6/dt:6.0f} MB/s  ({got/1e6:.0f} MB, {len(ranges)} chunks, {dt:.2f}s)", flush=True)

    # 3. multi-file aggregate (raw boto3 ceiling), distinct files per level
    print("[3] multi-file aggregate (distinct whole files, C workers):", flush=True)
    cursor = 0
    for C in AGG_CONC:
        block = keys[cursor:cursor + max(C, 8)]
        cursor += len(block)
        if len(block) < C:
            block = keys[:max(C, 8)]
        cc = client(C + 4)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=C) as ex:
            got = sum(r[1] for r in ex.map(lambda k: get_whole(cc, k), block))
        dt = time.perf_counter() - t0
        cc.close()
        print(f"      conc={C:>3}: {got/1e6/dt:6.0f} MB/s  ({got/1e6:.0f} MB, {len(block)} files, {dt:.2f}s)", flush=True)

    # 4. DuckDB httpfs scan — our actual engine. count(COLUMNS(*)) forces reading
    #    every column (= all bytes) so MB/s on file size ≈ wire throughput.
    try:
        import duckdb
        print(f"[4] DuckDB httpfs scan (count(COLUMNS(*)) over all {len(keys)} files):", flush=True)
        for T in DUCK_THREADS:
            con = duckdb.connect(":memory:")
            con.sql("INSTALL httpfs; LOAD httpfs")
            con.sql(f"SET THREADS={T}; SET preserve_insertion_order=false; SET enable_object_cache=false")
            con.sql("SET s3_allow_recursive_globbing=false")
            con.sql(f"CREATE OR REPLACE SECRET s3 (TYPE S3, ENDPOINT '{ENDPOINT.split('://')[-1]}', "
                    f"URL_STYLE 'path', USE_SSL '{str(USE_SSL).lower()}', KEY_ID '', SECRET '')")
            t0 = time.perf_counter()
            con.sql(f"SELECT count(COLUMNS(*)) FROM read_parquet('{GLOB}')").fetchall()
            dt = time.perf_counter() - t0
            con.close()
            print(f"      THREADS={T:>3}: {total_bytes/1e6/dt:6.0f} MB/s  ({total_bytes/1e9:.1f} GB / {dt:.2f}s)", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[4] DuckDB scan FAILED: {e!r}", flush=True)


if __name__ == "__main__":
    main()
