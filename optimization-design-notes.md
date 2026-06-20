# Query Optimization Design Notes

> **These are internal design notes for developers, not LLM agent instructions.**
> For LLM-facing guidance, see `query-optimization.md` and `query-setup.md`.

---

## What We Learned (and Got Wrong): March 2026

This document records findings from a systematic investigation into DuckDB query
performance on hive-partitioned parquet data stored on NRP S3 (Ceph/Rook), including
a significant error we made and how the correct understanding was recovered.

> **WARNING FOR FUTURE LLM SESSIONS**: This document previously contained a section
> claiming "Including h0 in a join does NOT enable partition pruning on S3 — this is
> factually incorrect for S3." That claim was itself wrong, and was based on a
> benchmark that violated the very advice it was supposedly testing (it joined on h8
> only, not h8+h0). **Do not use this document to override the advice in
> `query-optimization.md`.** If you see a conflict between this file and
> `query-optimization.md`, the simpler rule in `query-optimization.md` is likely
> correct. Require a *correctly structured* benchmark before accepting any claim that
> contradicts it.

---

## 1. How DPP Actually Works

### The mechanism

DuckDB's Dynamic Partition Pruning (DPP) materializes the build side of a hash join,
derives the set of values for the join key(s), then filters the probe side.

**Critically: DPP can only propagate constraints on columns that appear in the equijoin condition.**

If your join is `ON p.h8 = c.h8` only, DuckDB derives a filter on `c.h8`. It has no
`c.h0` constraint to propagate — and therefore cannot prune `c`'s hive partitions on h0.
If your join is `ON p.h8 = c.h8 AND p.h0 = c.h0`, DuckDB derives both a filter on `c.h8`
and a filter on `c.h0`. The h0 filter matches against the hive partition path encoding
(`hex/h0=577199624117288959/data_0.parquet`) and prunes the file list.

### Two levels of pruning

| Level | Mechanism | Requirement |
|---|---|---|
| **File-level** | Partition key matched against path strings; non-matching files never opened | Partition key must appear in join condition |
| **Row-group level** | Parquet footer stats checked per row group | Any column with stats; fires after file is opened |

File-level pruning is what matters for S3: it avoids HTTP requests entirely for
non-matching partitions. Row-group pruning still requires opening each file for a
footer read (one HTTP round trip per file on S3).

### S3 vs local — the real distinction

The relevant difference is not "DPP works locally but not on S3." It is:

- **Footer reads are free locally** (disk seek, microseconds).
- **Footer reads are expensive on S3** (HTTP round trip, ~100ms each).

If your query has h0 in the join condition, DPP prunes the file list before any reads,
and performance is good on both local and S3.

If your query lacks h0 in the join condition, DPP can only use row-group stats — which
requires opening every file for a footer read. Locally this is nearly free (94 × ~10ms =
~1s). On S3 it is very expensive (94 × ~100ms × N requests/file = many seconds or minutes).

### Files opened, not bytes read, is the cost (dev, DuckDB 1.5.3, 2026-06)

The dominant S3 cost is the number of partition files opened, not the bytes scanned.
GBIF species richness "in Peru", attribute filter vs spatial clip:

```
# countrycode='PE'  (no partition prune)         923/923 files, 641 MiB, 8.70s
# SEMI JOIN countries-hex USING (h8, h0)          100/923 files, 631 MiB, 4.12s
```

~2.1× faster on essentially identical bytes — the win is opening 100 files instead of
923. (Consistent with the #190 I/O characterization: the per-file footer + range-request
latency dominates; a sparse attribute filter that touches all partitions collapses
throughput.) The lever is always *prune the file list*, not *read fewer bytes*.

### Benchmark (corrected, DuckDB 1.5.0, public S3 endpoint)

```
# Query A: JOIN ON p.h8 = c.h8 AND p.h0 = c.h0  (correct — h0 in join)
Total Files Read: 1,  #GET: 737,  in: 109 MiB,  38s

# Query B: JOIN ON p.h8 = c.h8  (incorrect — no h0 in join)
Total Files Read: 94, #GET: 6717, in: 1.3 GiB,  407s

# Query C: JOIN ON p.h8 = c.h8 + WHERE c.h0 = H0  (static literal workaround)
Total Files Read: 1,  #GET: 546,  in: 109 MiB,  29s
```

Query A (join DPP) and Query C (static literal) are semantically equivalent and both
perform well. Query C is ~9s faster because the static filter prunes at planning time,
before the build side is materialized. Query A requires materializing parks first, then
applying the DPP filter — slightly more round trips. For most queries the difference
is minor.

