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
import os
import sys

import polars as pl

from engines import sql_translate
from engines.base import QueryEngine, S3Request, render_rows, RESULT_LIMIT

VALID_MODES = ("polars-cpu", "polars-gpu", "polars-gpu-cudf")


def _probe_gpu():
    """Return a GPUEngine if cudf-polars is importable, else None.

    Probed once at construction. Import failure (no cudf-polars / no GPU build)
    is not an error — the engine just collects on CPU, so a GPU image that loses
    its GPU deps degrades to correct-but-slow rather than breaking.
    """
    try:
        import cudf_polars  # noqa: F401 — importing activates the GPU backend
        from polars import GPUEngine
        return GPUEngine()
    except Exception as e:  # ImportError, or a CUDA/driver init failure
        print(f"⚙️  GPU engine unavailable ({e}); Polars will collect on CPU",
              file=sys.stderr)
        return None


class PolarsEngine(QueryEngine):
    def __init__(self, mode: str):
        if mode not in VALID_MODES:
            raise ValueError(f"Unknown Polars engine mode: {mode!r}")
        self.name = mode
        self.want_gpu = mode in ("polars-gpu", "polars-gpu-cudf")
        self.want_cudf_io = mode == "polars-gpu-cudf"
        # ALLOW_CPU_FALLBACK (default true): a GPU-runtime failure (VRAM OOM, an
        # unsupported plan node) falls back to a CPU collect of the same plan —
        # correctness-preserving, just slower. Set false when benchmarking to
        # surface GPU errors instead of masking them. (A *dialect* miss is a
        # different failure, rejected earlier by sql_translate.guard_unsupported.)
        self.allow_cpu_fallback = (
            os.environ.get("ALLOW_CPU_FALLBACK", "true").strip().lower() != "false"
        )
        self._gpu = _probe_gpu() if self.want_gpu else None

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
        """Collect a LazyFrame — on the GPU when available, else CPU.

        With a GPU present and ALLOW_CPU_FALLBACK on, a failed GPU collect is
        retried on CPU. With fallback off, the GPU error propagates.
        """
        if self._gpu is None:
            return lf.collect()
        if not self.allow_cpu_fallback:
            return lf.collect(engine=self._gpu)
        try:
            return lf.collect(engine=self._gpu)
        except Exception as e:
            print(f"⚠️ GPU collect failed, falling back to CPU: {e}", file=sys.stderr)
            return lf.collect()
