# Agent Architecture

## Deployment

The MCP server runs on the NRP Nautilus Kubernetes cluster at **`https://duckdb-mcp.nrp-nautilus.io`**.

- **2 replicas** of the `duckdb-mcp` pod, each cloning this repo at startup and running `server.py` via `uv`
- **Resources:** 16 Gi RAM requested, up to 160 Gi / 16 CPU per pod
- **STAC catalog:** `https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json` (set via `STAC_CATALOG_URL` env var)
- **Ingress:** HAProxy with CORS enabled, 10-minute query timeout, 1-hour SSE tunnel timeout

To redeploy after pushing changes to `main`, restart the pods so they re-clone:
```
kubectl rollout restart deployment/duckdb-mcp
```

---

This project uses two distinct, asynchronous AI agent processes. Do not confuse them.

---

## Process 1: Real-time MCP Tool (Small LLM)

A lightweight open-source LLM serves user requests in real time via the MCP tool.
It has no memory between requests. Its only context is what is injected at call time.

**Files it reads (injected as prompts):**
- `query-setup.md` — required DuckDB setup SQL, must run before every query
- `query-optimization.md` — short, actionable query-writing rules
- `datasets.md` — STAC catalog summary, dataset paths, column schemas
- `assistant-role.md` — role and response style instructions

**Rules for editing these files:**
- Write for a small, stateless model with limited context
- Instructions must be short, concrete, and unambiguous
- No debugging advice, no explanation of DuckDB internals, no "check X before Y"
- No meta-commentary about why rules exist — just the rules
- Every example must be a correct, copy-pasteable query

---

## Process 2: Asynchronous Evaluation (Claude)

A separate, asynchronous process uses Claude to review logs from real user sessions,
identify slow or incorrect queries, diagnose root causes, and update the small LLM's
instructions.

**Files it reads:**
- Real-time query logs and timing data from production
- `optimization-design-notes.md` — developer notes on DuckDB behavior and benchmarks
- `AGENTS.md` — this file

**Files it writes:**
- `query-optimization.md`, `query-setup.md` — updated instructions for the small LLM
- `optimization-design-notes.md` — updated technical findings

**Rules for this process:**
- When a query is slow, check whether it follows all rules in `query-optimization.md`
  *before* diagnosing infrastructure, DuckDB behavior, or S3 limitations
- Benchmark queries used to test a rule must themselves follow that rule
  (e.g., a benchmark testing "h0 in join" must actually have h0 in the join)
- Do not edit `query-optimization.md` with debugging checklists, developer notes,
  or explanations of DuckDB internals — that content belongs in `optimization-design-notes.md`
- A claim that a rule in `query-optimization.md` is wrong requires a correctly structured
  benchmark that violates only that rule and nothing else

---

## Failure mode to avoid

In March 2026, a small LLM generated a query omitting h0 from a join (violating the
rule in `query-optimization.md`). The resulting slow query was diagnosed as "S3 DPP
is broken" rather than "the query violated the h0-in-join rule." Hours of investigation
followed, producing `optimization-design-notes.md` content that incorrectly characterized
the rule as wrong. A subsequent Claude session read those notes and tried to remove the
correct rule from `query-optimization.md`.

The fix is this document: keep the two processes and their files clearly separated.
