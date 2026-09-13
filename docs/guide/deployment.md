# Deployment

The server runs on the [NRP Nautilus](https://nrp.ai) Kubernetes cluster.

## Hosted endpoint

```
https://duckdb-mcp.nrp-nautilus.io/mcp
```

- 6 replicas (prod), each running the baked image directly (`server.py`) — code is built into the image, not cloned at startup
- 16 Gi RAM requested, up to 160 Gi / 16 CPU per pod
- HAProxy ingress with CORS enabled, 10-minute query timeout, 1-hour SSE tunnel timeout

## Kubernetes manifests

```bash
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ingress.yaml
```

## Redeploying

Code is baked into the image, so redeploying means rolling to a new image — not re-cloning.

- **dev** tracks the moving `:main` tag. After CI builds your merge to `main`, roll dev to pick it up:

  ```bash
  kubectl apply -f k8s/dev-deployment.yaml
  kubectl rollout restart deployment/dev-duckdb-mcp -n biodiversity
  ```

- **prod** pins an immutable `vX.Y.Z@sha256:<digest>`. Cut a release tag, then bump the digest in `k8s/deployment.yaml`, apply, and roll:

  ```bash
  kubectl apply -f k8s/deployment.yaml
  kubectl rollout restart deployment/duckdb-mcp -n biodiversity
  ```

See [`AGENTS.md` → Rollout workflow](https://github.com/boettiger-lab/mcp-data-server/blob/main/AGENTS.md) for the full release procedure (tagging, reading the digest, verifying convergence).

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `STAC_CATALOG_URL` | NRP public catalog | URL of the STAC catalog to serve (the address *this server* reads) |
| `STAC_PUBLIC_CATALOG_URL` | `STAC_CATALOG_URL` | The same catalog's client-reachable URL. Set it only when the server reads the catalog over an address clients can't resolve (e.g. an in-cluster mirror); this is what `browse_stac_catalog` and `GET /version` advertise |
| `STAC_CATALOG_TOKEN` | *(unset)* | Bearer token for `STAC_CATALOG_URL`, when the deployment's catalog is private. Sent as `Authorization: Bearer <token>` on catalog fetches; never travels in a JSON-RPC body. The env-side replacement for the removed `catalog_token` tool argument (#420) |
| `STAC_DISCOVERY` | `1` (on) | Whether to register the whole-catalog discovery surface — `browse_stac_catalog` and the `catalog://list` resource. `0`/`false`/`off` leaves them out of `tools/list` entirely, so a curated app's model sees only what the app configures. Per-dataset lookup (`get_stac_details`, `get_collection`, `catalog://{id}`) is unaffected |
| `THREADS` | 100 | DuckDB thread count (S3 workloads are I/O-bound) |
| `PORT` | 8000 | HTTP server port |
