# Private Data Access

The server supports private STAC catalogs and private S3 buckets. Query credentials are supplied per-call and scoped to that request only — they are never logged, cached, or shared between clients. Catalog scope is different: it is a property of the deployment, not an argument.

## Private STAC catalog

Point the deployment at its own catalog and supply the bearer token through the environment:

```yaml
env:
  - name: STAC_CATALOG_URL
    value: https://your-app.example.org/stac/catalog.json
  - name: STAC_CATALOG_TOKEN
    valueFrom:
      secretKeyRef: { name: stac-catalog, key: token }
```

The token is forwarded as `Authorization: Bearer <token>` when fetching catalog JSON, and never travels in a JSON-RPC body.

::: warning Catalog scope is not a tool argument
`get_stac_details`, `get_collection` and `browse_stac_catalog` took `catalog_url` and
`catalog_token` until [#420](https://github.com/boettiger-lab/mcp-data-server/issues/420).
They do not any more: an argument the model supplies is an argument the model can change,
which let an agent reach datasets outside the ones its app configures. A client that still
sends them is not broken — unknown arguments are dropped during validation — but the value
is ignored and lookups resolve against the deployment's own catalog.

A client that already holds the STAC JSON can still skip the fetch entirely by passing it
inline as `collection` (or `catalog` for `browse_stac_catalog`). That remains the primary
contract; see [catalog sourcing](https://github.com/boettiger-lab/mcp-data-server/blob/main/docs/architecture/catalog-sourcing.md).
:::

Running a private app and want the model to see *only* the collections you configure? Set
`STAC_DISCOVERY=0` and `browse_stac_catalog` / `catalog://list` are not registered at all —
they never appear in `tools/list`, so the model is not told about a tool it cannot call.
Per-dataset lookup (`get_stac_details`, `get_collection`, `catalog://{id}`) is unaffected.

::: tip Serving a private catalog
If you use oauth2-proxy for browser access, add a parallel nginx `auth_request` bypass for the `/stac/` path that accepts a static shared token via the `Authorization` header. This allows the MCP server to fetch catalog metadata without a browser OAuth session.
:::

## Private S3 data

Pass S3 credentials directly to the `query` tool:

```json
{
  "tool": "query",
  "arguments": {
    "sql_query": "SELECT * FROM read_parquet('s3://my-private-bucket/data/**') LIMIT 10",
    "s3_key": "YOUR_ACCESS_KEY_ID",
    "s3_secret": "YOUR_SECRET_ACCESS_KEY",
    "s3_endpoint": "minio.example.org"
  }
}
```

`s3_endpoint` defaults to `s3-west.nrp-nautilus.io` if omitted.

## Mixing private and public data

Use `s3_scope` when a query reads from both a private and the public S3 endpoint, so DuckDB routes each path to the correct endpoint:

```json
{
  "tool": "query",
  "arguments": {
    "sql_query": "SELECT a.h8, b.value FROM read_parquet('s3://private-wyoming/...') a JOIN read_parquet('s3://public-data/...') b ON a.h8 = b.h8 AND a.h0 = b.h0",
    "s3_key": "YOUR_ACCESS_KEY_ID",
    "s3_secret": "YOUR_SECRET_ACCESS_KEY",
    "s3_endpoint": "minio.example.org",
    "s3_scope": "s3://private-wyoming"
  }
}
```

## Security properties

| Concern | How it is handled |
|---|---|
| Credential bleed between clients | Each request uses a separate `duckdb.connect(":memory:")` — secrets are connection-scoped and destroyed on close |
| Credentials in server logs | `CREATE SECRET` statements are constructed internally and never written to stderr |
| Credentials in transit | All traffic is TLS-terminated at the ingress |
| Credential persistence | `stateless_http=True` — no session state survives between requests |

## Deploying private apps without a separate server

Private geo-agent apps can share the public MCP server endpoint and pass credentials per-call. This avoids maintaining a separate server deployment per app while ensuring all apps benefit from server improvements automatically.
