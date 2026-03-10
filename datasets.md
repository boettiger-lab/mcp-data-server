# SQL Data Guide

**IMPORTANT:** There are no local tables. You must read remote parquet datasets with `read_parquet('s3://...')`.

## Discovering datasets

Available datasets are described in a STAC catalog of harmonized, cloud-native data **co-located with this server** on the same Kubernetes cluster (enabling high-speed internal S3 reads). Use the `list_datasets` and `get_dataset` tools to browse available collections, their S3 parquet paths, and column schemas.

## Choosing the right format

Most datasets are available in multiple formats. **Pick the right one for the task:**

| Format | Path pattern | When to use |
|--------|-------------|-------------|
| **H3 hex parquet** | `…/hex/**` | Spatial joins, overlap analysis, area calculations, cross-dataset queries. **Always prefer this for any spatial operation.** |
| **Flat parquet** | `….parquet` | Single-dataset filtering, column value lookups, aggregations, checking unique values. No geometry overhead. |
| **GeoParquet** (flat parquet with a `geometry`/`geom` column) | same as flat | **Almost never use the geometry column.** H3 hex joins are faster and simpler. Only read geometry if the user explicitly asks for WKT output or polygon shapes. |

**Rule of thumb:** If your query involves two or more datasets, or any concept of "overlap", "within", "intersection", or "area of X inside Y" → use the H3 hex paths and join on `h8` (+ `h0` for partition pruning). Do NOT use `ST_Intersects`, `ST_Area`, or any geometry functions when an H3 join will answer the question.
