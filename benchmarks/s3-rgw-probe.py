"""Minimal boto3 RGW read-ceiling probe (issue #250).

Reads the full carbon hex via parallel whole-file GETs and reports node + wire
Gb/s. Run as an Indexed Job with parallelism=N across distinct nodes, all
starting together: sum the per-pod Gb/s and compare to a single pod's rate ->
per-client (aggregate ~N×) vs shared RGW ceiling (aggregate flat).
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore import UNSIGNED
from botocore.config import Config

EP = os.environ.get("MRE_ENDPOINT", "rook-ceph-rgw-nautiluss3.rook")
SSL = os.environ.get("MRE_SSL", "false") == "true"
B = os.environ.get("MRE_BUCKET", "public-carbon")
PRE = os.environ.get("MRE_PREFIX", "vulnerable-carbon-2024/hex/")
CONC = [int(x) for x in os.environ.get("MRE_CONC", "64,128").split(",")]
NODE = os.environ.get("NODE_NAME", "?")


def cli():
    return boto3.client(
        "s3", endpoint_url=f"http{'s' if SSL else ''}://{EP}", use_ssl=SSL,
        config=Config(signature_version=UNSIGNED, s3={"addressing_style": "path"},
                      region_name="us-east-1", max_pool_connections=max(CONC) + 8,
                      retries={"max_attempts": 1}, read_timeout=600))


def main():
    c = cli()
    keys, tot = [], 0
    for pg in c.get_paginator("list_objects_v2").paginate(Bucket=B, Prefix=PRE):
        for o in pg.get("Contents", []):
            if o["Key"].endswith(".parquet"):
                keys.append(o["Key"]); tot += o["Size"]

    def gw(client, k):
        body = client.get_object(Bucket=B, Key=k)["Body"]
        n = 0
        for ch in body.iter_chunks(1 << 20):
            n += len(ch)
        return n

    for C in CONC:
        cc = cli()
        t0 = time.perf_counter()
        with ThreadPoolExecutor(C) as ex:
            got = sum(ex.map(lambda k: gw(cc, k), keys))
        dt = time.perf_counter() - t0
        print(f"[RGW] node={NODE} conc={C} {got/1e9/dt:.2f} GB/s = {got*8/1e9/dt:.1f} Gb/s", flush=True)


if __name__ == "__main__":
    main()
