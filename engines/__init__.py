"""Query engine registry.

`select_engine()` reads QUERY_ENGINE (default "duckdb") and returns the matching
engine. Engine modules are imported lazily per mode so the default DuckDB path
never imports Polars — a lean base image with no Polars/GPU deps still runs.
"""
import os
import sys

from engines.base import QueryEngine, S3Request

__all__ = ["QueryEngine", "S3Request", "select_engine"]


def select_engine() -> QueryEngine:
    mode = os.environ.get("QUERY_ENGINE", "duckdb").strip().lower()

    if mode in ("", "duckdb"):
        from engines.duckdb_engine import DuckDBEngine
        engine: QueryEngine = DuckDBEngine()
    elif mode in ("polars-cpu", "polars-gpu", "polars-gpu-cudf"):
        from engines.polars_engine import PolarsEngine
        engine = PolarsEngine(mode)
    else:
        raise ValueError(
            f"QUERY_ENGINE={mode!r} is not recognised. Valid values: "
            "duckdb, polars-cpu, polars-gpu, polars-gpu-cudf."
        )

    print(f"⚙️  Query engine: {engine.name}", file=sys.stderr)
    return engine
