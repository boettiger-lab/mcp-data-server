# Running an app on a mirror during an S3 outage

When the primary NRP Ceph endpoint (`s3-west.nrp-nautilus.io`) is unavailable,
an app can be pointed at a public **mirror** of the `public-*` buckets and keep
working. This is app-driven — nothing needs to change on the primary server.

The reference mirror is **`minio.carlboettiger.info`** (MinIO): a drop-in copy of
the NRP `public-*` buckets — same bucket names, same catalog structure,
self-consistent asset hrefs, public/anonymous reads, with CORS + HTTP range
enabled for browser access. (A partial AWS mirror also exists on source.coop; see
[architecture.md](architecture.md) and issue #260. MinIO is the more complete
drop-in.)

## The two data surfaces

An app reads data over **two independent paths**, and both must be pointed at the
mirror to fully ride out an outage:

| Surface | Fetched by | Through the MCP server? |
| --- | --- | --- |
| Collection JSON, PMTiles, COGs (map layers) | the browser (and TiTiler) | **No** — client-side |
| SQL analytics (`query` tool) | the LLM → MCP server → DuckDB | **Yes** |

Because these are independent, they are switched separately.

## 1. Map layers (client-side)

The map layers are fetched directly from the URLs in your app config
(`layers-input.json`) — they never touch the MCP server. Point them at the mirror:

- `catalog`:
  `https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json`
  → `https://minio.carlboettiger.info/public-data/stac/catalog.json`
- every `collection_url`: swap the host
  `s3-west.nrp-nautilus.io` → `minio.carlboettiger.info`
  (the path is unchanged — the bucket names are identical)

That is the only layer change. The mirror's collection JSONs are self-consistent —
their PMTiles / COG / parquet asset hrefs already point at the mirror — so
everything cascades. Leave `titiler_url` as `https://titiler.nrp-nautilus.io`:
TiTiler reads the mirror's COG URLs server-side (verified).

## 2. Query / analytics (through the MCP server)

Query paths are the canonical `s3://public-*` form. These are **identical across
the primary and the mirror** (same bucket names) — only *where they resolve*
changes, which is the server's endpoint config, not the query.

Point your app's **`mcp_url`** at an MCP head configured for the mirror — e.g. a
head deployed with `S3_DEFAULT_ENDPOINT=minio.carlboettiger.info` (see
[deployment.md](deployment.md) and issue #268). All `s3://public-*` reads then
resolve to the mirror, with no per-query changes and no credentials (the mirror
is anonymous).

Alternatively, a client can pass `s3_endpoint` + `s3_scope` per query (issue
#264) to route to the mirror without switching `mcp_url`.

> **Known caveat (client-side).** geo-agent's `get_schema` forwards the app's
> cached collection *inline* to the server, so on a mirror-repointed app the
> `read_parquet` path it returns is an `https://<mirror>/…` URL — and DuckDB
> cannot glob generic HTTP (needed for `hex/h0=*` datasets). The query path must
> use the canonical `s3://public-*` form (which the mirror head routes
> correctly). This is being addressed so the browser-href and query-path
> concerns are cleanly separated; until then, hex/analytics queries on a
> repointed app may need the `s3://` form explicitly.

## Reverting

After the outage, flip the `catalog` / `collection_url` hosts back to
`s3-west.nrp-nautilus.io` and point `mcp_url` back at the default MCP server.

## Zero-touch alternative (infrastructure)

If failover is handled at the DNS/proxy layer — resolving
`s3-west.nrp-nautilus.io` to the mirror — then **all four paths** (collection
JSON, PMTiles, COG, and query) redirect transparently with **no app or server
changes at all**. That is the cleanest option when it's available, since every
asset href and query path is already written against `s3-west`.

## Why it's this simple

The server is a stateless, env-configured data-access head; apps carry their own
per-dataset links from any source. See
[architecture/catalog-sourcing.md](../architecture/catalog-sourcing.md) for the
"carry the links" model this failover relies on.
