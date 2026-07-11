"""DuckDB session configuration derived from deployment env.

Companion to s3config.py (which owns the S3 secrets): this owns the non-S3
`SET` statements whose value depends on the deployment/pod rather than the
query. Env is read at call time, not import time, so per-connection setup picks
up changes without a restart and tests can monkeypatch — the same contract as
s3config.default_s3_secret_sql().
"""
import os
import re
import sys

from s3config import sql_quote

# Fraction of the pod's memory *limit* DuckDB may use before it spills to
# temp_directory. The headroom absorbs allocations DuckDB doesn't count against
# memory_limit (the Python process, httpfs buffers, uvicorn) so the cgroup never
# OOM-kills the pod — an OOM takes out every co-tenant query on the replica, not
# just the offender, whereas spilling only makes the one oversized query slower
# (#270).
_MEMORY_LIMIT_FRACTION = 0.8

# SI (10^3) and binary (2^10) byte units, plus the k8s "Ki/Mi/Gi/Ti" quantities.
_BYTE_UNITS = {
    "": 1, "b": 1,
    "k": 10**3, "kb": 10**3, "ki": 2**10, "kib": 2**10,
    "m": 10**6, "mb": 10**6, "mi": 2**20, "mib": 2**20,
    "g": 10**9, "gb": 10**9, "gi": 2**30, "gib": 2**30,
    "t": 10**12, "tb": 10**12, "ti": 2**40, "tib": 2**40,
}


def _parse_bytes(text: str) -> int:
    """Parse a byte count: a plain integer (the Downward API emits bytes) or a
    k8s/SI quantity like '96Gi', '2Gi', '500Mi', '10G'. Raises ValueError otherwise.
    """
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", text or "")
    if not m:
        raise ValueError(f"unparseable byte quantity: {text!r}")
    num, unit = m.group(1), m.group(2).lower()
    if unit not in _BYTE_UNITS:
        raise ValueError(f"unknown byte unit {unit!r} in {text!r}")
    return int(float(num) * _BYTE_UNITS[unit])


def duckdb_memory_limit() -> str | None:
    """The DuckDB `memory_limit` value for this pod, or None to leave DuckDB's
    own default in place.

    Resolution order:
    - DUCKDB_MEMORY_LIMIT (explicit override) wins, passed through verbatim so an
      operator can set '120GB', '160GiB', '-1' (unlimited), etc. (DuckDB units:
      KB/MB/GB/TB or KiB/MiB/GiB/TiB, or -1; no '%' form.)
    - else POD_MEMORY_LIMIT (wire the pod's cgroup limit in via the Downward API —
      resourceFieldRef limits.memory): return ~80% of it.
    - else None. In a plain local run DuckDB sizes itself from detected RAM, which
      is only unsafe under a container cgroup — where detection can read the node's
      RAM, not the pod's, and DuckDB allocates past the limit and gets OOM-killed
      before it ever spills (#270).
    """
    explicit = os.environ.get("DUCKDB_MEMORY_LIMIT", "").strip()
    if explicit:
        return explicit
    raw = os.environ.get("POD_MEMORY_LIMIT", "").strip()
    if not raw:
        return None
    try:
        total = _parse_bytes(raw)
    except ValueError as e:
        print(f"⚠️ POD_MEMORY_LIMIT ignored ({e}); using DuckDB's default memory_limit",
              file=sys.stderr)
        return None
    budget_mib = int(total * _MEMORY_LIMIT_FRACTION) // (1024 * 1024)
    if budget_mib <= 0:
        return None
    return f"{budget_mib}MiB"


def memory_limit_sql() -> str:
    """`SET memory_limit=...` for this pod, or '' when unconfigured (caller skips).

    Run on every DuckDB connection (query and tiles) so the spill-not-OOM
    protection is identical on both paths.
    """
    value = duckdb_memory_limit()
    return f"SET memory_limit='{sql_quote(value)}'" if value else ""
