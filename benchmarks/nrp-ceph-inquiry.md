# Draft inquiry to NRP: West Ceph RGW read throughput ceiling

**Context.** We run DuckDB-based geospatial query + tile services on NRP (`biodiversity` ns),
reading partitioned Parquet from the **West Ceph pool** via the internal endpoint
`rook-ceph-rgw-nautiluss3.rook` (public `s3-west.nrp-nautilus.io`). Read throughput is our
main performance limiter, so we benchmarked the raw S3 read path (engine-independent — plain
boto3 parallel GETs, so this is the fabric/RGW, not our query engine).

**Setup.** us-west nodes (incl. a 100 Gb/s DTN, `k8s-100g-dtn-6`), internal endpoint, reading
a 12 GB Parquet dataset (whole-object GETs). Metrics = compressed wire Gb/s. Fabric is
nominally ~100 Gb/s.

**What we see:**
- **Single pod**, 64–256 parallel GETs: plateaus at **~7 Gb/s** (flat across concurrency →
  saturated upstream of the client). Occasionally bursts to ~31 Gb/s — **high variance**.
- **4 pods on 4 distinct nodes, reading simultaneously**: each drops to **~2.5 Gb/s**,
  **aggregate ~10 Gb/s** — *not* 4×7. → a **shared read ceiling (~10 Gb/s aggregate)** across
  concurrent clients, not per-client.
- **8 pods (~1024 concurrent connections)**: RGW connection resets
  (`IncompleteRead` / "Connection broken") → a **connection-count ceiling** as well.

So a single client can't exceed ~7 Gb/s and many clients share ~10 Gb/s — ~10% of the
~100 Gb/s fabric — with connection resets under high concurrency.

**Questions:**
1. Is ~7–10 Gb/s the expected read ceiling for the West RGW gateway? How many RGW instances
   back `rook-ceph-rgw-nautiluss3.rook`, and how is it load-balanced?
2. Can per-client and/or aggregate read throughput be raised (more RGW replicas, ingress/
   HAProxy tuning, a higher-throughput endpoint)?
3. What concurrency ceiling per client do you recommend to avoid connection resets?
4. Why the large variance (7 → 31 Gb/s on the same node/dataset)? Contention, or RGW
   placement/affinity?
5. For large sequential reads, is there a recommended lower-latency/higher-throughput path
   (e.g. a specific endpoint, RADOS, or client settings)?

**Reproduce:** `benchmarks/s3-rgw-probe.py` + `benchmarks/k8s/s3-rgw-concurrency-job.yaml`
(github.com/boettiger-lab/mcp-data-server, branch `bench/s3-ceiling`; tracking issue #250).
