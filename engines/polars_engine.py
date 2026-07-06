"""Polars query engine — CPU now, GPU (cudf-polars + kvikio) in PR 2.

Modes (via QUERY_ENGINE):
  polars-cpu       Polars lazy reader + CPU collect. No GPU deps; runs in CI and
                   is the fallback target when a GPU collect fails.
  polars-gpu       Polars lazy reader + cudf-polars GPU collect.        (PR 2)
  polars-gpu-cudf  kvikio GPU-direct S3 reads (large files) + GPU collect. (PR 2)

The GPU flags are wired here so the seam is stable, but PR 1 only implements the
CPU collect. GPU availability probing, GPUEngine collect, the kvikio reader, and
the GPU→CPU fallback land in PR 2. See docs/architecture/gpu-query-engine.md.
"""
import sys

import polars as pl

from engines import sql_translate
from engines.base import QueryEngine, S3Request, render_rows, RESULT_LIMIT

VALID_MODES = ("polars-cpu", "polars-gpu", "polars-gpu-cudf")


class PolarsEngine(QueryEngine):
    def __init__(self, mode: str):
        if mode not in VALID_MODES:
            raise ValueError(f"Unknown Polars engine mode: {mode!r}")
        self.name = mode
        # GPU compute / cuDF I/O requested by the mode. Honoured in PR 2; in
        # PR 1 every mode collects on CPU (a GPU deploy that runs PR 1 code
        # simply runs correct-but-CPU, never wrong).
        self.want_gpu = mode in ("polars-gpu", "polars-gpu-cudf")
        self.want_cudf_io = mode == "polars-gpu-cudf"

    def run(self, sql_query: str, s3: S3Request) -> str:
        print(f"🔍 [{self.name}] Executing: {sql_query}", file=sys.stderr)
        try:
            rewritten, ctx = sql_translate.build_context(
                sql_query, s3, use_cudf_io=self.want_cudf_io
            )
            # SQLContext defaults to lazy: execute() returns a LazyFrame.
            lf = ctx.execute(rewritten).limit(RESULT_LIMIT + 1)
            df = self._collect(lf)

            # Drop binary/geometry (WKB) columns — not useful in tabular output
            # and awkward to render, mirroring the DuckDB engine's geometry drop.
            bincols = [c for c, dt in zip(df.columns, df.dtypes) if dt == pl.Binary]
            if bincols:
                df = df.drop(bincols)

            return render_rows(df.columns, df.rows())
        except sql_translate.UnsupportedSQL as e:
            # Dialect/capability miss: fail loudly with an actionable message —
            # falling back to DuckDB would mean shipping it in the GPU image.
            return f"SQL Error: {e}"
        except Exception as e:
            return f"SQL Error: {str(e)}"

    def _collect(self, lf: "pl.LazyFrame") -> "pl.DataFrame":
        """Collect a LazyFrame. PR 1: CPU only. PR 2 adds GPUEngine + fallback."""
        return lf.collect()
