"""
source.coop endpoint comparison: direct AWS S3 bucket vs the data.source.coop proxy.

The mirror fallback (#260, #261) rewrites STAC hrefs to the S3 bucket form
`s3://us-west-2.opendata.source.coop/...`, which is a CNAME straight to AWS S3
(s3.us-west-2.amazonaws.com) — i.e. DIRECT AWS. The STAC assets themselves are
published under the `data.source.coop` HTTPS gateway, which is Cloudflare-fronted
(a CDN PROXY in front of the same S3 origin). Same bytes, two front doors.

This measures whether it's worth going direct to AWS vs through the CDN proxy:

  RAW HTTP (isolates transport; both are plain HTTPS GETs of the same object):
    1. footer latency  - many small ranged GETs (last 16 KiB) -> median ms.
    2. single-stream   - one full object end-to-end -> MB/s.
    3. aggregate       - K distinct objects at concurrency C -> aggregate MB/s.

  DuckDB (how the server actually reads — s3:// vs https:// read_parquet):
    4. count(*)        - footer-only across the whole dataset (latency-bound).
    5. sum(1 column)   - column-pruned scan across the whole dataset (throughput).

Cold vs warm matters here: Cloudflare can edge-cache, so the proxy may *win*
on warm reads and lose on cold. Both passes are reported.

CAVEAT: numbers are relative to WHERE THIS RUNS. From NRP the path to AWS is
Internet2/CENIC; from a laptop/campus it is commodity egress. Run the k8s Job
form on-cluster for production-representative numbers.

Run
---
  uv run --with 'duckdb>=1.1' --with requests python3 source-coop-endpoint-bench.py
  MRE_DATASET=cboettig/gbif/... MRE_THREADS=16 uv run ... python3 source-coop-endpoint-bench.py

Anonymous (unsigned) reads only; the source.coop mirror is public.
"""
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import duckdb
import requests

# --- knobs (override via env) -----------------------------------------------
# Prefix under the mirror bucket whose h0=*/data_0.parquet files are compared.
DATASET = os.environ.get("MRE_DATASET", "cboettig/carbon/vulnerable-carbon-2024/hex")
COLUMN = os.environ.get("MRE_COLUMN", "carbon")   # column for the pruned-scan test
THREADS = int(os.environ.get("MRE_THREADS", "16"))
N_LATENCY = int(os.environ.get("MRE_N_LATENCY", "20"))   # small ranged GETs
N_STREAM = int(os.environ.get("MRE_N_STREAM", "5"))      # distinct full-object GETs
AGG_OBJS = int(os.environ.get("MRE_AGG_OBJS", "16"))     # objects in the aggregate test
AGG_CONC = int(os.environ.get("MRE_AGG_CONC", "8"))      # concurrency for aggregate test
FOOTER_BYTES = int(os.environ.get("MRE_FOOTER_BYTES", str(16 * 1024)))

BUCKET = "us-west-2.opendata.source.coop"
AWS_HOST = "https://s3.us-west-2.amazonaws.com"          # direct AWS, path-style
PROXY_HOST = "https://data.source.coop"                  # Cloudflare CDN proxy

# key -> URL for each front door (same underlying object)
def aws_url(key):   return f"{AWS_HOST}/{BUCKET}/{key}"
def proxy_url(key): return f"{PROXY_HOST}/{key}"

# key -> DuckDB read_parquet target for each front door
def aws_s3(key):    return f"s3://{BUCKET}/{key}"
def proxy_https(key): return proxy_url(key)

ENDPOINTS = {"aws-direct": (aws_url, aws_s3), "sourcecoop-proxy": (proxy_url, proxy_https)}


def _new_con():
    con = duckdb.connect()
    con.sql("INSTALL httpfs; LOAD httpfs;")
    con.sql(f"SET threads={THREADS};")
    # Anonymous, path-style secret for the direct AWS bucket; https reads need none.
    con.sql(
        "CREATE OR REPLACE SECRET sc (TYPE S3, KEY_ID '', SECRET '', "
        "REGION 'us-west-2', ENDPOINT 's3.us-west-2.amazonaws.com', "
        "URL_STYLE 'path', USE_SSL 'true');"
    )
    return con


