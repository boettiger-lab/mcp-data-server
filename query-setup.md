# CRITICAL: Required Query Setup

**EVERY query must start with this setup:**

```sql
SET THREADS=100; 
SET preserve_insertion_order=false; 
SET enable_object_cache=true; 
SET temp_directory='/tmp';
INSTALL httpfs; LOAD httpfs; 
INSTALL h3 FROM community; LOAD h3;
CREATE OR REPLACE SECRET s3 (TYPE S3, ENDPOINT 'rook-ceph-rgw-nautiluss3.rook', URL_STYLE 'path', USE_SSL 'false', KEY_ID '', SECRET '');
```

**Why these settings?**

- `THREADS=100` - Parallel S3 reads (I/O bound)
- `preserve_insertion_order=false` - Faster aggregation
- `enable_object_cache=true` - Reduces S3 requests
- `httpfs` - Required for S3 access
- `h3` - Required for H3 functions

**Note:** `rook-ceph-rgw-nautiluss3.rook` is an internal endpoint that only your tool running on k8s can access. The publicly accessible external endpoint is `s3-west.nrp-nautilus.io`, which requires `USE_SSL true` and `SET THREADS=2`. Always use the internal endpoint to run queries.

You must read parquet datasets with from S3 using read_parquet().  There are no local tables.
