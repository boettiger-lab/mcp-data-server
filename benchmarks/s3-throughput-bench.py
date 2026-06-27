"""
S3 read-throughput benchmark: NRP Ceph (internal/external) vs AWS us-west-2.

Why throughput, not per-object latency
--------------------------------------
Our queryable hex datasets are 1 file per h0 (carbon: 122 files @112 MB;
gbif 2026: 122 @3 GB; padus: 21 @356 MB). A pruned query opens ONE footer;
a global scan opens <=122. So per-object open-latency is not the cost — the
cost is streaming the bytes. (The old "~923 files / ~126-per-h0" figure was a
now-fixed over-sharding bug in the GBIF pipeline, not steady state.)

The open question this answers: how does sustained read throughput from the
in-cluster Ceph West pool compare to the same data on AWS us-west-2 (the
source.coop mirror) reached over Internet2/CENIC — and how far does aggregate
throughput scale with concurrent streams (NRP docs claim parallel requests
across storage servers hit high aggregate bandwidth).

Measures, with raw boto3 (no query engine in the path):
  1. LIST latency + key discovery.
  2. Single-stream throughput   - one full object end-to-end -> MB/s.
  3. Aggregate throughput curve - read a distinct block of objects at
                                  concurrency {1,4,8,...}; aggregate MB/s +
                                  per-stream median. THIS IS THE HEADLINE.
  4. Per-object open latency     - N small ranged GETs, warm vs cold connection
     (secondary diagnostic)        (cold-warm gap = TCP/TLS setup cost).

Reported per endpoint; what matters is the SHAPE across endpoints and across
nodes (run the k8s Job to sample multiple us-west nodes for fleet heterogeneity).

Run
---
  uv run --with boto3 python3 s3-throughput-bench.py
  # off-cluster, the rook internal host won't resolve and is skipped.
  MRE_ENDPOINTS=nrp-external,aws-sourcecoop uv run --with boto3 python3 s3-throughput-bench.py

Anonymous (unsigned) reads only; all buckets used here are public.
"""
import os
import socket
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore import UNSIGNED
from botocore.config import Config

# --- knobs (override via env) -----------------------------------------------
CONCURRENCY_LEVELS = [int(x) for x in os.environ.get("MRE_CONCURRENCY", "1,4,8").split(",")]
OBJS_PER_LEVEL = int(os.environ.get("MRE_OBJS_PER_LEVEL", "8"))  # distinct objects read at each level
N_LATENCY = int(os.environ.get("MRE_N_LATENCY", "40"))          # small-GETs for the latency diagnostic
FOOTER_BYTES = int(os.environ.get("MRE_FOOTER_BYTES", str(16 * 1024)))
MAX_POOL = max(CONCURRENCY_LEVELS) + 4

# --- endpoints --------------------------------------------------------------
# Same logical dataset (vulnerable carbon hex, ~112 MB/file) in each location
# so object sizes are comparable. The script discovers actual keys via LIST,
# so prefixes only need to be a parent of the parquet objects.
ALL_ENDPOINTS = {
    "nrp-internal": dict(
        endpoint_url="http://rook-ceph-rgw-nautiluss3.rook",
        bucket="public-carbon", prefix="vulnerable-carbon-2024/hex/",
        region="us-east-1", use_ssl=False,
    ),
    "nrp-external": dict(
        endpoint_url="https://s3-west.nrp-nautilus.io",
        bucket="public-carbon", prefix="vulnerable-carbon-2024/hex/",
        region="us-east-1", use_ssl=True,
    ),
    "aws-sourcecoop": dict(
        endpoint_url=None,  # default AWS endpoint (s3.us-west-2.amazonaws.com)
        # NOTE: must be the hex sub-prefix, not "cboettig/carbon/" — the parent
        # also holds large non-hex carbon products (irrecoverable/manageable/v2)
        # which would make the comparison non-apples-to-apples. This mirror is
        # byte-identical to the NRP path: 122 files, 1/h0, ~112 MB avg.
        bucket="us-west-2.opendata.source.coop",
        prefix="cboettig/carbon/vulnerable-carbon-2024/hex/",
        region="us-west-2", use_ssl=True,
    ),
}

_selected = os.environ.get("MRE_ENDPOINTS")
ENDPOINTS = (
    {k: ALL_ENDPOINTS[k] for k in _selected.split(",") if k in ALL_ENDPOINTS}
    if _selected else ALL_ENDPOINTS
)


def _client(cfg, pool=MAX_POOL):
    config = Config(
        signature_version=UNSIGNED,
        s3={"addressing_style": "path"},  # bucket has dots -> path style for TLS
        region_name=cfg["region"],
        retries={"max_attempts": 1, "mode": "standard"},
        connect_timeout=10,
        read_timeout=300,
        max_pool_connections=pool,
    )
    return boto3.client(
        "s3", endpoint_url=cfg["endpoint_url"], use_ssl=cfg["use_ssl"], config=config
    )


def _reachable(cfg):
    url = cfg["endpoint_url"]
    if url is None:
        return True
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    try:
        socket.getaddrinfo(host, None)
        return True
    except socket.gaierror:
        return False


