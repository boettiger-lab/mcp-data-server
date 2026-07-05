# CRITICAL: Required Query Setup

**EVERY query must start with this setup:**

```sql
SET THREADS=100;
SET preserve_insertion_order=false;
SET enable_object_cache=true;
SET temp_directory='/tmp';
LOAD httpfs;
LOAD h3;
LOAD spatial;
CREATE OR REPLACE SECRET source_coop (TYPE S3, SCOPE 's3://us-west-2.opendata.source.coop', REGION 'us-west-2', ENDPOINT 's3.us-west-2.amazonaws.com', URL_STYLE 'path', USE_SSL 'true', KEY_ID '', SECRET '');
```

The default `s3` secret (for `s3://public-*` and other paths) is created by the
server per request, with its endpoint set by the deployment (`S3_DEFAULT_ENDPOINT`,
default the NRP Ceph internal endpoint). Do not create or override it in query SQL.

**Why these settings?**

- `THREADS=100` - Parallel S3 reads (I/O bound)
- `preserve_insertion_order=false` - Faster aggregation
- `enable_object_cache=true` - Reduces S3 requests
- `httpfs` - Required for S3 access
- `h3` - Required for H3 functions
- `spatial` - Required for `ST_*` functions (line-data exact mileage, GeoParquet inspection)
- `source_coop` secret - Anonymous, prefix-scoped fallback for the public source.coop mirror. DuckDB routes `s3://us-west-2.opendata.source.coop/...` paths to it automatically; `s3://public-*` paths still go to Ceph. Use mirror paths (returned by the STAC tools as `s3://us-west-2.opendata.source.coop/cboettig/<dataset>/...`) when the primary Ceph endpoint is unavailable.

**Note:** On the default NRP deployment, the server's `s3` secret points at the internal endpoint `rook-ceph-rgw-nautiluss3.rook` (only reachable from inside the k8s cluster; the public external endpoint is `s3-west.nrp-nautilus.io`, which needs `USE_SSL true` and `SET THREADS=2`). A deployment can repoint this default at another backend (e.g. a MinIO mirror) via `S3_DEFAULT_ENDPOINT` without any query changes.

You must read parquet datasets with from S3 using read_parquet().  There are no local tables.
