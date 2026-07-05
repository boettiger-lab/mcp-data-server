"""Deployment-level S3 endpoint configuration.

Single source of truth for the default S3 backend (#268/#271): one env surface
(S3_DEFAULT_ENDPOINT / S3_DEFAULT_URL_STYLE / S3_DEFAULT_USE_SSL) drives both
DuckDB connection factories — the per-request query engine
(server.get_isolated_db) and the persistent tile connection
(tiles.db.build_tile_connection) — so repointing a deployment at another
backend (MinIO, source.coop, ...) via env covers query AND hex tiles.

Precursor to the data-driven source registry (#264): when routing becomes
registry-driven, this module is where the registry lives.
"""
import os


def sql_quote(value: str) -> str:
    """Escape a value for embedding in a single-quoted SQL string literal.

    Secret parameters are interpolated into CREATE SECRET statements; a stray
    quote would otherwise break the statement — and the resulting parser error
    can echo the statement (credentials included) back to the caller (#271).
    """
    return (value or "").replace("'", "''")


def default_endpoint() -> str:
    """The deployment's default S3 endpoint (bare host, no scheme).

    Unset = the NRP Ceph internal endpoint (back-compat)."""
    return os.environ.get("S3_DEFAULT_ENDPOINT", "rook-ceph-rgw-nautiluss3.rook")


def infer_use_ssl(endpoint: str) -> str:
    """In-cluster rook endpoints are plain http; everything else defaults to SSL."""
    return "false" if endpoint.startswith("rook") else "true"


def default_s3_secret_sql(name: str = "s3") -> str:
    """CREATE SECRET statement for the deployment's default (anonymous) S3 backend.

    Env is read at call time, not import time, so per-request connections pick
    up changes without a restart and tests can monkeypatch. USE_SSL is inferred
    from the endpoint unless S3_DEFAULT_USE_SSL overrides it.
    """
    endpoint = default_endpoint()
    url_style = os.environ.get("S3_DEFAULT_URL_STYLE", "path")
    use_ssl = (
        os.environ.get("S3_DEFAULT_USE_SSL") or infer_use_ssl(endpoint)
    ).strip().lower()
    return (
        f"CREATE OR REPLACE SECRET {name} ("
        f"TYPE S3, KEY_ID '', SECRET '', "
        f"ENDPOINT '{sql_quote(endpoint)}', URL_STYLE '{sql_quote(url_style)}', "
        f"USE_SSL '{sql_quote(use_ssl)}')"
    )
