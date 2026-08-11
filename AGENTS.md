# Agent Architecture

> **The `.md` files in this repo are runtime prompt artifacts, not documentation.**
> `server.py` loads them at startup and injects their content into MCP tool descriptions
> and prompts. Editing them changes what the agent is instructed to do.
> `README.md` is the only human-facing documentation.

## Development & test environment — READ FIRST

**Dev is the pre-prod canary: it tracks `main` and is never repointed by hand.**
`dev-duckdb-mcp.nrp-nautilus.io` runs the current `:main` build (pinned by digest,
≥2 replicas) precisely so merged changes are exercised on real infrastructure —
including cross-pod skew — before prod. All MCP/guidance testing runs against
**dev**, never a local process. Dev has one job; it is *not* a place to host a
branch. Repointing dev to a branch stomps `main` and decays into the hand-pinning
loop that keeps breaking it (#341/#366).

**To test a change before it merges**, do not repoint dev. Options:
- **Merge-then-validate (works today):** merge to `main` → dev rolls onto that
  digest → run the headless matrix with `MCP_URL=dev`. This is the current gate.
- **Preview env (planned, #366):** a `preview` label builds `:pr-<n>` for an
  ephemeral `dev-pr-<n>` — the build trigger and CI deploy land with the NRP
  deploy token.
- **Guidance PR gate (planned, #245):** the matrix Job runs the PR's `server.py`
  as a sidecar (git-checkout of the branch — no image build) so the *exact* PR
  guidance is validated pre-merge.

**Never run `server.py` (the MCP server) locally in this JupyterLab environment.**
It loads DuckDB plus the full STAC catalog in-process; running it here OOM'd the shared
pod and took down the instance (2026-07-07). There is no "local MCP" testing path —
if you catch yourself starting `server.py`, `uvicorn`, or a wrapper that imports the
server on this machine, stop. The workflow is: land the change on dev (dev tracks
`:main` — see Rollout below), then validate on dev with the headless matrix
(see *Validating guidance changes*).

**When NRP Ceph (`s3-west.nrp-nautilus.io`) is down**, don't improvise a fallback:
follow [docs/guide/mirror-failover.md](docs/guide/mirror-failover.md) — the single
runbook for the MinIO mirror and the mirror-configured head at
`duckdb-mcp.carlboettiger.info`. Its "For agents querying through MCP" section is
the short version: the normal STAC tools work on a mirror head; use the paths the
tools return verbatim. Older outage notes elsewhere describing a source.coop route
are superseded.

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

- **Prod:** `https://duckdb-mcp.nrp-nautilus.io` — `k8s/deployment.yaml` (replica count and image pin live there)
- **Dev:** `https://dev-duckdb-mcp.nrp-nautilus.io` — `k8s/dev-deployment.yaml`; must stay ≥2 replicas so cross-pod bugs surface here before prod
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

Application code is **baked into the image** (`COPY . /app` in the `Dockerfile`); pods no
longer `git clone` at startup. `docker.yml` builds on every push to `main` and on `vX.Y.Z`
release tags. The image is the unit of release.

**Tags CI produces:**
- `:main` — moving; rebuilt on every push to `main` and by the weekly cron. **dev** tracks this.
- `:<git-sha>` — immutable; one per commit.
- `:vX.Y.Z` — immutable; built on release tags. **prod** pins this (by digest, below).

**Merge to `main` → redeploy dev (promote by digest — same discipline as prod):**
Dev pins an immutable `:main@sha256:…` (tag for humans, digest enforced), **not** the
bare moving `:main` tag. `:main` + `imagePullPolicy: Always` is *non-convergent*: `Always`
re-resolves the mutable `:main` → digest independently per pod at each (re)start, and
`:main` moves on every push to `main` plus the weekly cron. So pods brought up seconds
apart, or any single later liveness/eviction/crash/cron restart, silently drift onto
different builds — the exact cross-pod skew dev's ≥2 replicas exist to catch (issue #341).
Pinning a digest makes every dev pod run one reproducible build.

1. **Wait for the `docker.yml` run on your merge to go green first** — rolling before the
   image is pushed gives `ImagePullBackOff`.
2. Read the freshly-built `:main` digest from the build run's job summary, or:
   `docker buildx imagetools inspect ghcr.io/boettiger-lab/mcp-data-server:main --format '{{.Manifest.Digest}}'`
3. Set `image: ghcr.io/boettiger-lab/mcp-data-server:main@sha256:<digest>` in
   `k8s/dev-deployment.yaml` (keep `imagePullPolicy: IfNotPresent`).
4. `kubectl apply -f k8s/dev-deployment.yaml`
5. `kubectl rollout restart deployment/dev-duckdb-mcp -n biodiversity`
6. Verify convergence (below) — this is dev's canary job; do not skip it.

**Tag a release → redeploy prod (promote by digest):**
1. `git tag vX.Y.Z && git push origin vX.Y.Z`, then wait for `docker.yml` to build `:vX.Y.Z`.
2. Read the digest from the build run's job summary, or:
   `docker buildx imagetools inspect ghcr.io/boettiger-lab/mcp-data-server:vX.Y.Z --format '{{.Manifest.Digest}}'`
3. Set `image: ghcr.io/boettiger-lab/mcp-data-server:vX.Y.Z@sha256:<digest>` in
   `k8s/deployment.yaml` (separate commit).
4. `kubectl apply -f k8s/deployment.yaml`
5. `kubectl rollout restart deployment/duckdb-mcp -n biodiversity`

prod pins an immutable `:vX.Y.Z@sha256:…` (tag for humans, digest enforced — if they ever
disagree, the digest wins). **Never apply prod while the manifest points at an image CI
hasn't built yet** — the rollout stalls on `ImagePullBackOff`. `kubectl apply` must precede
`rollout restart`; a git push alone does not update the cluster.

**No `docker` CLI (e.g. a JupyterLab session)?** `imagetools inspect` needs it, and the
GHCR packages API needs a `read:packages` token most of our `gh` logins don't carry. An
anonymous pull token against the registry v2 API works anywhere `curl` does — substitute
`main` or `vX.Y.Z` for `<tag>`:
```
repo=boettiger-lab/mcp-data-server
tok=$(curl -s "https://ghcr.io/token?scope=repository:$repo:pull" \
      | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
curl -sI -H "Authorization: Bearer $tok" \
  -H "Accept: application/vnd.oci.image.index.v1+json" \
  -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
  "https://ghcr.io/v2/$repo/manifests/<tag>" | grep -i '^docker-content-digest:'
```
Sanity-check the method before trusting it: run it on the tag prod already pins and
confirm the digest matches `k8s/deployment.yaml`.

**Verify all replicas converge on a single digest after rollout** (both dev and prod —
dev is the multi-replica canary, so its convergence matters as much as prod's). Swap the
label for the deployment you rolled (`duckdb-mcp` for prod, `dev-duckdb-mcp` for dev):
```
kubectl -n biodiversity get pods -l app=duckdb-mcp \
  -o custom-columns='NAME:.metadata.name,IMAGE:.status.containerStatuses[0].imageID'
```
Every live (non-`Terminating`) pod must report the **same** `imageID` digest, and it must
match the digest pinned in the manifest. Stuck `Terminating` zombie pods on unreachable
NRP nodes are out of the Service's endpoints, so ignore them here — **never** force-delete
them (NRP ops policy).

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

## Where guidance lives (layering model)

All of `query-optimization.md` and `h3-guide.md` are injected **verbatim into the
`query` tool description** (`TOOL_INJECTED_CONTEXT` in `server.py`), right after the
hardcoded CRITICAL block. So putting a rule in the server.py CRITICAL block instead of
an `.md` file does **not** make it more available to the model — it's the same blob.
The only differences are *maintainability* (hardcoded Python vs the designated runtime
artifacts) and *prominence* (the CRITICAL block is first and stamped MUST-FOLLOW —
scarce attention that every addition dilutes).

So choose the home by **what kind of fact it is**, and pick the **most specific** layer
that fits. Redundancy across layers is a cost (drift + diluted attention), not a
feature — state a thing once, in one layer, and point to detail rather than copy it.

| Layer | Owns | Examples |
|---|---|---|
| **dataset STAC** (in `boettiger-lab/data-workflows`) | facts true of *one dataset* — surfaced by `get_stac_details` exactly when the model is choosing columns | "this file repeats sites — dedup by `ramsarid`"; no-data sentinel codes; which column is the feature id; native resolution |
| **`h3-guide.md`** | general H3 / hex model & patterns | resolution direction, cell-area table, parent-resolution joins, multiple-rows-per-*hex* |
| **`query-optimization.md`** | general SQL-writing rules, cross-dataset | include `h0` in joins, NaN poisons `SUM`, fuzzy vs exact text match |
| **server.py CRITICAL block** | the few foundational invariants, read first, ~never change | no tables, use `read_parquet`, trust STAC paths verbatim, DPP mask-before-aggregate |
| **server.py code** | runtime tool *behavior* | the 50-row truncation footer, geometry-column drop |

Decision rule when you catch a model error:
- A wrong answer caused by a **property of one dataset** (duplicate rows, sentinel
  values, an id column) → fix the **dataset STAC**, not the global prompt. A global rule
  asserting "geoparquet has duplicate feature rows" is false for clean files and pollutes
  every query; the per-dataset note is true exactly where it's needed.
- A **general hex/SQL pattern** → the matching `.md` file. Use a concrete dataset only as
  an *illustrative example* of the general pattern, never as the rule itself.
- The **server.py CRITICAL block is not a priority flag.** "This error is the most common"
  is not a reason to hardcode a rule there — that just crowds the invariants. Put the
  guidance in its proper layer; if it's a per-dataset fact, STAC is what surfaces it at
  the right moment.

---

## Validating guidance changes (headless model-suite test)

**Every change to a prompt artifact** (`h3-guide.md`, `query-optimization.md`,
`query-setup.md`, `assistant-role.md`, `datasets.md`) **must be validated against the
open-model suite on dev before it ships to prod.** It is not done until the matrix
shows **both**: (a) the targeted failure is fixed, and (b) no regression on the
standing baseline set (below). These files are runtime instructions for small open
models — the ground truth is not "does the SQL I write run," it's "does the model
*generate* the right SQL after reading this guidance." Only running the models confirms that. A fix verified solely by
running the corrected query yourself proves the pattern works, not that the guidance
steers the model to it.

Guidance is shared context: text added for one trap is read by every query. Tighter
wording is safer wording — say it once, in the most-read place, and stop. Prefer
editing an existing rule over adding a parallel one; never restate a rule the suite
already passes.

The question bank, gold answers, and tiers live in **`boettiger-lab/geo-agent-benchmark`**
(private) — read its `AGENTS.md` for the full runbook. Execution is the harness in
**`boettiger-lab/open-llm-proxy`** under `headless/`, which replays the real geo-agent
tool-use loop from the command line against the LLM proxy, so prompt assembly, the
tool-call parser, and the MCP transport stay in sync with the browser app by
construction.

**Workflow for a guidance change:**

1. Merge the change and let dev pick it up — dev tracks `:main` and serves the
   injected guidance. (Confirm: `curl -s https://dev-duckdb-mcp.nrp-nautilus.io/version`
   git_sha matches `main`; the new text appears in the `query` tool description.)
2. Run the `regression` tier as one-shot k8s Jobs (they pull `PROXY_KEY` from the
   cluster Secret — no local creds, and reading that Secret locally is not the
   supported path). **`MCP_URL` is what points a run at dev** — every app repo
   commits an `mcp_url` aimed at a production head, so a run without it measures the
   *old* guidance no matter what dev is serving:

   ```bash
   cd ../geo-agent-benchmark
   MCP_URL=https://dev-duckdb-mcp.nrp-nautilus.io/mcp \
     APP_REPO=boettiger-lab/ca-30x30 TIER=regression \
     MODELS="z-ai/glm-5.2" TRIALS=2 TAG=<short-tag> \
     ./scripts/run_benchmark.sh
   ```

   One Job per model runs them in parallel (one pod each, `MODELS` holding a single
   value, distinct `TAG`s); one Job with several models runs them serially in one pod.
   The runner is per-app, so repeat per app repo to cover a whole tier. The tier
   already carries the trap gates *and* the standing baseline, so the fix and the
   regression check share one run.

3. Read the transcripts from `kubectl -n biodiversity logs job/<JOB_NAME>`: confirm
   (a) the models emit the intended SQL pattern and the correct answer and the prior
   failure form is gone (zero occurrences), **and** (b) every baseline question still
   resolves to its known-good answer — no question the change wasn't aimed at got worse.
   Check the Job log's `--- mcp: ---` line says dev before believing any of it.
   A regression on the baseline blocks promotion to prod even if the targeted trap is
   fixed — fix forward or revert on dev first.

**Pick the models deliberately.** Always include any model that previously exhibited
the failure (the logs naming the symptom are the regression set), plus a spread of the
suite. `geo-agent-benchmark/suite/models.yaml` is the model registry
(`python3 scripts/models.py standard`); an app's `k8s/configmap.yaml` picker is what
`MODELS`-unset measures — deployed behavior. Establish a ground-truth answer first by
running the correct query yourself, so the transcripts have something to check against.

**The standing baseline is the `regression` tier** in `geo-agent-benchmark` — a fixed
set of questions with golden answers (`suite/gold/`), re-run on every guidance change
to catch collateral damage, and graded mechanically (`scripts/grade.py`). Grow-on-fix:
when a change fixes a new trap, add the question that exposed it to the tier with a
`trap:` tag, so future changes can't silently reintroduce it.

To A/B-test app-level *system prompt* wording (not the MCP-injected guides) without
forking the app, `run-matrix-k8s.sh` takes `SYSTEM_PROMPT_APPEND_FILE`.

---

## Failure modes to avoid

### Running the MCP server locally OOM'd the shared pod (July 2026)

To validate a guidance change without merging, an agent started `server.py` locally in
the JupyterLab environment and pointed the headless runner at it. The server loads
DuckDB + the full STAC catalog in-process; the memory pressure OOM'd the shared pod and
crashed the instance. There is no local-MCP testing path — dev is the development
server. Land the change on dev and test there. See *Development & test environment* at
the top of this file.

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
