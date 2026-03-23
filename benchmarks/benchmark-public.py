"""
DuckDB S3 DPP benchmark against public NRP S3 endpoint.

Tests two issues:
  Issue A: s3_allow_recursive_globbing regression (DuckDB 1.5.0 #21347)
  Issue B: DPP file-level pruning on S3 (path encoding not used)

Public endpoint: s3-west.nrp-nautilus.io (requires SSL, limited threads)
"""
import duckdb, time

ENDPOINT = "s3-west.nrp-nautilus.io"
SETUP = f"""
SET THREADS=2;
SET preserve_insertion_order=false;
SET enable_object_cache=false;
INSTALL httpfs; LOAD httpfs;
CREATE OR REPLACE SECRET s3 (
    TYPE S3,
    ENDPOINT '{ENDPOINT}',
    URL_STYLE 'path',
    USE_SSL 'true',
    KEY_ID '',
    SECRET ''
);
"""

CARBON = "s3://public-carbon/vulnerable-carbon-2024/hex/**"
PADUS  = "s3://public-padus/padus-4-1/fee/hex/**"
# A single h0 cell covering part of California
H0 = 577199624117288959


def fresh(extra_sql=None):
    c = duckdb.connect(":memory:")
    c.sql(SETUP)
    if extra_sql:
        c.sql(extra_sql)
    return c


def run_explain(label, conn, query):
    t0 = time.time()
    rows = conn.sql(f"EXPLAIN ANALYZE {query}").fetchall()
    elapsed = time.time() - t0
    text = "\n".join(r[1] for r in rows)
    print(f"\n{'='*60}", flush=True)
    print(f"[{label}] {elapsed:.2f}s", flush=True)
    for line in text.split("\n"):
        if any(x in line for x in ["Files Read", "Scanned", "Total Files", "HTTP", "GET", "Bytes", " in:", "Dynamic", "Total Time", "Filters"]):
            print(f"  {line.strip()}", flush=True)
    return elapsed, text


def run(label, conn, query):
    t0 = time.time()
    result = conn.sql(query).fetchall()
    elapsed = time.time() - t0
    print(f"[{label}] {elapsed:.2f}s => {result}", flush=True)
    return elapsed, result


print(f"DuckDB {duckdb.__version__}  endpoint={ENDPOINT}", flush=True)

# ============================================================
# ISSUE A: s3_allow_recursive_globbing regression (DuckDB 1.5.0)
#
# Static h0 filter on carbon. With the 1.5.0 default
# (s3_allow_recursive_globbing=true), the hierarchical glob
# expansion lists ALL sub-prefixes recursively before applying
# hive partition filters, reading all 94 files. With the
# workaround (=false), DuckDB uses a flat glob and the hive
# filter prunes to 1 file at planning time.
# ============================================================
STATIC_QUERY = f"""
    SELECT SUM(carbon)/1e6 AS megatons
    FROM read_parquet('{CARBON}')
    WHERE h0 = {H0}
"""

print("\n--- ISSUE A: s3_allow_recursive_globbing regression ---", flush=True)
run_explain("A1_default_glob_regression", fresh(),                             STATIC_QUERY)
run_explain("A2_fixed_glob_workaround",   fresh("SET s3_allow_recursive_globbing=false;"), STATIC_QUERY)

# ============================================================
# ISSUE B: DPP file-level pruning on S3
#
# Join where build side (parks) filters to h0=H0, probe side
# (carbon) spans 94 h0 partitions. With file-level DPP, only
# 1 carbon file should open. On S3, all 94 open for footers.
# Compare:
#   B1: join-driven DPP (all 94 carbon files opened on S3)
#   B2: static h0 literal (1 carbon file opened — workaround)
#
# Note: B1 uses the workaround for Issue A so we isolate Issue B.
# ============================================================
print("\n--- ISSUE B: DPP file-level pruning on S3 ---", flush=True)

DPP_QUERY = f"""
    WITH parks AS (
        SELECT DISTINCT h8, h0
        FROM read_parquet('{PADUS}')
        WHERE State_Nm = 'CA' AND Des_Tp = 'NP'
          AND h0 = {H0}
    )
    SELECT SUM(c.carbon)/1e6
    FROM parks p
    JOIN read_parquet('{CARBON}') c ON p.h8 = c.h8 AND p.h0 = c.h0
"""

STATIC_JOIN_QUERY = f"""
    WITH parks AS (
        SELECT DISTINCT h8, h0
        FROM read_parquet('{PADUS}')
        WHERE State_Nm = 'CA' AND Des_Tp = 'NP'
          AND h0 = {H0}
    )
    SELECT SUM(c.carbon)/1e6
    FROM parks p
    JOIN read_parquet('{CARBON}') c ON p.h8 = c.h8
    WHERE c.h0 = {H0}
"""

workaround = "SET s3_allow_recursive_globbing=false;"
run_explain("B1_join_dpp_no_static_filter", fresh(workaround), DPP_QUERY)
run_explain("B2_static_h0_workaround",      fresh(workaround), STATIC_JOIN_QUERY)
