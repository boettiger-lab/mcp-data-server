# Running an app on a mirror during an S3 outage

When the primary NRP Ceph endpoint (`s3-west.nrp-nautilus.io`) is unavailable, an
app can be pointed at a public **mirror** of the `public-*` buckets and keep
working. This is app-driven — nothing changes on the primary server.

The reference mirror is **`minio.carlboettiger.info`**: a drop-in copy of the NRP
`public-*` buckets — same bucket names, same paths, self-consistent asset hrefs,
anonymous reads, CORS + HTTP range enabled for browsers. A mirror-configured MCP
head already reads from it at **`https://duckdb-mcp.carlboettiger.info/mcp`**.

> **Pointed here mid-outage and just need to query?** Read
> [For agents querying through MCP](#agents) — those five rules are the whole
> instruction, and this page is the only place they live. Everything else here is
> for whoever switches an app over.

## Runbook

An app reads over **two independent surfaces, and both must be switched** — one
alone leaves the app half-broken (layers load but queries 503, or the reverse):

| Surface | Fetched by | Switch by |
| --- | --- | --- |
| Catalog + collection JSON, PMTiles, COGs | the browser | app config |
| SQL analytics (`query`) | LLM → MCP server → DuckDB | `mcp_url` |

In the app config (`layers-input.json`):

1. **`catalog` and every `collection_url`** — swap the host, keep the path:
   `s3-west.nrp-nautilus.io` → `minio.carlboettiger.info`.
   Bucket names are identical, so nothing else changes. The mirror's collection
   JSONs are self-consistent (their PMTiles/COG/parquet hrefs already point at
   the mirror), so assets cascade on their own.
2. **`mcp_url`** → `https://duckdb-mcp.carlboettiger.info/mcp`.
3. **`titiler_url`** → leave as `https://titiler.nrp-nautilus.io`. TiTiler reads
   the mirror's COG URLs server-side (verified).

If the app's `mcp_url` comes from a deployment env var rather than the config
file, that is a manifest change: `kubectl apply` it, not just a rollout restart.

### Verify before declaring it done

```bash
# 1. collection JSON on the mirror (expect 200 + JSON, not console HTML —
#    minio.carlboettiger.info is the object host; data.carlboettiger.info is the console)
curl -s https://minio.carlboettiger.info/public-padus/padus-4-1/fee/stac-collection.json | head -c 40

# 2. browser-side prerequisites on a PMTiles object (expect 206 + access-control-allow-origin)
curl -sD- -o /dev/null -H "Origin: https://<app-host>" -H "Range: bytes=0-99" \
  https://minio.carlboettiger.info/public-padus/padus-4-1/fee.pmtiles | grep -iE "^HTTP|access-control-allow-origin"

# 3. query surface: the MCP head actually reads the mirror
curl -s -X POST https://duckdb-mcp.carlboettiger.info/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"query","arguments":{
       "sql_query":"SELECT COUNT(*) FROM read_parquet('"'"'s3://public-padus/padus-4-1/fee.parquet'"'"')"}}}'
```

### Reverting

Flip both hosts back (`minio.carlboettiger.info` → `s3-west.nrp-nautilus.io`,
`mcp_url` → the default head) and redeploy. **This is a standing manual
obligation** for every app switched — see [Zero-touch alternative](#zero-touch-alternative)
for how to stop owing it.

## For agents querying through MCP during an outage {#agents}

- **Use the normal tools.** On a mirror-configured head, `browse_stac_catalog`,
  `get_stac_details`, and `get_collection` all work — the head serves a complete
  mirrored catalog. Do **not** apply outage advice that says to avoid them and
  use only `get_schema`; that applied to a partial source.coop fallback with no
  root catalog, and following it on a mirror head needlessly blinds you.
- **Use the paths the STAC tools return, verbatim.** The server owns endpoint
  routing (`s3config.py`): it rewrites known mirror hrefs to globbable `s3://`
  paths and creates the matching anonymous, prefix-scoped DuckDB secrets. Never
  hand-edit an endpoint into query SQL.
- **Internal vs public hosts.** A head's own read endpoint may be an in-cluster
  address (e.g. `minio-svc.minio.svc.cluster.local`); the URLs it hands you for
  client use are public. If `browse_stac_catalog` reports an in-cluster catalog
  root, that's a display bug (#346) — the public equivalent is the mirror host
  above; asset hrefs from `get_collection` are already public.
- **When mixing sources, always pass `s3_scope`.** A per-request `s3_endpoint`
  (or credentials) *without* a scope applies to every `s3://` path in that query
  and disables the server default for the request (deterministic since #273) —
  correct for a query hitting only your bucket, wrong for one mixing your bucket
  with catalog data.

## Deploying your own mirror-configured head

Set these and point `mcp_url` at it (#268/#264; see [deployment.md](deployment.md)):

```
S3_DEFAULT_ENDPOINT=minio.carlboettiger.info
S3_SOURCES='[{"name":"minio","https_prefix":"https://minio.carlboettiger.info/","s3_prefix":"s3://"}]'
STAC_CATALOG_URL=https://minio.carlboettiger.info/public-data/stac/catalog.json
```

`s3://public-*` reads — and hex-tile reads, which honor the same default
endpoint (#275) — then resolve to the mirror anonymously, with no per-query
changes. Two caveats:

- **Hex-tile builds write.** `register_hex_tiles` writes its pyramid to
  `s3://public-output` on the default backend, anonymously; the mirror must be
  open for anonymous Get/Put/List (`mc anonymous set public <alias>/public-output`).
  Verified end-to-end in [#279](https://github.com/boettiger-lab/mcp-data-server/issues/279).
  To redirect *only* tile output while the default stays on Ceph, use a scoped
  entry: `{"name":"minio_output","secret":{"endpoint":"minio.carlboettiger.info","scope":"s3://public-output"}}`.
- **Booting mid-outage.** With `STAC_CATALOG_URL` on the mirror the head starts
  normally. Keeping the Ceph catalog URL requires `STAC_ALLOW_DEGRADED_START=true`
  (#262) or fail-fast startup crashloops until the primary returns.

## Zero-touch alternative

If failover happens at the DNS/proxy layer — resolving `s3-west.nrp-nautilus.io`
to the mirror — then **all four paths** (collection JSON, PMTiles, COG, query)
redirect transparently with **no app or server changes**, and no revert to
remember. Every asset href and query path is already written against `s3-west`,
so this is the cleanest option whenever it's available, and it retires this
entire runbook.

## Why it's this simple

The server is a stateless, env-configured data-access head; apps carry their own
per-dataset links from any source. See
[architecture/catalog-sourcing.md](../architecture/catalog-sourcing.md) for the
"carry the links" model this failover relies on.