def discover_keys():
    con = _new_con()
    rows = con.sql(
        f"SELECT file FROM glob('s3://{BUCKET}/{DATASET}/h0=*/data_0.parquet') ORDER BY file"
    ).df()["file"].tolist()
    keys = [f.split(f"{BUCKET}/", 1)[1] for f in rows]
    if not keys:
        raise SystemExit(f"no files under s3://{BUCKET}/{DATASET}")
    return keys


# --- raw HTTP probes --------------------------------------------------------
def footer_latency_ms(url_for, keys):
    """Median wall time of a small ranged GET (last FOOTER_BYTES). Warm session."""
    sess = requests.Session()
    samples = []
    for key in keys[:N_LATENCY]:
        url = url_for(key)
        # find size once, then range the tail
        h = sess.head(url, timeout=30)
        size = int(h.headers.get("Content-Length", "0")) or None
        rng = f"bytes=-{FOOTER_BYTES}" if size is None else f"bytes={max(0,size-FOOTER_BYTES)}-{size-1}"
        t = time.perf_counter()
        r = sess.get(url, headers={"Range": rng}, timeout=30)
        r.content  # force read
        samples.append((time.perf_counter() - t) * 1000)
    return statistics.median(samples), min(samples)


def single_stream_mbps(url_for, keys):
    """Median MB/s over N distinct full-object GETs (distinct keys avoid caching)."""
    sess = requests.Session()
    rates = []
    for key in keys[:N_STREAM]:
        url = url_for(key)
        t = time.perf_counter()
        n = 0
        with sess.get(url, stream=True, timeout=120) as r:
            for chunk in r.iter_content(1 << 20):
                n += len(chunk)
        dt = time.perf_counter() - t
        rates.append((n / 1e6) / dt)
    return statistics.median(rates), max(rates)


def aggregate_mbps(url_for, keys):
    """Aggregate MB/s reading AGG_OBJS distinct objects at AGG_CONC concurrency."""
    sel = keys[:AGG_OBJS]

    def fetch(key):
        n = 0
        with requests.get(url_for(key), stream=True, timeout=120) as r:
            for chunk in r.iter_content(1 << 20):
                n += len(chunk)
        return n

    t = time.perf_counter()
    with ThreadPoolExecutor(max_workers=AGG_CONC) as ex:
        total = sum(ex.map(fetch, sel))
    dt = time.perf_counter() - t
    return (total / 1e6) / dt


# --- DuckDB query probes ----------------------------------------------------
def duckdb_query(target_for, keys, sql_template):
    """Run a query over all files via a fresh connection. Returns seconds."""
    con = _new_con()
    targets = [target_for(k) for k in keys]
    lst = "[" + ",".join(f"'{t}'" for t in targets) + "]"
    t = time.perf_counter()
    con.sql(sql_template.format(files=lst)).fetchall()
    return time.perf_counter() - t


def main():
    print(f"dataset : {DATASET}")
    keys = discover_keys()
    print(f"files   : {len(keys)}   threads={THREADS}\n")

    COUNT_SQL = "SELECT count(*) FROM read_parquet({files})"
    SUM_SQL = f"SELECT sum({COLUMN}) FROM read_parquet({{files}})"

    for name, (url_for, target_for) in ENDPOINTS.items():
        print(f"=== {name} ===")
        med_ms, min_ms = footer_latency_ms(url_for, keys)
        print(f"  footer latency (last {FOOTER_BYTES//1024}KiB) : median {med_ms:6.1f} ms   best {min_ms:6.1f} ms  (n={min(N_LATENCY,len(keys))})")
        med_mbps, max_mbps = single_stream_mbps(url_for, keys)
        print(f"  single-stream throughput           : median {med_mbps:6.1f} MB/s  peak {max_mbps:6.1f} MB/s  (n={N_STREAM})")
        agg = aggregate_mbps(url_for, keys)
        print(f"  aggregate throughput (c={AGG_CONC}, {AGG_OBJS} obj) : {agg:6.1f} MB/s")
        for label, sql in (("count(*) [footers]", COUNT_SQL), (f"sum({COLUMN}) [1-col scan]", SUM_SQL)):
            cold = duckdb_query(target_for, keys, sql)
            warm = min(duckdb_query(target_for, keys, sql) for _ in range(2))
            print(f"  duckdb {label:<22} : cold {cold:5.1f}s   warm {warm:5.1f}s")
        print()


if __name__ == "__main__":
    main()
