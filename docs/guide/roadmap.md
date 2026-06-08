# Roadmap

This page describes where the project is heading. Items here are **directions, not
commitments** — the [Quick Start](./quickstart) and [Architecture](./architecture) pages
describe what the server does *today* (DuckDB SQL over Parquet on S3, H3 spatial indexing,
STAC-driven discovery). The roadmap is the bridge from that proven core toward the broader
[vision](./vision).

## Beyond tables: array data via Zarr

Today the server queries **tabular** cloud-native data (Parquet) through DuckDB. A growing
share of science data is **array** data — gridded rasters, imaging stacks, multidimensional
cubes — for which [Zarr](https://zarr.dev) and xarray are the cloud-native standard. We aim
to extend the server so an agent can discover and query Zarr arrays through the same
STAC-grounded interface it uses for tables, without leaving the cloud-native path.

This matters most for the **life sciences**, where spatially-resolved assays — spatial
transcriptomics, whole-slide microscopy, 3D-genome imaging — are natively array-shaped and
arrive at terabyte scale. Conventions like OME-Zarr and AnnData already point the way; the
missing piece is the agentic layer that makes them reachable.

## Hardware-accelerated engines (Polars / GPU)

The server confines agents to **validated cloud-native engines**. DuckDB on CPU is the
engine today. A working GPU-accelerated prototype —
[`mcp-gpu-data-server`](https://github.com/boettiger-lab/mcp-gpu-data-server), built on
Polars / cuDF with KvikIO for fast S3 reads and partition pruning — already exists and is
being benchmarked head-to-head against this CPU server
([tracked in issue #42](https://github.com/boettiger-lab/mcp-data-server/issues/42)).
Early findings are nuanced: S3-I/O-bound queries favor CPU, while compute-heavy queries on
already-loaded data favor GPU. The goal is to let an agent transparently use whichever
engine fits the query, all behind the same MCP interface.

## Broader domains

The architecture is **domain-agnostic** — any data with a tabular or array structure and an
indexable dimension fits the same pattern. Proven first at earth-observation scale, the
work is being extended to the life sciences in collaboration with a lab that co-developed
image-based spatial transcriptomics. As the [vision](./vision) describes, the aim is a
single agentic interface to the cloud-native stack across scientific domains.

## Smaller, more open models

The server already injects all query guidance at call time, so compact open models can
drive it. We are continuing to tune that guidance so increasingly small, locally-run open
models can perform real analyses reliably — keeping sensitive data on local hardware and
reducing dependence on closed models.

---

Have a use case that needs one of these sooner? Open an
[issue](https://github.com/boettiger-lab/mcp-data-server/issues) — concrete use cases drive
prioritization.