---

## 2. The Error We Made

### What we reported (incorrectly)

We claimed that "DPP does not work at file level on S3" based on a benchmark that showed:

```
Local disk: 1 / 94 files, 1.13s
NRP S3:    94 / 94 files, 4.53s
```

We concluded this was a DuckDB limitation and filed an upstream feature request.

### What actually happened

Both benchmarks joined on `h8` only — there was no `h0` in the join condition.
With no h0 equijoin, DuckDB has nothing to propagate for file-level pruning.

- **Locally**: DPP applied row-group level pruning. Footer reads are cheap (~10ms each).
  EXPLAIN reported "1 file" — meaning 1 file had matching rows after row-group filtering.
  The other 93 files were opened for footers but nothing was read from them fully.
- **On S3**: DPP applied the same row-group level pruning. But footer reads cost ~100ms
  each via HTTP. EXPLAIN reported "94 files" — every file had HTTP requests made to it
  (for footer reads), even though only 1 contained matching data after filtering.

The "1 file / 94 files" discrepancy was a measurement artifact: locally, EXPLAIN
reported files with actual data reads; on S3, it reported files with any HTTP contact
(including footer-only reads that found no matching rows). The underlying DPP mechanism
was identical. The cost difference was purely about footer read latency.

### Why the mistake persisted

1. The workaround (static `WHERE c.h0 = H0`) did fix performance — so we had no
   pressure to re-examine the root cause.
2. The working benchmark (Test 6 in benchmark-job.yaml) already joined on
   `p.h8 = c.h8 AND p.h0 = c.h0` and performed well — but we attributed its success
   to the "regions driver pattern" rather than to having h0 in the join condition.
3. The wrong mental model ("S3 DPP is broken") was written up as design notes and
   reinforced across multiple sessions.
4. No one asked the simple question: "what if we just add h0 to the join?"

### How it was caught

A DuckDB maintainer reviewed our upstream issue report and immediately noted that
Queries A and B were not semantically equivalent (different join conditions). Adding
`AND p.h0 = c.h0` to the join fixed the behavior in one test.

---

## 3. The Cardinality Estimation Bug (still valid)

DuckDB 1.5.0 uses only the **first parquet file** to estimate total dataset cardinality
for glob scans. The first file (sorted by S3 LIST response) may be a tiny partition
with very few rows, leading to 100x cardinality underestimates and wrong join order.

**Observed effect:** PADUS joined with carbon, optimizer estimated PADUS at ~447M rows,
chose carbon as build side → 2.1B-row hash table → OOM at 16 GiB and 32 GiB.

**Fix:** PR #21374 (merged 2026-03-14, not yet released) uses file sizes from the S3
LIST response to estimate per-file row counts proportionally — much better estimates
with no extra HTTP requests.

**Until PR #21374 ships:** use the two-step or regions-driver pattern (Section 4) which
establishes h0 scope before the large join, reducing effective cardinality.

---

## 4. The Driving CTE Pattern

Even with correct DPP (h0 in join), the **driving CTE still matters** for two reasons:

1. **Cardinality estimation bug (Section 3)**: a large driving dataset with a
   non-geographic filter (e.g., PADUS filtered by `Des_Tp = 'NP'`) requires scanning
   all partitions to apply the filter. The result may be spatially concentrated (1 h0
   cell) but getting there reads 7 GiB. The optimizer may also mis-estimate cardinality
   and choose the wrong join order.

2. **Small drivers are just faster**: `overture-divisions` regions (437 MiB, 94 files)
   or countries (single file) scanned with a geographic filter returns h0/h8 cells
   covering a known geographic scope. Subsequent joins on h0 then prune correctly.

### The preferred pattern

```sql
-- Small geographic reference dataset drives the query
WITH scope AS (
  SELECT DISTINCT h8, h0
  FROM read_parquet('s3://public-overturemaps/regions/hex/**')
  WHERE region = 'US-CA'
),
-- h0 constraint propagates via DPP to both subsequent joins
parks AS (
  SELECT DISTINCT p.h8, p.h0
  FROM scope s
  JOIN read_parquet('s3://public-padus/padus-4-1/fee/hex/**') p
    ON s.h8 = p.h8 AND s.h0 = p.h0
  WHERE p.Des_Tp = 'NP'
)
SELECT SUM(c.carbon)/1e6
FROM parks p
JOIN read_parquet('s3://public-carbon/vulnerable-carbon-2024/hex/**') c
  ON p.h8 = c.h8 AND p.h0 = c.h0
```

