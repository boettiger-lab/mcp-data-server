"""
DuckDB S3-read diagnosis: is the ~180 MB/s ceiling I/O (httpfs) or compute?

Decisive test = LOCAL vs S3, same query, same files:
  - download K files to /tmp, run count(COLUMNS(*)) on the LOCAL copies   -> DuckDB
    compute + local-disk ceiling
  - run the identical query against the same files on S3                  -> DuckDB
    httpfs ceiling
  local >> S3  => httpfs I/O path is the bottleneck (bandwidth is available; DuckDB
                  isn't using it) — i.e. I/O-bound inside httpfs, not compute-bound.
  local ~= S3  => DuckDB compute (the count) is the limit, not I/O.

Plus:
  - THREADS sweep 16..128 on S3 (does it scale past our production 48?)
  - cache_httpfs community extension vs core httpfs (parallel large-request reads)
  - boto3 multi-file aggregate baseline (raw network ceiling on this node)
  - pod network info (MTU; NIC speed best-effort — node NIC needs hostNetwork,
    which NRP blocks)

count(COLUMNS(*)) reads every column = all file bytes, so MB/s on summed file
size ≈ wire throughput. Memory-safe at 16 GiB (validated).
"""
import os, glob, statistics, subprocess, time
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore import UNSIGNED
from botocore.config import Config

ENDPOINT = "http://rook-ceph-rgw-nautiluss3.rook"
BUCKET, PREFIX = "public-carbon", "vulnerable-carbon-2024/hex/"
GLOB = f"s3://{BUCKET}/{PREFIX}**"
LOCAL_DIR = "/tmp/localparq"
K_LOCAL = int(os.environ.get("MRE_K_LOCAL", "16"))
THREAD_SWEEP = [int(x) for x in os.environ.get("MRE_THREADS", "16,32,48,64,96,128").split(",")]


def s3():
    return boto3.client("s3", endpoint_url=ENDPOINT, use_ssl=False,
        config=Config(signature_version=UNSIGNED, s3={"addressing_style": "path"},
                      region_name="us-east-1", max_pool_connections=128,
                      retries={"max_attempts": 1}, read_timeout=300))


def netinfo():
    print("== network (pod view; node NIC needs hostNetwork, blocked) ==", flush=True)
    for d in sorted(glob.glob("/sys/class/net/*")):
        n = os.path.basename(d)
        if n == "lo":
            continue
        def rd(f):
            try:
                return open(os.path.join(d, f)).read().strip()
            except Exception:
                return "?"
        print(f"   {n}: mtu={rd('mtu')} speed={rd('speed')}Mb/s state={rd('operstate')}", flush=True)


def duck(con_sql, query, total_bytes, label):
    import duckdb
    con = duckdb.connect(":memory:")
    for s in con_sql:
        con.sql(s)
    t0 = time.perf_counter()
    con.sql(query).fetchall()
    dt = time.perf_counter() - t0
    con.close()
    print(f"   {label}: {total_bytes/1e6/dt:6.0f} MB/s  ({total_bytes/1e9:.1f} GB / {dt:.1f}s)", flush=True)
    return dt


