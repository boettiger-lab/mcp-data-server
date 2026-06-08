# Agent Architecture

> **The `.md` files in this repo are runtime prompt artifacts, not documentation.**
> `server.py` loads them at startup and injects their content into MCP tool descriptions
> and prompts. Editing them changes what the agent is instructed to do.
> `README.md` is the only human-facing documentation.

## Contributing

This repo uses **GitHub Flow**: all changes go through a branch + PR, never committed directly to `main`. `main` has branch protection enforced — direct pushes are rejected.

1. Create a branch for your change (`git switch -c <branch>`) **before** the first commit. Never commit on `main` and open a PR afterward — the squash-merge will leave local `main` permanently diverged from `origin/main`.
2. Open a PR against `main`
3. Merge via the GitHub UI (squash merge preferred)

### After a PR merges

This repo squash-merges, so the merged commit on `origin/main` has a different SHA than the local feature commits. Clean up with reset, not pull:

```
git switch main
git fetch origin
git reset --hard origin/main
git branch -D <feature-branch>
```

Do **not** `git pull` on `main` when local `main` has commits matching the just-merged PR — `pull` will create a merge commit because the squash changed the SHA. If you must `pull`, use `git pull --ff-only` so divergence fails loudly instead of silently merging.

## Deployment

The MCP server runs on the NRP Nautilus Kubernetes cluster.

- **Prod:** `https://duckdb-mcp.nrp-nautilus.io` — 2 replicas, `k8s/deployment.yaml`
- **Dev:** `https://dev-duckdb-mcp.nrp-nautilus.io` — 2 replicas, `k8s/dev-deployment.yaml` (must stay ≥2 so cross-pod bugs surface here before prod)
- **Resources:** 16 Gi RAM requested, up to 160 Gi / 16 CPU per pod
- **STAC catalog:** `https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json` (set via `STAC_CATALOG_URL` env var)
- **Ingress:** HAProxy with CORS enabled, 10-minute query timeout, 1-hour SSE tunnel timeout

### MCP transport: stateless HTTP

The server runs FastMCP in **stateless streamable-HTTP mode** (`server.py`: `FastMCP(..., stateless_http=True)`). Every `POST /mcp` is a complete, independent request/response. There is no `Mcp-Session-Id` pinning clients to a replica, no per-pod session cache, no in-memory state that survives across requests. The protocol's stateful SSE mode is **not** in use here.

This is intentional and load-bearing. Several things depend on it:

- The Service has `sessionAffinity: None` and the ingress uses `balance-algorithm: leastconn`. Both rely on the stateless premise — replicas are interchangeable on a per-request basis.
- Each query runs in a fresh `duckdb.connect(":memory:")` (the Isolation Engine, `server.py` §4). No connection, credential, or DuckDB state survives between requests.
- Durable cross-pod state for genuinely persistent artifacts (e.g. hex tile pyramid build markers, PRs #146–#148) lives in **S3** markers, not pod memory, for exactly this reason.

**Do not** introduce per-pod in-memory caches keyed on something the client provides expecting the same pod will see the next request — under stateless HTTP it almost certainly won't, and even if it did once, scaling up replicas or a single rollout breaks the assumption silently.

### Rollout workflow

**Merge to `main` → redeploy dev only:**
```
kubectl apply -f k8s/dev-deployment.yaml
kubectl rollout restart deployment/dev-duckdb-mcp -n biodiversity
```

**Tag a release → redeploy prod:**
```
kubectl apply -f k8s/deployment.yaml
kubectl rollout restart deployment/duckdb-mcp -n biodiversity
```

Prod clones a pinned tag, so a release is two steps: **first** bump the `--branch vX.Y.Z`
pin in `k8s/deployment.yaml` to the new tag (separate commit, as in #151), **then** apply.
Never `kubectl apply -f k8s/deployment.yaml` to prod while the pin still points at the
previous tag — `deployment.yaml` changes (e.g. new `/healthz` probes) can reference code
the pinned tag doesn't yet have, and the rollout will stall on failing probes.

`kubectl apply` must precede `rollout restart` — a git push alone does not update the cluster.

---

This project uses two distinct, asynchronous AI agent processes. Do not confuse them.

---

## Process 1: Real-time MCP Tool (Small LLM)

A lightweight open-source LLM serves user requests in real time via the MCP tool.
It has no memory between requests. Its only context is what is injected at call time.

**Files it reads (injected as prompts):**
- `query-setup.md` — required DuckDB setup SQL, must run before every query
- `query-optimization.md` — short, actionable query-writing rules
- `h3-guide.md` — H3 hex data model and per-problem patterns (tiling, overlap, raster, lines)
- `datasets.md` — STAC catalog summary, dataset paths, column schemas
- `assistant-role.md` — role and response style instructions

**Rules for editing these files:**
- Write for a small, stateless model with limited context
- Instructions must be short, concrete, and unambiguous
- No debugging advice, no explanation of DuckDB internals, no "check X before Y"
- No meta-commentary about why rules exist — just the rules
- **Every SQL example must be the correct shape. Never include ❌ / antipattern SQL blocks** — small LLMs reproduce the structure verbatim (variable names, CTE shape) regardless of the ❌ marker. Negation priming overrides the marker. State the rule in prose; show only the correct query. See PR #134 / Issue #133 for measured 2× reduction in antipattern rate after removing ❌ blocks.
- Prefer positive framing in bullets: "Do X first, then Y" beats "❌ Don't Y before X". Existing prose "Never X" rules without SQL bodies can stay — the hazard is SQL antipatterns, not the word "never."
- When adding guidance for a niche case (e.g., line datasets when most datasets are polygons), keep it short and gate with a "Skip if your dataset is X" header so models doing the common case skim past. Niche fixes must not displace attention from the dominant workload.

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

## Failure modes to avoid

### Misdiagnosing a rule-violation as an infrastructure bug (March 2026)

A small LLM generated a query omitting h0 from a join (violating the rule in
`query-optimization.md`). The resulting slow query was diagnosed as "S3 DPP is broken"
rather than "the query violated the h0-in-join rule." Hours of investigation followed,
producing `optimization-design-notes.md` content that incorrectly characterized the
rule as wrong. A subsequent Claude session read those notes and tried to remove the
correct rule from `query-optimization.md`.

The fix is this document: keep the two processes and their files clearly separated, and
diagnose rule violations before infrastructure.

### ❌ examples in prompts get reproduced verbatim (May 2026)

Baseline matrix runs (issue #133) showed small LLMs reproducing the ❌-marked DPP
antipattern CTE — same variable names, same shape — at a 50% first-query rate.
The ❌ marker did not suppress reuse; if anything the named, copy-pasteable SQL
primed it. PR #134 removed the ❌ blocks and hoisted the rule into the CRITICAL
SQL RULES header, halving the antipattern rate.

When extending the prompt artifacts (e.g., adding a new "Problem N" section to
`h3-guide.md`), the temptation to mirror the structure of the existing problems —
which already showed ❌/✅ pairs — is high. Resist it. Even on guidance covering a
new failure mode (lines, sparse rasters, etc.), include only the ✅ form. Past sections
that still carry ❌ blocks are the next thing to migrate, not a pattern to copy.
