"""DuckDB-dialect SQL → Polars SQLContext translation.

The Polars engines execute the same `read_parquet('s3://…')` SQL the DuckDB
engine accepts, but Polars `SQLContext` needs each source pre-registered as a
LazyFrame (it has no inline table function) and speaks a narrower SQL dialect.
This module bridges that gap:

  - extract every read_parquet() path and register it as a LazyFrame under a
    deterministic alias, then rewrite the SQL to reference the alias;
  - resolve per-path S3 `storage_options` from the request + s3config (the
    Polars analogue of DuckDB's scoped-SECRET routing — Polars takes options
    per source, which lets us preserve the repo's BYO-bucket / scope semantics);
  - rewrite the handful of DuckDB functions with Polars equivalents;
  - reject the documented-unsupported subset loudly (raise UnsupportedSQL) so the
    caller returns an actionable error rather than silently wrong results.

Ideas (regexes, DPP, function rewrites) are adapted from the reference fork
`boettiger-lab/mcp-gpu-data-server` (sql_rewriter.py); the S3 resolution and the
loud-subset contract are new here. GPU-only I/O (kvikio) lands in PR 2.
"""
import os
import re
import sys

import polars as pl

import s3config
from engines.base import S3Request


class UnsupportedSQL(ValueError):
    """SQL uses a construct the Polars engines cannot express (dialect subset)."""


# read_parquet('path')  or  read_parquet('path', hive_partitioning=true, ...)
READ_PARQUET_RE = re.compile(
    r"read_parquet\(\s*'([^']+)'\s*(?:,\s*[^)]*?)?\)",
    re.IGNORECASE,
)

# DuckDB APPROX_COUNT_DISTINCT(expr) → COUNT(DISTINCT expr)
_APPROX_COUNT_DISTINCT_RE = re.compile(r"APPROX_COUNT_DISTINCT\s*\(", re.IGNORECASE)

# Any community-h3 extension function (h3_cell_to_parent, h3_h3_to_string, …):
# unavailable under Polars. Pre-computed h0..h11 columns are the supported path.
_H3_FUNC_RE = re.compile(r"\bh3_[a-z0-9_]+\s*\(", re.IGNORECASE)

# h0 partition predicates, for explicit DPP in the gpu-cudf reader (PR 2).
_H0_IN_RE = re.compile(r"\bh0\s+IN\s*\(([^)]+)\)", re.IGNORECASE)
_H0_EQ_RE = re.compile(r"\bh0\s*=\s*(-?\d+)", re.IGNORECASE)


def extract_parquet_sources(sql: str) -> dict[str, str]:
    """Map each distinct read_parquet('path') to a deterministic alias."""
    paths: dict[str, str] = {}
    for match in READ_PARQUET_RE.finditer(sql):
        path = match.group(1)
        if path not in paths:
            paths[path] = f"__tbl_{len(paths)}"
    return paths


def extract_h0_predicates(sql: str) -> frozenset[int] | None:
    """Extract integer h0 values from `h0 IN (...)` / `h0 = N` predicates.

    Returns a frozenset if any found, else None. Used by the gpu-cudf reader to
    prune hive partitions before reading (the lazy Polars reader gets this for
    free). Pure/​testable; the reader that consumes it arrives in PR 2.
    """
    values: set[int] = set()
    for m in _H0_IN_RE.finditer(sql):
        for v in m.group(1).split(","):
            v = v.strip()
            if v.lstrip("-").isdigit():
                values.add(int(v))
    for m in _H0_EQ_RE.finditer(sql):
        values.add(int(m.group(1)))
    return frozenset(values) if values else None


def guard_unsupported(sql: str) -> None:
    """Raise UnsupportedSQL for constructs outside the Polars dialect subset."""
    m = _H3_FUNC_RE.search(sql)
    if m:
        raise UnsupportedSQL(
            f"{m.group(0).rstrip('(').strip()}() and other h3_* functions are not "
            "available in the Polars/GPU engine. Use the pre-computed H3 index "
            "columns (h0..h11) directly; for a cross-resolution join, pick the "
            "coarser shared column (e.g. join on h8 + h0)."
        )