def main():
    print(f"node={os.environ.get('NODE_NAME','?')} cpus={os.cpu_count()}", flush=True)
    import duckdb; print(f"duckdb {duckdb.__version__}", flush=True)
    netinfo()

    c = s3()
    objs = []
    pag = c.get_paginator("list_objects_v2")
    for pg in pag.paginate(Bucket=BUCKET, Prefix=PREFIX):
        for o in pg.get("Contents", []):
            if o["Key"].endswith(".parquet") and o["Size"] > 0:
                objs.append((o["Key"], o["Size"]))
    total = sum(s for _, s in objs)
    print(f"\n{len(objs)} files, {total/1e9:.1f} GB total", flush=True)

    # discover a numeric column for sum() probes (not strictly needed; we use count)
    base = [f"INSTALL httpfs; LOAD httpfs",
            "SET preserve_insertion_order=false", "SET enable_object_cache=false",
            "SET s3_allow_recursive_globbing=false",
            f"CREATE OR REPLACE SECRET s3 (TYPE S3, ENDPOINT '{ENDPOINT.split('://')[-1]}', "
            f"URL_STYLE 'path', USE_SSL 'false', KEY_ID '', SECRET '')"]

    # ---- I/O vs COMPUTE: download K files, compare LOCAL vs S3 (same files) ----
    os.makedirs(LOCAL_DIR, exist_ok=True)
    subset = objs[:K_LOCAL]
    sub_bytes = sum(s for _, s in subset)
    print(f"\n[A] downloading {K_LOCAL} files ({sub_bytes/1e9:.1f} GB) to {LOCAL_DIR} ...", flush=True)
    t0 = time.perf_counter()
    def dl(key):
        dst = os.path.join(LOCAL_DIR, key.replace("/", "_"))
        c.download_file(BUCKET, key, dst)
        return os.path.getsize(dst)
    with ThreadPoolExecutor(32) as ex:
        got = sum(ex.map(dl, [k for k, _ in subset]))
    print(f"    downloaded {got/1e9:.1f} GB in {time.perf_counter()-t0:.1f}s "
          f"({got/1e6/(time.perf_counter()-t0):.0f} MB/s via boto3)", flush=True)

    local_glob = f"{LOCAL_DIR}/*.parquet"
    s3_list = "[" + ",".join("'s3://%s/%s'" % (BUCKET, k) for k, _ in subset) + "]"
    print(f"[A] LOCAL vs S3, count(COLUMNS(*)) on the SAME {K_LOCAL} files, THREADS=48:", flush=True)
    duck(base + ["SET THREADS=48"], f"SELECT count(COLUMNS(*)) FROM read_parquet('{local_glob}')", sub_bytes, "LOCAL disk  ")
    duck(base + ["SET THREADS=48"], f"SELECT count(COLUMNS(*)) FROM read_parquet({s3_list})", sub_bytes, "S3 httpfs   ")

    # ---- THREADS sweep on S3 (all files) ----
    print(f"\n[B] DuckDB S3 count(COLUMNS(*)) over all {len(objs)} files, THREADS sweep:", flush=True)
    for T in THREAD_SWEEP:
        duck(base + [f"SET THREADS={T}"], f"SELECT count(COLUMNS(*)) FROM read_parquet('{GLOB}')", total, f"THREADS={T:<3}")

    # ---- cache_httpfs community extension (parallel large-request reads) ----
    print("\n[C] cache_httpfs community extension vs core httpfs (THREADS=48):", flush=True)
    try:
        import duckdb
        con = duckdb.connect(":memory:")
        con.sql("INSTALL cache_httpfs FROM community; LOAD cache_httpfs")
        con.sql("SET preserve_insertion_order=false; SET enable_object_cache=false; SET s3_allow_recursive_globbing=false; SET THREADS=48")
        con.sql(f"CREATE OR REPLACE SECRET s3 (TYPE S3, ENDPOINT '{ENDPOINT.split('://')[-1]}', URL_STYLE 'path', USE_SSL 'false', KEY_ID '', SECRET '')")
        t0 = time.perf_counter()
        con.sql(f"SELECT count(COLUMNS(*)) FROM read_parquet('{GLOB}')").fetchall()
        dt = time.perf_counter() - t0
        con.close()
        print(f"   cache_httpfs: {total/1e6/dt:6.0f} MB/s  ({total/1e9:.1f} GB / {dt:.1f}s)", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"   cache_httpfs FAILED: {e!r}", flush=True)

    # ---- boto3 raw network baseline on this node ----
    print("\n[D] boto3 multi-file aggregate (raw network ceiling, this node):", flush=True)
    for C in [16, 64]:
        keys = [k for k, _ in objs][:max(C, 16)]
        cc = s3()
        def gw(k):
            b = cc.get_object(Bucket=BUCKET, Key=k)["Body"]; n = 0
            for ch in b.iter_chunks(1 << 20): n += len(ch)
            return n
        t0 = time.perf_counter()
        with ThreadPoolExecutor(C) as ex:
            tot = sum(ex.map(gw, keys))
        dt = time.perf_counter() - t0
        print(f"   conc={C:<3}: {tot/1e6/dt:6.0f} MB/s", flush=True)


if __name__ == "__main__":
    main()
