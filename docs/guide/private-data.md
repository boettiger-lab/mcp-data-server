# Private Data Access

The server supports private STAC catalogs and private S3 buckets. Credentials are supplied per-call and scoped to that request only — they are never logged, cached, or shared between clients.

## Private STAC catalog

Pass a bearer token alongside the catalog URL:

```json
{
  "tool": "browse_stac_catalog",
  "arguments": {
    "catalog_url": "https://your-app.example.org/stac/catalog.json",
    "catalog_token": "YOUR_BEARER_TOKEN"
  }
}
```

Pass the same `catalog_url` and `catalog_token` to `get_stac_details` as well.

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
