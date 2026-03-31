# Deployment

The server runs on the [NRP Nautilus](https://nrp.ai) Kubernetes cluster.

## Hosted endpoint

```
https://duckdb-mcp.nrp-nautilus.io/mcp
```

- 2 replicas, each cloning this repo at startup and running `server.py` via `uv`
- 16 Gi RAM requested, up to 160 Gi / 16 CPU per pod
- HAProxy ingress with CORS enabled, 10-minute query timeout, 1-hour SSE tunnel timeout

## Kubernetes manifests

```bash
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ingress.yaml
```

## Redeploying after a push

After pushing changes to `main`, restart the pods so they re-clone the repo:

```bash
kubectl rollout restart deployment/duckdb-mcp
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `STAC_CATALOG_URL` | NRP public catalog | URL of the STAC catalog to serve |
| `THREADS` | 100 | DuckDB thread count (S3 workloads are I/O-bound) |
| `PORT` | 8000 | HTTP server port |
