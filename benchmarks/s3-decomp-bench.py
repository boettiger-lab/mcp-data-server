"""
Decompose DuckDB S3 read throughput: transport vs range-requests vs decode.

Probes, all on the SAME N files at matched threads (apples-to-apples):

  boto3 whole-file GET      client=boto3, whole-file GETs, ~zero compute   -- raw network
  DuckDB read_blob          client=httpfs, whole-file GETs, ~zero compute  -- httpfs transport
  DuckDB read_parquet       client=httpfs, RANGE GETs + parquet decode + count
  LOCAL read_blob/parquet   no network                                     -- decode/compute floor

Reads:
  boto3 vs read_blob      -> is DuckDB's httpfs TRANSPORT slower than boto3 for identical work?
  read_blob vs read_parquet -> cost of parquet range-request pattern + decode (the "amplification")
  s3 read_parquet vs local read_parquet -> network's share of the parquet path

read_blob / octet_length does near-zero compute, so its MB/s ~= pure transfer.
N-file subset keeps read_blob memory bounded (it materializes a BLOB per file).
"""
import os, glob, resource, time
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore import UNSIGNED
from botocore.config import Config

# raise the open-file limit (cache/boto3 fan-out hit Errno 24 last run)
try:
    resource.setrlimit(resource.RLIMIT_NOFILE, (1 << 16, 1 << 16))
except Exception as e:  # noqa: BLE001
    print(f"setrlimit warn: {e!r}")

ENDPOINT = "http://rook-ceph-rgw-nautiluss3.rook"
HOST = ENDPOINT.split("://")[-1]
BUCKET, PREFIX = "public-carbon", "vulnerable-carbon-2024/hex/"
N = int(os.environ.get("MRE_N", "32"))
THREADS = [int(x) for x in os.environ.get("MRE_THREADS", "32,64,96").split(",")]
LOCAL_DIR = "/tmp/lp"


def s3():
    return boto3.client("s3", endpoint_url=ENDPOINT, use_ssl=False,
        config=Config(signature_version=UNSIGNED, s3={"addressing_style": "path"},
                      region_name="us-east-1", max_pool_connections=160,
                      retries={"max_attempts": 1}, read_timeout=300))


def duckcon(threads):
    import duckdb
    con = duckdb.connect(":memory:")
    con.sql("INSTALL httpfs; LOAD httpfs")
    con.sql(f"SET THREADS={threads}; SET preserve_insertion_order=false; SET enable_object_cache=false")
    con.sql(f"CREATE OR REPLACE SECRET s3 (TYPE S3, ENDPOINT '{HOST}', URL_STYLE 'path', USE_SSL 'false', KEY_ID '', SECRET '')")
    return con


def run(con, q, nbytes, label):
    t0 = time.perf_counter()
    con.sql(q).fetchall()
    dt = time.perf_counter() - t0
    print(f"   {label:34s} {nbytes/1e6/dt:6.0f} MB/s  ({nbytes/1e9:.1f} GB / {dt:.1f}s)", flush=True)


def main():
    print(f"node={os.environ.get('NODE_NAME','?')} cpus={os.cpu_count()} "
          f"nofile={resource.getrlimit(resource.RLIMIT_NOFILE)}", flush=True)
    import duckdb; print(f"duckdb {duckdb.__version__}", flush=True)
    for d in sorted(glob.glob("/sys/class/net/*")):
        n = os.path.basename(d)
        if n == "lo": continue
        try:
            print(f"   nic {n}: mtu={open(d+'/mtu').read().strip()} speed={open(d+'/speed').read().strip()}Mb/s", flush=True)
        except Exception: pass

    c = s3()
    objs = []
    for pg in c.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PREFIX):
        for o in pg.get("Contents", []):
            if o["Key"].endswith(".parquet") and o["Size"] > 0:
                objs.append((o["Key"], o["Size"]))
    objs = objs[:N]
    keys = [k for k, _ in objs]
    nbytes = sum(s for _, s in objs)
    s3_list = "[" + ",".join("'s3://%s/%s'" % (BUCKET, k) for k in keys) + "]"
    print(f"\nsubset: {N} files, {nbytes/1e9:.1f} GB\n", flush=True)

    # 1. boto3 whole-file (raw network transport) at matched concurrency
    print("[1] boto3 whole-file GET (raw network transport):", flush=True)
    for T in THREADS:
        cc = s3()
        def gw(k):
            b = cc.get_object(Bucket=BUCKET, Key=k)["Body"]; n = 0
            for ch in b.iter_chunks(1 << 20): n += len(ch)
            return n
        t0 = time.perf_counter()
        with ThreadPoolExecutor(T) as ex:
            tot = sum(ex.map(gw, keys))
        print(f"   conc={T:<3}                        {tot/1e6/(time.perf_counter()-t0):6.0f} MB/s", flush=True)

    # 2. DuckDB read_blob (httpfs whole-file transport, ~zero compute)
    print("[2] DuckDB read_blob (httpfs transport, whole-file):", flush=True)
    for T in THREADS:
        con = duckcon(T)
        run(con, f"SELECT sum(octet_length(content)) FROM read_blob({s3_list})", nbytes, f"THREADS={T}")
        con.close()

    # 3. DuckDB read_parquet (httpfs range GETs + decode + count)
    print("[3] DuckDB read_parquet count(COLUMNS(*)) (range GETs + decode):", flush=True)
    for T in THREADS:
        con = duckcon(T)
        run(con, f"SELECT count(COLUMNS(*)) FROM read_parquet({s3_list})", nbytes, f"THREADS={T}")
        con.close()

    # 4. local floor (download once, read from disk)
    os.makedirs(LOCAL_DIR, exist_ok=True)
    print(f"\n[4] download {N} files, then LOCAL read (decode/compute floor):", flush=True)
    def dl(k):
        dst = os.path.join(LOCAL_DIR, k.replace("/", "_")); c.download_file(BUCKET, k, dst); return dst
    with ThreadPoolExecutor(32) as ex:
        list(ex.map(dl, keys))
    lg = f"{LOCAL_DIR}/*.parquet"
    con = duckcon(max(THREADS))
    run(con, f"SELECT sum(octet_length(content)) FROM read_blob('{lg}')", nbytes, f"local read_blob T={max(THREADS)}")
    run(con, f"SELECT count(COLUMNS(*)) FROM read_parquet('{lg}')", nbytes, f"local read_parquet T={max(THREADS)}")
    con.close()


if __name__ == "__main__":
    main()