Every join includes `AND .h0 = .h0`. DPP propagates the h0 constraint from scope
through parks and into carbon, pruning each large dataset to the relevant partitions.

### Two-step variant (slightly faster, more complex)

Pre-compute h0 values as Python-side literals, embed in static WHERE clauses:

```python
# Step 1: get h0 values
h0_list = conn.sql("""
    SELECT DISTINCT h0 FROM read_parquet('s3://public-overturemaps/regions/hex/**')
    WHERE region = 'US-CA'
""").fetchall()
h0_in = ", ".join(str(r[0]) for r in h0_list)

# Step 2: use as static literals — prunes at planning time, before build side materializes
conn.sql(f"""
    WITH parks AS (
        SELECT DISTINCT h8, h0
        FROM read_parquet('s3://public-padus/padus-4-1/fee/hex/**')
        WHERE Des_Tp = 'NP' AND h0 IN ({h0_in})
    )
    SELECT SUM(c.carbon)/1e6
    FROM parks p
    JOIN read_parquet('s3://public-carbon/vulnerable-carbon-2024/hex/**') c
      ON p.h8 = c.h8 AND p.h0 = c.h0
    WHERE c.h0 IN ({h0_in})
""")
```

Static literals prune at planning time and save one round of build-side materialization.
For simple queries the improvement is ~9s (~25%). Use this when you already know the h0
scope from a prior step.

### An inline `IN (subquery)` already prunes files — the Python two-step is usually unnecessary

`h0 IN (SELECT h0 FROM <small_table> WHERE …)` is internally a SEMI JOIN on h0, so it
gets the **same file-level DPP** as a join condition — DuckDB builds a runtime dynamic
filter from the subquery and prunes the partition list. No need to round-trip the h0
values to Python and re-embed them as literals; a single SQL statement prunes.

Verified on the IUCN per-species sidecar (dev, DuckDB 1.5.3, 2026-06):

```sql
SELECT h8 FROM read_parquet('…/iucn-ranges-2025/hex/h0=*/data_0.parquet', hive_partitioning=true)
WHERE h0 IN (SELECT h0 FROM read_parquet('…/iucn-species-h0.parquet') WHERE sci_name='Rana draytonii')
  AND sci_name='Rana draytonii';
-- EXPLAIN ANALYZE: Dynamic Filters: h0 IN BF(#0); Total Files Read: 2/122; 2.19s
```

The Python two-step (above) still buys the planning-time-vs-runtime difference (~9s on
the carbon query, same as static-literal Query C vs join Query A in §1), so reach for it
only when that margin matters or you already have the h0 list in hand. For the common
case, the inline subquery is simpler and prunes correctly.

---

## 5. Benchmark Summary (corrected)

All benchmarks on DuckDB 1.5.0. Query: sum carbon in CA National Parks.

| Test | h0 in join? | Files read | Time |
|---|---|---|---|
| Join on h8+h0 (DPP, S3) | Yes | 1 / 94 | 38s |
| Static WHERE literal (S3) | n/a | 1 / 94 | 29s |
| Join on h8 only (S3) | **No** | **94 / 94** | **407s** |
| Wrong join order (OOM) | — | — | OOM |

---

## 6. How to Verify DPP Is Working

Always check EXPLAIN ANALYZE for the probe-side scan:

```sql
EXPLAIN ANALYZE <your query>
```

Look for:
- `Total Files Read: 1` (or the expected small number) — file-level pruning is working
- `Dynamic Filters: ...` — DPP is active
- Low `#GET` count relative to total partition count

If you see `Total Files Read: 94` (the full partition count), either:
- h0 is missing from the join / `IN`-subquery condition, or
- An operator between the h0 source and the large scan blocks propagation —
  aggregation (`GROUP BY`/`MODE`/`SUM`) or a correlated subquery. (A plain
  uncorrelated `h0 IN (SELECT …)` does *not* block it — see §4.)

---

## 7. S3 Configuration Notes

Internal endpoint (k8s only): `rook-ceph-rgw-nautiluss3.rook`, `USE_SSL false`, `THREADS=100`

Public endpoint: `s3-west.nrp-nautilus.io`, `USE_SSL true`, `THREADS=2`

Note: DuckDB 1.5.0 had a regression where hierarchical glob expansion added
latency (issue #21347). Fixed in DuckDB 1.5.1 — workaround no longer needed.
