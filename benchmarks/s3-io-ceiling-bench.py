"""
S3 read I/O-ceiling report (issue #250): how close to network line-rate can we
read NRP Ceph, and what limits DuckDB's httpfs path?

Motivation: the fabric should support ~100 Gb/s, yet DuckDB read_parquet on NRP
Ceph runs ~0.8 Gb/s wire while boto3 does ~16 Gb/s on the same node — a
per-request-LATENCY problem (many small range requests, no connection pooling),
not bandwidth. This quantifies all three layers in **Gb/s of compressed wire
bytes** so they compare directly to the 100 Gb/s NIC.

Sections (all move the same ~12 GB compressed carbon hex, so rates are comparable):
  1. boto3 parallel whole-file GET, concurrency sweep  -> fabric/RGW ceiling
  2. DuckDB core httpfs, sum(all cols), THREADS sweep   -> DuckDB wire rate + scaling
     (sum of every column forces reading all bytes; not a metadata shortcut)
  3. DuckDB cache_httpfs (community), same query        -> does parallel/tunable
                                                          request batching close the gap

Reproduce: k8s/s3-io-ceiling-job.yaml (pin to a 100G DTN). Set MRE_ENDPOINT for
NRP Ceph (rook-ceph-rgw-nautiluss3.rook) vs cirrus MinIO
(minio-svc.minio.svc.cluster.local:9000).
"""
import os, time, resource, socket, glob
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore import UNSIGNED
from botocore.config import Config

try:
    resource.setrlimit(resource.RLIMIT_NOFILE, (1 << 18, 1 << 18))
except Exception as e:  # noqa: BLE001
    print(f"setrlimit warn: {e!r}")

ENDPOINT = os.environ.get("MRE_ENDPOINT", "rook-ceph-rgw-nautiluss3.rook")
USE_SSL = os.environ.get("MRE_SSL", "false") == "true"
BUCKET = os.environ.get("MRE_BUCKET", "public-carbon")
PREFIX = os.environ.get("MRE_PREFIX", "vulnerable-carbon-2024/hex/")
GLOB = f"s3://{BUCKET}/{PREFIX}**"
BOTO_CONC = [int(x) for x in os.environ.get("MRE_BOTO_CONC", "16,64,128,256").split(",")]
DUCK_THREADS = [int(x) for x in os.environ.get("MRE_THREADS", "16,48,96,192").split(",")]
MEM = os.environ.get("MRE_MEM", "10GB")


def rate(nbytes, dt):
    return f"{nbytes/1e9/dt:6.2f} GB/s = {nbytes*8/1e9/dt:6.1f} Gb/s"


def boto():
    return boto3.client("s3", endpoint_url=f"http{'s' if USE_SSL else ''}://{ENDPOINT}",
        use_ssl=USE_SSL, config=Config(signature_version=UNSIGNED, s3={"addressing_style": "path"},
        region_name="us-east-1", max_pool_connections=max(BOTO_CONC)+8, retries={"max_attempts": 1}, read_timeout=600))


def duck(threads, cache=False):
    import duckdb
    c = duckdb.connect()
    if cache:
        c.execute("INSTALL cache_httpfs FROM community; LOAD cache_httpfs")
    else:
        c.execute("INSTALL httpfs; LOAD httpfs")
    c.execute(f"SET threads={threads}; SET preserve_insertion_order=false; SET memory_limit='{MEM}'; SET temp_directory='/tmp'")
    c.execute(f"SET s3_endpoint='{ENDPOINT}'; SET s3_url_style='path'; SET s3_use_ssl={str(USE_SSL).lower()}; SET s3_access_key_id=''; SET s3_secret_access_key=''")
    return c


def main():
    for d in sorted(glob.glob("/sys/class/net/*")):
        n = os.path.basename(d)
        if n == "lo": continue
        try: print(f"nic {n}: mtu={open(d+'/mtu').read().strip()} speed={open(d+'/speed').read().strip()}Mb/s", flush=True)
        except Exception: pass
    try:
        q = open("/sys/fs/cgroup/cpu.max").read().split(); cpu = "unlimited" if q[0]=="max" else f"{int(q[0])/int(q[1]):.0f}"
    except Exception: cpu = "?"
    print(f"node={os.environ.get('NODE_NAME','?')} cpu_quota={cpu} endpoint={ENDPOINT}", flush=True)

    c = boto()
    objs = []
    for pg in c.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PREFIX):
        objs += [(o["Key"], o["Size"]) for o in pg.get("Contents", []) if o["Key"].endswith(".parquet")]
    keys = [k for k, _ in objs]
    comp = sum(s for _, s in objs)
    print(f"dataset: {len(keys)} files, {comp/1e9:.1f} GB compressed (wire bytes)\n", flush=True)

    # 1. boto3 fabric ceiling
    print("[1] boto3 parallel whole-file GET (fabric/RGW ceiling):", flush=True)
    for C in BOTO_CONC:
        cc = boto()
        def gw(k):
            b = cc.get_object(Bucket=BUCKET, Key=k)["Body"]; n = 0
            for ch in b.iter_chunks(1 << 20): n += len(ch)
            return n
        t0 = time.perf_counter()
        with ThreadPoolExecutor(C) as ex:
            got = sum(ex.map(gw, keys))
        print(f"   conc={C:<4} {rate(got, time.perf_counter()-t0)}", flush=True)

    # discover columns for the full-read probe
    cols = [r[0] for r in duck(4).execute(f"DESCRIBE SELECT * FROM read_parquet('s3://{BUCKET}/{keys[0]}')").fetchall()]
    sumall = "SELECT " + ",".join(f"sum({c})" for c in cols) + f" FROM read_parquet('{GLOB}')"

    # 2. DuckDB core httpfs
    print(f"\n[2] DuckDB core httpfs, sum({len(cols)} cols) — full wire read:", flush=True)
    base = None
    for T in DUCK_THREADS:
        try:
            cc = duck(T); t0 = time.perf_counter(); cc.execute(sumall).fetchall(); dt = time.perf_counter()-t0; cc.close()
            sc = f" | {base/dt:.1f}x vs T={DUCK_THREADS[0]}" if base else ""
            print(f"   T={T:<4} {dt:7.1f}s {rate(comp, dt)}{sc}", flush=True)
            if base is None: base = dt
        except Exception as e:  # noqa: BLE001
            print(f"   T={T:<4} FAILED {str(e)[:80]}", flush=True)

    # 3. DuckDB cache_httpfs
    print("\n[3] DuckDB cache_httpfs (community), same query:", flush=True)
    for T in [DUCK_THREADS[len(DUCK_THREADS)//2], max(DUCK_THREADS)]:
        try:
            cc = duck(T, cache=True); t0 = time.perf_counter(); cc.execute(sumall).fetchall(); dt = time.perf_counter()-t0; cc.close()
            print(f"   T={T:<4} {dt:7.1f}s {rate(comp, dt)}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"   T={T:<4} FAILED {str(e)[:100]}", flush=True)


if __name__ == "__main__":
    main()