def rewrite_functions(sql: str) -> str:
    """Rewrite DuckDB-specific functions to their Polars SQL equivalents."""
    return _APPROX_COUNT_DISTINCT_RE.sub("COUNT(DISTINCT ", sql)


def substitute_aliases(sql: str, path_aliases: dict[str, str]) -> str:
    """Replace each read_parquet('path') occurrence with its table alias."""
    rewritten = sql
    for match in READ_PARQUET_RE.finditer(sql):
        rewritten = rewritten.replace(match.group(0), path_aliases[match.group(1)])
    return rewritten


def _truthy_ssl(value) -> bool:
    return str(value).strip().lower() == "true"


def _default_use_ssl(endpoint: str) -> bool:
    """use_ssl for the deployment default endpoint — S3_DEFAULT_USE_SSL override
    else inferred, mirroring s3config.default_s3_secret_sql exactly (so the Polars
    default backend and the DuckDB default `s3` secret agree). This is what a
    plain-HTTP MinIO/Ceph deployment relies on."""
    return _truthy_ssl(os.environ.get("S3_DEFAULT_USE_SSL")
                       or s3config.infer_use_ssl(endpoint))


def _options(endpoint: str, key: str | None, secret: str | None,
             region: str | None, use_ssl: bool) -> dict:
    """Build a Polars storage_options dict, anonymous unless creds are given.

    Key names mirror the reference fork's working config (endpoint_url /
    aws_region / allow_http / skip_signature), which Polars' object_store accepts.
    Always path-style (aws_virtual_hosted_style_request=false) to match DuckDB's
    URL_STYLE 'path' — required by Ceph/MinIO.
    """
    scheme = "https" if use_ssl else "http"
    url = endpoint if endpoint.startswith(("http://", "https://")) else f"{scheme}://{endpoint}"
    opts = {
        "endpoint_url": url,
        "aws_region": region or "us-east-1",
        "allow_http": "false" if use_ssl else "true",
        "aws_virtual_hosted_style_request": "false",
    }
    if key and secret:
        opts["aws_access_key_id"] = key
        opts["aws_secret_access_key"] = secret
    else:
        # Anonymous: empty creds make object_store send malformed signed requests
        # that Ceph/MinIO reject; skip signing instead.
        opts["skip_signature"] = "true"
    return opts


def resolve_storage_options(path: str, s3: S3Request) -> dict | None:
    """Resolve Polars storage_options for one read_parquet path.

    The Polars analogue of DuckDB's scoped-SECRET routing, resolved per source:

      1. Non-s3 paths (local files, http(s)) → None (Polars reads them directly).
      2. Per-request client endpoint (s3.s3_endpoint), applied to paths under
         s3.s3_scope — or to *all* s3 paths when no scope is given (matching the
         DuckDB engine, where an unscoped client secret owns every s3:// path).
      3. A source-registry entry whose secret scope prefixes the path.
      4. The deployment default endpoint.
    """
    if not path.startswith("s3://"):
        return None

    # (2) per-request bring-your-own endpoint / credentials. use_ssl inferred
    # from the endpoint, matching the DuckDB client_s3 secret.
    has_client = bool(s3.s3_endpoint) or bool(s3.s3_key and s3.s3_secret)
    if has_client:
        in_scope = (not s3.s3_scope) or path.startswith(s3.s3_scope)
        if in_scope:
            endpoint = s3.s3_endpoint or s3config.default_endpoint()
            return _options(endpoint, s3.s3_key, s3.s3_secret, None,
                            use_ssl=_truthy_ssl(s3config.infer_use_ssl(endpoint)))

    # (3) source-registry scoped secret (longest matching scope wins). use_ssl
    # from the secret's own field else inferred, matching source_secret_sql.
    best = None
    for src in s3config.get_sources():
        sec = src.get("secret") or {}
        scope = sec.get("scope")
        if sec.get("endpoint") and scope and path.startswith(scope):
            if best is None or len(scope) > len(best[0]):
                best = (scope, sec)
    if best:
        sec = best[1]
        return _options(
            sec["endpoint"], sec.get("key_id"), sec.get("secret"), sec.get("region"),
            use_ssl=_truthy_ssl(sec.get("use_ssl", s3config.infer_use_ssl(sec["endpoint"]))),
        )

    # (4) deployment default — honours S3_DEFAULT_USE_SSL (the plain-HTTP MinIO
    # case). This was the bug: inferring SSL sent https:// at a plaintext port.
    endpoint = s3config.default_endpoint()
    return _options(endpoint, None, None, None, use_ssl=_default_use_ssl(endpoint))