def _pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def discover_keys(client, bucket, prefix, limit):
    t0 = time.perf_counter()
    keys, sizes = [], {}
    pag = client.get_paginator("list_objects_v2")
    for page in pag.paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            if o["Size"] > 0 and o["Key"].endswith(".parquet"):
                keys.append(o["Key"])
                sizes[o["Key"]] = o["Size"]
                if len(keys) >= limit:
                    return keys, sizes, time.perf_counter() - t0
    return keys, sizes, time.perf_counter() - t0


def timed_get(client, bucket, key, nbytes=None):
    """GET (optionally a leading range), drain body; return (seconds, bytes)."""
    kw = {"Bucket": bucket, "Key": key}
    if nbytes is not None:
        kw["Range"] = f"bytes=0-{nbytes - 1}"
    t0 = time.perf_counter()
    body = client.get_object(**kw)["Body"]
    n = 0
    for chunk in body.iter_chunks(1 << 20):
        n += len(chunk)
    return time.perf_counter() - t0, n


def bench_endpoint(name, cfg):
    print(f"\n{'=' * 72}\n[{name}]  endpoint={cfg['endpoint_url']}  "
          f"bucket={cfg['bucket']}  prefix={cfg['prefix']}", flush=True)
    if not _reachable(cfg):
        print("  SKIP: endpoint host does not resolve from here.", flush=True)
        return

    client = _client(cfg)
    need = max(N_LATENCY, OBJS_PER_LEVEL * len(CONCURRENCY_LEVELS) + 1)
    try:
        keys, sizes, list_dt = discover_keys(client, cfg["bucket"], cfg["prefix"], need)
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR during LIST: {e!r}", flush=True)
        return
    if not keys:
        print(f"  ERROR: no parquet under s3://{cfg['bucket']}/{cfg['prefix']}", flush=True)
        return
    med_mb = statistics.median(sizes.values()) / 1e6
    print(f"  LIST: {list_dt * 1000:.0f}ms, {len(keys)} objects, median size {med_mb:.0f} MB", flush=True)

    # 2. single-stream throughput
    dt, nb = timed_get(client, cfg["bucket"], keys[0])
    print(f"  single-stream throughput: {nb / 1e6 / dt:7.0f} MB/s "
          f"({nb / 1e6:.0f} MB in {dt:.1f}s)", flush=True)

    # 3. aggregate throughput vs concurrency (HEADLINE) — distinct objects per
    #    level so we measure cold reads, not warmed Ceph/OS cache.
    print("  aggregate throughput vs concurrency (distinct objects/level):", flush=True)
    cursor = 0
    for c in CONCURRENCY_LEVELS:
        block = keys[cursor:cursor + OBJS_PER_LEVEL]
        cursor += OBJS_PER_LEVEL
        if len(block) < c:  # not enough distinct objects; reuse from start
            block = (keys * ((c // len(keys)) + 1))[:max(c, OBJS_PER_LEVEL)]
        cc = _client(cfg, pool=c + 4)
        per_stream = []
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=c) as ex:
            futs = [ex.submit(timed_get, cc, cfg["bucket"], k) for k in block]
            tot = 0
            for f in futs:
                s, nbytes = f.result()
                per_stream.append(nbytes / 1e6 / s)
                tot += nbytes
        wall = time.perf_counter() - t0
        cc.close()
        print(f"    conc={c:>3}: aggregate {tot / 1e6 / wall:7.0f} MB/s  "
              f"per-stream median {statistics.median(per_stream):5.0f} MB/s  "
              f"({tot / 1e6:.0f} MB, {len(block)} objs, wall {wall:.1f}s)", flush=True)

    # 4. per-object open latency (secondary diagnostic)
    small = keys[:N_LATENCY]
    warm = [timed_get(client, cfg["bucket"], k, FOOTER_BYTES)[0] for k in small]
    cold = []
    for k in small:
        c2 = _client(cfg, pool=1)
        cold.append(timed_get(c2, cfg["bucket"], k, FOOTER_BYTES)[0])
        c2.close()
    print(f"  open latency ({FOOTER_BYTES // 1024} KiB GET): "
          f"warm p50={_pct(warm, 50) * 1000:.0f}ms p95={_pct(warm, 95) * 1000:.0f}ms | "
          f"cold p50={_pct(cold, 50) * 1000:.0f}ms p95={_pct(cold, 95) * 1000:.0f}ms | "
          f"setup~{(_pct(cold, 50) - _pct(warm, 50)) * 1000:+.0f}ms", flush=True)


def main():
    print(f"boto3 {boto3.__version__}", flush=True)
    print(f"node={os.environ.get('NODE_NAME', '?')}  region={os.environ.get('NODE_REGION', '?')}  "
          f"pod={os.environ.get('POD_NAME', '?')}", flush=True)
    print(f"knobs: CONCURRENCY={CONCURRENCY_LEVELS} OBJS_PER_LEVEL={OBJS_PER_LEVEL} "
          f"N_LATENCY={N_LATENCY}", flush=True)
    for name, cfg in ENDPOINTS.items():
        try:
            bench_endpoint(name, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"  [{name}] FAILED: {e!r}", flush=True)


if __name__ == "__main__":
    main()
