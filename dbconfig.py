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
import uuid

from s3config import sql_quote

# Fraction of the pod's memory *limit* DuckDB may use before it spills to
# temp_directory. The headroom absorbs allocations DuckDB doesn't count against
# memory_limit (the Python process, httpfs buffers, uvicorn) so the cgroup never
# OOM-kills the pod — an OOM takes out every co-tenant query on the replica, not
# just the offender, whereas spilling only makes the one oversized query slower
# (#270).
_MEMORY_LIMIT_FRACTION = 0.8

# Fraction of the container's ephemeral-storage *limit* one DuckDB instance may
# spill before its query fails with DuckDB's own "max_temp_directory_size"
# error. Unset, DuckDB defaults to 90% of the *node's* free disk — it cannot see
# the container limit (50Gi on NRP, a namespace LimitRange default), so a big
# spill ran past it and the kubelet evicted the pod: #270's spill-not-OOM traded
# an OOM kill for an eviction with the same blast radius (#423). The cap is
# per-instance, not pod-wide (every query and tile build is its own DuckDB), and
# the same limit also counts logs and the container's writable layer — hence
# half, not 80%: one runaway spill is contained with room to spare.
_TEMP_DIRECTORY_FRACTION = 0.5

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


def _fraction_of_pod_limit(var: str, fraction: float, setting: str) -> str | None:
    """`fraction` of the k8s quantity in env `var`, as a DuckDB MiB string, or
    None when unset, unparseable, or zero (caller leaves DuckDB's default)."""
    raw = os.environ.get(var, "").strip()
    if not raw:
        return None
    try:
        total = _parse_bytes(raw)
    except ValueError as e:
        print(f"⚠️ {var} ignored ({e}); using DuckDB's default {setting}",
              file=sys.stderr)
        return None
    budget_mib = int(total * fraction) // (1024 * 1024)
    if budget_mib <= 0:
        return None
    return f"{budget_mib}MiB"


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
    return _fraction_of_pod_limit("POD_MEMORY_LIMIT", _MEMORY_LIMIT_FRACTION, "memory_limit")


def duckdb_max_temp_directory_size() -> str | None:
    """The DuckDB `max_temp_directory_size` for this pod, or None to leave
    DuckDB's default (90% of the disk under temp_directory).

    Same resolution as duckdb_memory_limit(): DUCKDB_MAX_TEMP_DIRECTORY_SIZE
    verbatim, else half of POD_EPHEMERAL_LIMIT (Downward API — resourceFieldRef
    limits.ephemeral-storage), else None (#423).
    """
    explicit = os.environ.get("DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "").strip()
    if explicit:
        return explicit
    return _fraction_of_pod_limit("POD_EPHEMERAL_LIMIT", _TEMP_DIRECTORY_FRACTION,
                                  "max_temp_directory_size")


def memory_limit_sql() -> str:
    """`SET memory_limit=...` for this pod, or '' when unconfigured (caller skips).

    Run on every DuckDB connection (query and tiles) so the spill-not-OOM
    protection is identical on both paths.
    """
    value = duckdb_memory_limit()
    return f"SET memory_limit='{sql_quote(value)}'" if value else ""


def spill_sql() -> list[str]:
    """`SET` statements giving this connection its own bounded spill directory.

    Run on every DuckDB connection (query and tiles), after any SETUP_SQL
    `temp_directory`, and before the connection has spilled — DuckDB refuses to
    switch temp_directory once it has been used.

    - A private temp_directory per connection. Every instance names its spill
      files identically (`duckdb_temp_storage_DEFAULT-0.tmp`, ...), so two
      concurrently spilling instances in one process sharing '/tmp' overwrite
      each other's blocks and segfault the server — every co-tenant query on the
      pod dies with it (#423). DuckDB creates the directory on first spill and
      removes it on close. Root is DUCKDB_TEMP_ROOT (default '/tmp').
    - max_temp_directory_size, when the pod limit is known (see
      duckdb_max_temp_directory_size).
    """
    root = os.environ.get("DUCKDB_TEMP_ROOT", "").strip() or "/tmp"
    stmts = [f"SET temp_directory='{sql_quote(os.path.join(root, f'duckdb-{uuid.uuid4().hex}'))}'"]
    cap = duckdb_max_temp_directory_size()
    if cap:
        stmts.append(f"SET max_temp_directory_size='{sql_quote(cap)}'")
    return stmts
