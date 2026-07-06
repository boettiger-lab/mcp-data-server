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

The server owns endpoint routing via a data-driven **source registry**
(`s3config.py`, #264/#271): it rewrites known mirror hrefs to globbable `s3://`
paths and creates the matching (anonymous, prefix-scoped) DuckDB secrets. The
`query` paths themselves are the `s3://` form, and the STAC tools return them
ready to use — you don't hand-edit query SQL. There are two ways to route
queries to the mirror during an outage:

**a. Point `mcp_url` at a mirror-configured MCP head (recommended).** Deploy the
server with `S3_DEFAULT_ENDPOINT=<mirror host>` (and, so the mirror's own hrefs
rewrite cleanly, register it via `S3_SOURCES` — see [deployment.md](deployment.md)
and issues #268/#264):

```
S3_DEFAULT_ENDPOINT=minio.carlboettiger.info
S3_SOURCES='[{"name":"minio","https_prefix":"https://minio.carlboettiger.info/","s3_prefix":"s3://"}]'
STAC_CATALOG_URL=https://minio.carlboettiger.info/public-data/stac/catalog.json
```

Point the app's `mcp_url` at this head. `s3://public-*` reads (and hex-tile
*reads*, which now honor the same default endpoint — #275) resolve to the
mirror, anonymously, with no per-query changes.

> **Two caveats on a mirror-configured head:**
> - **Hex-tile builds write.** `register_hex_tiles` writes its pyramid output to
>   `s3://public-output` on the default backend. NRP Ceph accepts those writes
>   anonymously; the mirror bucket likely requires credentials, so expect builds
>   to fail at write time until the scoped write secret in
>   [#279](https://github.com/boettiger-lab/mcp-data-server/issues/279) is in
>   place. `query` reads are unaffected.
> - **Booting mid-outage.** With `STAC_CATALOG_URL` swapped to the mirror as
>   above, the head starts normally. If you keep the Ceph catalog URL (or the
>   mirror lacks a root catalog), also set `STAC_ALLOW_DEGRADED_START=true`
>   (#262) or the fail-fast startup will crashloop until the primary returns —
>   discovery is empty in that mode, but inline/known-path queries all work.

**b. Or keep your `mcp_url` and pass routing per query (#264/#267).** For a
source the server doesn't know, `get_stac_details` now returns the derived
`s3://` path **and an in-band ⚠️ line telling you exactly what to pass** — e.g.
`s3_endpoint='minio.carlboettiger.info', s3_scope='s3://public-<name>'`
(anonymous; add `s3_key`/`s3_secret` if private). Following that instruction
routes the read to the mirror.

> **When mixing sources, always pass `s3_scope`.** A per-request `s3_endpoint`
> (or credentials) **without** a scope applies to *every* `s3://` path in that
> query and disables the server default for the request (deterministic since
> #273) — correct for a query hitting only your bucket, wrong for one mixing
> your bucket with catalog data. The scope confines your endpoint to its prefix.

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
