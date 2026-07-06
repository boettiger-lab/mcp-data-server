"""Default query engine: DuckDB + httpfs (unchanged behaviour).

This is the historical `query()` path, now expressed behind the engine seam. It
reuses server.get_isolated_db() — the connection factory, its SETUP_SQL and its
per-request S3 SECRET model stay in server.py so existing behaviour and the test
surface are preserved exactly. `server` is imported lazily inside run() to avoid
an import cycle (server imports the engine registry at module load).
"""
import sys

from engines.base import QueryEngine, S3Request, render_preview, RESULT_LIMIT


class DuckDBEngine(QueryEngine):
    name = "duckdb"

    def run(self, sql_query: str, s3: S3Request) -> str:
        import server  # lazy: breaks the server ⇄ engines import cycle

        print(f"🔍 Executing: {sql_query}", file=sys.stderr)
        try:
            with server.get_isolated_db(
                s3_key=s3.s3_key,
                s3_secret=s3.s3_secret,
                s3_endpoint=s3.s3_endpoint,
                s3_scope=s3.s3_scope,
            ) as db:
                result = db.sql(sql_query)
                if result is None:
                    return "Command executed successfully."

                # Drop geometry columns — GEOMETRY('OGC:CRS84') crashes pandas
                # conversion (DuckDB: unsupported NumPy type) and is not useful
                # in tabular output.
                geom_cols = [
                    c for c, t in zip(result.columns, result.dtypes)
                    if "GEOMETRY" in str(t).upper()
                ]
                if geom_cols:
                    keep = [f'"{c}"' for c in result.columns if c not in geom_cols]
                    result = result.select(", ".join(keep))

                # Fetch one extra row to detect truncation without a COUNT scan.
                df = result.limit(RESULT_LIMIT + 1).df()
                return render_preview(df)
        except Exception as e:
            return f"SQL Error: {str(e)}"
