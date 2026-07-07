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


def _lazyframe_for(path: str, s3: S3Request, want_gpu: bool,
                   use_cudf_io: bool) -> "pl.LazyFrame":
    """Build the LazyFrame for one read_parquet path, choosing the reader by mode.

    - CPU: lazy `scan_parquet` — streaming with projection/predicate pushdown.
    - GPU: cudf-polars cannot read a remote `s3://` scan itself (its GPU/KvikIO
      reader rejects the scheme, failing the whole collect), so read the parquet
      into host memory on CPU via object_store, then let GPUEngine compute on it.
      This is the reference fork's pattern, confirmed necessary on RAPIDS 25.10.
    - use_cudf_io (gpu-cudf): the kvikio GPU-direct reader (pread → BytesIO →
      read_parquet, with h0 DPP) is a later step; until it lands, gpu-cudf uses
      the same host read as gpu, so it is correct (just not yet I/O-accelerated).
    """
    kwargs = {"hive_partitioning": _hive(path)}
    storage_options = resolve_storage_options(path, s3)
    if storage_options is not None:
        kwargs["storage_options"] = storage_options

    if want_gpu:
        # Eager host read, then .lazy() so GPUEngine runs the compute on GPU
        # without attempting a remote read it can't do.
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

    path_aliases = extract_parquet_sources(sql)
    ctx = pl.SQLContext()
    for path, alias in path_aliases.items():
        ctx.register(alias, _lazyframe_for(path, s3, want_gpu, use_cudf_io))

    rewritten = substitute_aliases(sql, path_aliases)
    rewritten = rewrite_functions(rewritten)
    return rewritten, ctx