def _hive(path: str) -> bool:
    """Whether a path looks hive-partitioned (glob or an h0= segment)."""
    return "*" in path or "h0=" in path


# ---------------------------------------------------------------------------
# gpu-cudf reader: kvikio GPU-direct S3 I/O (adapted from the reference fork)
# ---------------------------------------------------------------------------
# Threshold below which kvikio's per-connection overhead loses to Polars' Rust
# object_store; small files read via Polars instead.
_KVIKIO_MIN_AVG_BYTES = 5 * 1024 * 1024
_H0_PATH_RE = re.compile(r"/h0=(\d+)/")


def _filter_files_by_h0(files: list[str], h0_values: frozenset[int]) -> list[str]:
    """Keep files whose /h0=N/ path component is in h0_values (DPP); files with
    no h0= component are always kept (non-partitioned)."""
    out = []
    for f in files:
        m = _H0_PATH_RE.search(f)
        if m:
            if int(m.group(1)) in h0_values:
                out.append(f)
        else:
            out.append(f)
    return out


def _s3fs_from_options(storage_options: dict):
    import s3fs
    endpoint = storage_options["endpoint_url"]
    if storage_options.get("skip_signature") == "true":
        return s3fs.S3FileSystem(anon=True, endpoint_url=endpoint)
    return s3fs.S3FileSystem(
        key=storage_options.get("aws_access_key_id"),
        secret=storage_options.get("aws_secret_access_key"),
        endpoint_url=endpoint,
    )


def _kvikio_download_one(args: tuple) -> tuple[bytes, int]:
    """Download one parquet file via kvikio pread (parallel chunked HTTP)."""
    import kvikio
    url, h0 = args
    f = kvikio.RemoteFile.open_http(url)
    n = f.nbytes()
    buf = bytearray(n)
    f.pread(buf, size=n, file_offset=0).get()
    return bytes(buf), h0


def _read_with_h0(df: "pl.DataFrame", source: str) -> "pl.DataFrame":
    """Inject the h0 hive-partition column from the file path if not present."""
    m = _H0_PATH_RE.search(source)
    if m and "h0" not in df.columns:
        return df.with_columns(pl.lit(int(m.group(1))).alias("h0"))
    return df


