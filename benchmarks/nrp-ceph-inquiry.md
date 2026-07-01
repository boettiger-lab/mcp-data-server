# Draft inquiry to NRP: West Ceph RGW read throughput ceiling

**Context.** We run DuckDB-based geospatial query + tile services on NRP (`biodiversity` ns),
reading partitioned Parquet from the **West Ceph pool** via the internal endpoint
`rook-ceph-rgw-nautiluss3.rook` (public `s3-west.nrp-nautilus.io`). Read throughput is our
main performance limiter. We benchmarked the raw S3 read path (engine-independent boto3, so
this is the fabric/RGW, not our query engine) and cross-checked against NRP Prometheus.

## Client-side measurements
us-west nodes (incl. a 100 Gb/s DTN), internal endpoint, reading a 12 GB Parquet dataset,
compressed wire Gb/s; fabric is nominally ~100 Gb/s.
- **Single pod** (64–256 parallel GETs): ~**7 Gb/s** (flat across concurrency → saturated
  upstream of the client; occasionally bursts to ~31).
- **4 pods on distinct nodes, concurrent**: each ~2.5 Gb/s, **aggregate ~10 Gb/s** (not 4×7)
  → a **shared** ceiling, not per-client.
- **8 pods (~1024 connections)**: RGW connection resets (`IncompleteRead`) → connection-count
  ceiling.

## Prometheus (the RGW's own metrics — `67.58.50.67`, the West rook-ceph-exporter)
- **`ceph_rgw_get_b` peak read = 19.5 Gb/s over BOTH the last 1h and last 24h** — i.e. across a
  full day of all-cluster load the West RGW tier never exceeds ~20 Gb/s. Every other RGW
  exporter instance is ~0 during our reads (single West gateway tier).
- **`ceph_rgw_get_initial_lat` median = 118 ms/GET (24h baseline)** — inherent per-request
  latency, not caused by our load. This is why a many-small-range-request reader (DuckDB
  httpfs) is latency-bound here.

So: a single client caps ~7 Gb/s, many clients share ~10–20 Gb/s, the tier's hard ceiling is
~20 Gb/s (~20% of a 100 Gb/s fabric), with ~120 ms GET latency and connection resets under
high concurrency.

## For comparison — same data on AWS us-west-2 (source.coop mirror), from NRP over Internet2
- Single pod ~5.6 Gb/s; **4 pods concurrent aggregate ~19 Gb/s** (near per-client scaling) —
  AWS S3 does **not** present the single-shared-gateway ceiling. (It would keep scaling with
  more clients; single-pod degrades at very high concurrency from AWS anonymous throttling.)

## Questions
1. Is ~20 Gb/s the expected read ceiling for the West RGW gateway tier? How many RGW daemons
   back `rook-ceph-rgw-nautiluss3.rook`, and how is it fronted/load-balanced?
2. Is ~120 ms median GET latency expected? It dominates our read performance.
3. Can per-client and/or aggregate read throughput be raised (more RGW replicas, ingress/
   HAProxy tuning, a higher-throughput or per-tenant endpoint)?
4. Recommended per-client concurrency ceiling to avoid connection resets (~1024 conns broke it)?
5. For large sequential/bulk reads, is there a recommended lower-latency/higher-throughput
   path (dedicated RGW, RADOS, or client settings)?

**Reproduce:** `benchmarks/s3-rgw-probe.py`, `s3-io-ceiling-bench.py` +
`benchmarks/k8s/*.yaml` (github.com/boettiger-lab/mcp-data-server, branch `bench/s3-ceiling`;
tracking issue #250). Prometheus: `https://prometheus.nrp-nautilus.io` (`ceph_rgw_get_b`,
`ceph_rgw_get_initial_lat_*`).
