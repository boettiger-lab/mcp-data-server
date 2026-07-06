"""Engine-agnostic query seam.

`QUERY_ENGINE` selects a backend behind this interface. The default `duckdb`
engine is byte-for-byte the historical behaviour; the Polars engines add an
optional GPU-capable path. See docs/architecture/gpu-query-engine.md.

The engine owns *execution* (dialect + S3 resolution + collect). Everything
around it — the async offload, the CapacityLimiter, tool registration, and the
docstring — stays in server.py and is shared across engines. Result rendering is
shared here so every engine produces the same 50-row markdown preview.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from tabulate import tabulate

# Preview size: show up to RESULT_LIMIT rows; fetch one extra to detect
# truncation without a second COUNT scan.
RESULT_LIMIT = 50

TRUNCATION_NOTICE = (
    "\n\n⚠️ Showing the first 50 rows only — this is a preview, not the"
    " full result and NOT a count. The true number of matching rows is"
    " larger; use COUNT(...) / COUNT(DISTINCT ...) / SUM(...) for totals."
)


@dataclass(frozen=True)
class S3Request:
    """Per-request S3 intent, engine-agnostic.

    Mirrors the `query` tool's S3 parameters. Each engine realises these against
    its own S3 mechanism — DuckDB SECRETs for the DuckDB engine, per-path Polars
    `storage_options` for the Polars engines — plus the deployment default and
    the source registry from s3config.
    """
    s3_key: str | None = None
    s3_secret: str | None = None
    s3_endpoint: str | None = None
    s3_scope: str | None = None


class QueryEngine(ABC):
    """A SQL execution backend for the `query` tool."""

    #: short identifier, e.g. "duckdb" / "polars-cpu" (set by subclasses)
    name: str = "base"

    @abstractmethod
    def run(self, sql_query: str, s3: S3Request) -> str:
        """Execute SQL and return a markdown preview (or an "SQL Error: …" string).

        Must not raise for query-level failures — return the error as a string,
        matching the historical `query()` contract that the MCP tool relies on.
        """
        raise NotImplementedError


def render_preview(df) -> str:
    """Render a pandas DataFrame (already limited to RESULT_LIMIT+1 rows) as a
    markdown preview, appending the truncation notice when more rows existed.

    The historical DuckDB path — pandas `to_markdown` (tablefmt="pipe"). Kept
    byte-for-byte so the DuckDB engine's output does not change.
    """
    if df.empty:
        return "No results found."
    truncated = len(df) > RESULT_LIMIT
    md = df.head(RESULT_LIMIT).to_markdown(index=False)
    if truncated:
        md += TRUNCATION_NOTICE
    return md


def render_rows(columns, rows) -> str:
    """Render columns + row tuples (already limited to RESULT_LIMIT+1) as a
    markdown preview. Pandas-free (uses tabulate directly), so the Polars engines
    don't drag pandas/pyarrow into the CPU path. Same "pipe" table format and
    truncation notice as render_preview.
    """
    if not rows:
        return "No results found."
    truncated = len(rows) > RESULT_LIMIT
    md = tabulate(rows[:RESULT_LIMIT], headers=list(columns), tablefmt="pipe")
    if truncated:
        md += TRUNCATION_NOTICE
    return md