def _scan_cudf(path: str, storage_options: dict | None,
               h0_filter: frozenset[int] | None) -> "pl.LazyFrame":
    """GPU-direct read for gpu-cudf mode: glob → DPP → kvikio pread (large files)
    or Polars (small) → concat → lazy, for GPUEngine compute.

    kvikio downloads bytes on the host (parallel chunked HTTP), then Polars parses
    into CPU RAM (NOT cudf.read_parquet, which would materialise the whole table
    in VRAM and OOM). GPU compute runs on the collected LazyFrame. Any failure —
    kvikio/s3fs missing, a private (signed) bucket kvikio's open_http can't read,
    a glob miss — falls back to the plain host read so gpu-cudf degrades to gpu.
    """
    if storage_options is None:
        # Local file: nothing to accelerate.
        return pl.read_parquet(path, hive_partitioning=_hive(path)).lazy()
    try:
        endpoint = storage_options["endpoint_url"].rstrip("/")
        fs = _s3fs_from_options(storage_options)
        # Glob the pattern directly — s3fs expands wildcards, including the
        # mid-path globs our STAC paths use (hex/h0=*/data_0.parquet) and single
        # files. Only if that yields no parquet (e.g. a bare directory) recurse.
        pattern = path.removeprefix("s3://")
        raw = [f for f in fs.glob(pattern) if f.endswith(".parquet")]
        if not raw:
            base = pattern.rstrip("/").rstrip("*").rstrip("/")
            raw = fs.glob(base + "/**/*.parquet") or fs.glob(base + "/*.parquet")
        if not raw:
            raise FileNotFoundError(f"no parquet at {path}")
        files = [f"s3://{f}" for f in raw]

        if h0_filter:
            before = len(files)
            files = _filter_files_by_h0(files, h0_filter)
            print(f"  [cudf DPP] {path}: {before} → {len(files)} files", file=sys.stderr)
            if not files:
                return pl.LazyFrame()

        sizes = [fs.info(f.removeprefix("s3://"))["size"] for f in files]
        avg = (sum(sizes) / len(sizes)) if sizes else 0

        if avg < _KVIKIO_MIN_AVG_BYTES:
            # Small files: kvikio overhead dominates — read via Polars per file.
            dfs = [_read_with_h0(pl.read_parquet(f, storage_options=storage_options), f)
                   for f in files]
            return pl.concat(dfs, how="diagonal_relaxed").lazy()

        # Large files: kvikio parallel pread → BytesIO → Polars parse (CPU RAM).
        import concurrent.futures
        import io
        h0s = [int(m.group(1)) if (m := _H0_PATH_RE.search(f)) else 0 for f in files]
        urls = [f"{endpoint}/{f.removeprefix('s3://')}" for f in files]
        workers = min(len(urls), int(os.environ.get("KVIKIO_DOWNLOAD_WORKERS", "64")))
        print(f"  [kvikio] {path}: {len(urls)} files, avg {avg/1e6:.0f}MB, {workers} workers",
              file=sys.stderr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_kvikio_download_one, zip(urls, h0s)))
        dfs = []
        for raw_bytes, h0 in results:
            d = pl.read_parquet(io.BytesIO(raw_bytes))
            if "h0" not in d.columns:
                d = d.with_columns(pl.lit(h0).alias("h0"))
            dfs.append(d)
        return pl.concat(dfs, how="diagonal_relaxed").lazy()

    except Exception as e:
        print(f"cuDF I/O failed for {path} ({e}); falling back to host read",
              file=sys.stderr)
        return pl.read_parquet(path, hive_partitioning=_hive(path),
                               storage_options=storage_options).lazy()


def _lazyframe_for(path: str, s3: S3Request, want_gpu: bool, use_cudf_io: bool,
                   h0_filter: frozenset[int] | None) -> "pl.LazyFrame":
    """Build the LazyFrame for one read_parquet path, choosing the reader by mode.

    - CPU: lazy `scan_parquet` — streaming with projection/predicate pushdown.
    - GPU: cudf-polars cannot read a remote `s3://` scan itself (its GPU/KvikIO
      reader rejects the scheme, failing the collect), so read the parquet into
      host memory on CPU, then let GPUEngine compute on it. Confirmed necessary
      on RAPIDS 25.10.
    - gpu-cudf: kvikio GPU-direct read with explicit h0 partition pruning
      (_scan_cudf), which falls back to the host read on any failure.
    """
    storage_options = resolve_storage_options(path, s3)

    if want_gpu and use_cudf_io:
        return _scan_cudf(path, storage_options, h0_filter)

    kwargs = {"hive_partitioning": _hive(path)}
    if storage_options is not None:
        kwargs["storage_options"] = storage_options
    if want_gpu:
        return pl.read_parquet(path, **kwargs).lazy()
    return pl.scan_parquet(path, **kwargs)


def build_context(
    sql: str,
    s3: S3Request,
    want_gpu: bool = False,
    use_cudf_io: bool = False,
) -> tuple[str, "pl.SQLContext"]:
    """Translate DuckDB SQL into (rewritten_sql, SQLContext) for Polars execution.

    Registers each read_parquet path as a LazyFrame (reader chosen by mode via
    _lazyframe_for), rewrites the SQL to reference aliases, and applies the
    function rewrites. Raises UnsupportedSQL for out-of-subset constructs.
    """
    guard_unsupported(sql)

    # Explicit partition pruning only matters for the cudf-io reader (the lazy
    # Polars path prunes for free); extract h0 predicates once for it.
    h0_filter = extract_h0_predicates(sql) if (want_gpu and use_cudf_io) else None

    path_aliases = extract_parquet_sources(sql)
    ctx = pl.SQLContext()
    for path, alias in path_aliases.items():
        ctx.register(alias, _lazyframe_for(path, s3, want_gpu, use_cudf_io, h0_filter))

    rewritten = substitute_aliases(sql, path_aliases)
    rewritten = rewrite_functions(rewritten)
    return rewritten, ctx
