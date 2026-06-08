# Deployment Pinning: Baked Images Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the runtime `git clone` code-delivery mechanism with a single baked image artifact — dev tracks a moving `:main` tag, prod pins an immutable `:vX.Y.Z@sha256:…` digest — so "what version is running?" has one answer and replicas are identical by construction.

**Architecture:** The Dockerfile bakes application code (`COPY . /app`) on top of a cached dependency layer; `docker.yml` builds on every push to `main` (tag `:main` + `:<sha>`) and on `vX.Y.Z` tags (tag `:vX.Y.Z` + `:<sha>`), with the weekly cron rebuilding `:main` with no cache to refresh unpinned deps + base-image security patches. dev pins `:main` + `imagePullPolicy: Always`; prod pins `:vX.Y.Z@sha256:…` + `IfNotPresent`, promoted by reading the digest of the validated build. Resolves issue #169.

**Tech Stack:** Docker / GHCR, GitHub Actions (`docker/build-push-action@v6`, GHA layer cache), Kubernetes (NRP Nautilus, namespace `biodiversity`).

---

## File Structure

- `Dockerfile` (modify) — add cached deps layer from `requirements.txt`, `COPY . /app`, `CMD`; drop `git`.
- `.dockerignore` (create) — exclude `node_modules`, `.venv`, `.git`, caches, tiles from the build context.
- `.github/workflows/docker.yml` (modify) — build on push-to-main + tags; `:main`/`:<sha>`/`:vX.Y.Z` tagging; GHA cache for pushes, `no-cache`+`pull` for cron; stop pushing `:latest`.
- `k8s/dev-deployment.yaml` (modify) — `:main` + `Always`, drop `command`/`args` git clone.
- `k8s/deployment.yaml` (modify) — `:vX.Y.Z@sha256:…` + `IfNotPresent`, drop `command`/`args` git clone.
- `AGENTS.md` (modify) — rewrite the `Rollout workflow` section for the baked-image model.

**Sequencing (critical):** Phase 1 (Tasks 1–5) changes the build pipeline and docs only — it does **not** touch running deployments, and it deliberately stops moving `:latest` so the still-deployed old manifests keep pulling the frozen deps-only `:latest` and keep git-cloning successfully. Phase 2 (Task 6) migrates dev. Phase 3 (Task 7) cuts the release and migrates prod. Phase 4 (Task 8) cleans up.

---

## Task 1: Add `.dockerignore`

**Files:**
- Create: `.dockerignore`

- [ ] **Step 1: Create the file**

```
.git
.github
.venv
node_modules
**/__pycache__
*.pyc
.pytest_cache
.worktrees
.claude
.roo
.continue
.vscode
tiles
```

- [ ] **Step 2: Verify the big directories are excluded**

Run: `git check-ignore -v --no-index node_modules .venv tiles 2>/dev/null; echo "---"; du -sh node_modules .venv 2>/dev/null`
Expected: `node_modules` and `.venv` exist and are large — confirming they must be excluded (the `.dockerignore` keeps them out of the build context).

- [ ] **Step 3: Commit**

```bash
git add .dockerignore
git commit -m "build: add .dockerignore to keep node_modules/.venv/.git out of image context"
```

---

## Task 2: Bake code into the Dockerfile

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Replace the Dockerfile with the baked-code version**

```dockerfile
FROM python:3.12-slim

# ca-certificates for HTTPS to S3 / STAC. git is intentionally NOT installed:
# code is baked into the image (COPY below), not cloned at pod startup.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer — re-resolves ONLY when requirements.txt changes, so code-only
# rebuilds reuse it (with GHA cache, those finish in seconds). The weekly cron
# builds with --no-cache so this layer actually re-resolves the unpinned deps.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install DuckDB extensions so pods start offline-capable.
# Uses the default extension directory (~/.duckdb/extensions/) which the runtime
# will find automatically since the container runs as root.
RUN python -c "import duckdb; c = duckdb.connect(); c.sql('INSTALL httpfs; INSTALL spatial; INSTALL h3 FROM community')"

# Application code last, so a code change rebuilds only this cheap layer.
COPY . /app

CMD ["python", "server.py"]
```

- [ ] **Step 2: Build the image locally to verify it succeeds**

Run: `docker build -t mcp-data-server:plantest .`
Expected: build completes; final layers are `COPY . /app` and `CMD`. If Docker is unavailable locally, skip — CI (Task 3) is the authoritative gate.

- [ ] **Step 3: Verify the baked image starts the server and code is present**

Run: `docker run --rm --entrypoint sh mcp-data-server:plantest -c 'ls /app/server.py /app/stac.py /app/h3-guide.md && python -c "import duckdb, mcp, pystac"'`
Expected: the three files list without error and the imports succeed (code + deps are baked in).

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
git commit -m "build: bake application code into image (COPY . /app); drop runtime git clone dependency"
```

---

## Task 3: Update the build workflow

**Files:**
- Modify: `.github/workflows/docker.yml`

- [ ] **Step 1: Replace `docker.yml` with the build-on-push version**

```yaml
name: Build Docker image

on:
  push:
    branches: [main]
    tags: ['v*']
  schedule:
    - cron: '0 6 * * 1'  # Mondays 06:00 UTC — refresh base image + unpinned deps
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Set up Buildx
        uses: docker/setup-buildx-action@v3

      - name: Compute image tags
        id: tags
        run: |
          repo=ghcr.io/boettiger-lab/mcp-data-server
          case "${{ github.event_name }}" in
            push)
              if [ "${{ github.ref_type }}" = "tag" ]; then
                # Release tag (vX.Y.Z): immutable version tag + immutable sha.
                tags="$repo:${{ github.ref_name }}"$'\n'"$repo:${{ github.sha }}"
              else
                # Push to main: moving dev tag + immutable sha.
                tags="$repo:main"$'\n'"$repo:${{ github.sha }}"
              fi
              ;;
            *)
              # schedule / workflow_dispatch: dep+base refresh. Move ONLY the dev
              # tag — never mint a :sha or :vX.Y.Z from a non-push event.
              tags="$repo:main"
              ;;
          esac
          {
            echo "tags<<EOF"
            echo "$tags"
            echo "EOF"
          } >> "$GITHUB_OUTPUT"

      - name: Build and push
        id: build
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.tags.outputs.tags }}
          # Pushes reuse the cached deps layer (fast code-only builds). The weekly
          # cron sets no-cache + pull so it re-resolves unpinned deps and pulls a
          # fresh base image (security patches); cache-to still warms the cache.
          cache-from: type=gha
          cache-to: type=gha,mode=max
          no-cache: ${{ github.event_name == 'schedule' }}
          pull: ${{ github.event_name == 'schedule' }}

      - name: Report image digest
        run: |
          {
            echo "### Image digest"
            echo '```'
            echo "${{ steps.build.outputs.digest }}"
            echo '```'
          } >> "$GITHUB_STEP_SUMMARY"
```

- [ ] **Step 2: Lint the workflow YAML**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/docker.yml')); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Confirm `:latest` is no longer pushed**

Run: `grep -n "latest" .github/workflows/docker.yml || echo "no :latest references — correct"`
Expected: `no :latest references — correct` (the old `:latest` image stays frozen in GHCR so still-deployed old manifests keep working until migrated).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/docker.yml
git commit -m "ci: build image on push to main + release tags; tag :main/:sha/:vX.Y.Z; cron refreshes deps via no-cache"
```

---

## Task 4: Rewrite the AGENTS.md rollout workflow

**Files:**
- Modify: `AGENTS.md` (the `### Rollout workflow` section, currently lines ~39–59)

- [ ] **Step 1: Replace the entire `### Rollout workflow` section**

Replace from the `### Rollout workflow` heading through the line `kubectl apply must precede rollout restart — a git push alone does not update the cluster.` with:

````markdown
### Rollout workflow

Application code is **baked into the image** (`COPY . /app` in the `Dockerfile`); pods no
longer `git clone` at startup. `docker.yml` builds on every push to `main` and on `vX.Y.Z`
release tags. The image is the unit of release.

**Tags CI produces:**
- `:main` — moving; rebuilt on every push to `main` and by the weekly cron. **dev** tracks this.
- `:<git-sha>` — immutable; one per commit.
- `:vX.Y.Z` — immutable; built on release tags. **prod** pins this (by digest, below).

**Merge to `main` → redeploy dev:**
```
kubectl apply -f k8s/dev-deployment.yaml
kubectl rollout restart deployment/dev-duckdb-mcp -n biodiversity
```
dev pins `:main` with `imagePullPolicy: Always`, so the restart pulls the freshest build.
**Wait for the `docker.yml` run on your merge to go green first** — rolling before the
image is pushed gives `ImagePullBackOff`.

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

Verify all prod replicas converge on a single digest after rollout:
```
kubectl -n biodiversity get pods -l app=duckdb-mcp \
  -o custom-columns='NAME:.metadata.name,IMAGE:.status.containerStatuses[0].imageID'
```
````

- [ ] **Step 2: Verify the section reads coherently**

Run: `sed -n '/### Rollout workflow/,/converge on a single digest/p' AGENTS.md | head -60`
Expected: the new section prints in full, no leftover `git clone --branch` references.

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs: rewrite rollout workflow for baked-image model (dev :main, prod digest-pinned)"
```

---

## Task 5: Merge Phase 1 and confirm CI produces baked images

**Files:** none (CI verification)

- [ ] **Step 1: Push the branch and open/merge the PR (squash)**

```bash
git push -u origin <branch>
gh pr create --fill
# after review:
gh pr merge --squash --delete-branch
```

- [ ] **Step 2: Watch the `docker.yml` run triggered by the merge**

Run: `gh run list --workflow=docker.yml --limit 3`
Expected: a `push` run for the merge commit, conclusion `success`.

- [ ] **Step 3: Confirm the baked `:main` image now contains code**

Run: `docker run --rm --entrypoint sh ghcr.io/boettiger-lab/mcp-data-server:main -c 'ls /app/server.py && python -c "import mcp"'`
Expected: `/app/server.py` lists and the import succeeds. (If Docker isn't available locally, instead confirm the tag exists: `docker buildx imagetools inspect ghcr.io/boettiger-lab/mcp-data-server:main`.)

- [ ] **Step 4: Confirm old `:latest` is untouched (old manifests still safe)**

Run: `docker buildx imagetools inspect ghcr.io/boettiger-lab/mcp-data-server:latest --format '{{.Manifest.Digest}}'`
Expected: a digest that has NOT changed from before the merge (the workflow no longer pushes `:latest`). The still-deployed old manifests keep git-cloning against this frozen deps-only image.

---

## Task 6: Migrate dev to the baked `:main` image

> **Load the `nrp-k8s` skill before running any kubectl command.** Namespace is `biodiversity`.

**Files:**
- Modify: `k8s/dev-deployment.yaml`

- [ ] **Step 1: Replace the image + command block (lines 26–32)**

Old:
```yaml
        image: ghcr.io/boettiger-lab/mcp-data-server:latest
        command: ["/bin/sh", "-c"]
        args:
          - |
            git clone https://github.com/boettiger-lab/mcp-data-server.git /app && \
            cd /app && \
            python server.py
```
New:
```yaml
        image: ghcr.io/boettiger-lab/mcp-data-server:main
        imagePullPolicy: Always
```
(The image's `CMD ["python", "server.py"]` runs the server; the `env`, `resources`,
`ports`, and probe blocks below are unchanged.)

- [ ] **Step 2: Validate the manifest**

Run: `kubectl apply --dry-run=client -f k8s/dev-deployment.yaml`
Expected: `deployment.apps/dev-duckdb-mcp configured (dry run)` with no schema errors.

- [ ] **Step 3: Commit**

```bash
git add k8s/dev-deployment.yaml
git commit -m "k8s(dev): run baked :main image with imagePullPolicy: Always; drop runtime git clone"
```

- [ ] **Step 4: Apply and roll dev**

Run:
```
kubectl apply -f k8s/dev-deployment.yaml
kubectl rollout restart deployment/dev-duckdb-mcp -n biodiversity
kubectl rollout status deployment/dev-duckdb-mcp -n biodiversity --timeout=180s
```
Expected: rollout completes; pods become Ready (probes pass). If `ImagePullBackOff`, the `:main` build from Task 5 hasn't finished — wait for it.

- [ ] **Step 5: Validate dev serves traffic and reports the baked image**

Run:
```
kubectl -n biodiversity get pods -l app=dev-duckdb-mcp \
  -o custom-columns='NAME:.metadata.name,IMAGE:.status.containerStatuses[0].imageID'
curl -sf https://dev-duckdb-mcp.nrp-nautilus.io/healthz && echo " healthz ok"
```
Expected: both pods report the **same** `:main` imageID digest, and `/healthz` returns ok. Smoke-test an MCP query against dev per the geo-agent validation workflow.

---

## Task 7: Cut the release and migrate prod (promote by digest)

> **Load the `nrp-k8s` skill before running any kubectl command.**

**Files:**
- Modify: `k8s/deployment.yaml`

- [ ] **Step 1: Tag the validated commit and push**

```bash
git tag v0.6.8
git push origin v0.6.8
```

- [ ] **Step 2: Wait for CI to build `:v0.6.8`, then read its digest**

Run:
```
gh run list --workflow=docker.yml --limit 3
docker buildx imagetools inspect ghcr.io/boettiger-lab/mcp-data-server:v0.6.8 --format '{{.Manifest.Digest}}'
```
Expected: the `push` (tag) run is `success`; the inspect prints `sha256:<digest>`. (Alternatively copy the digest from that run's job summary.)

- [ ] **Step 3: Pin prod to the digest (replace lines 26–32)**

Old:
```yaml
        image: ghcr.io/boettiger-lab/mcp-data-server:latest
        command: ["/bin/sh", "-c"]
        args:
          - |
            git clone --branch v0.6.7 https://github.com/boettiger-lab/mcp-data-server.git /app && \
            cd /app && \
            python server.py
```
New (substitute the real digest from Step 2):
```yaml
        image: ghcr.io/boettiger-lab/mcp-data-server:v0.6.8@sha256:<DIGEST>
        imagePullPolicy: IfNotPresent
```

- [ ] **Step 4: Validate and commit**

Run: `kubectl apply --dry-run=client -f k8s/deployment.yaml`
Expected: `deployment.apps/duckdb-mcp configured (dry run)`.

```bash
git add k8s/deployment.yaml
git commit -m "k8s(prod): pin v0.6.8 by digest with imagePullPolicy: IfNotPresent; drop runtime git clone (Fixes #169)"
git push
```

- [ ] **Step 5: Apply and roll prod**

Run:
```
kubectl apply -f k8s/deployment.yaml
kubectl rollout restart deployment/duckdb-mcp -n biodiversity
kubectl rollout status deployment/duckdb-mcp -n biodiversity --timeout=300s
```
Expected: rollout completes, all 6 pods Ready.

- [ ] **Step 6: Verify every prod replica converges on the one digest**

Run:
```
kubectl -n biodiversity get pods -l app=duckdb-mcp \
  -o custom-columns='NAME:.metadata.name,IMAGE:.status.containerStatuses[0].imageID'
curl -sf https://duckdb-mcp.nrp-nautilus.io/healthz && echo " healthz ok"
```
Expected: all 6 pods report the **identical** `@sha256:<digest>` imageID (the mixed-digest condition from #169 is gone), and `/healthz` is ok.

---

## Task 8: Cleanup and close the issue

**Files:** none

- [ ] **Step 1: Confirm nothing in-repo still references the retired `:latest` image**

Run: `grep -rn ":latest" k8s/ .github/ AGENTS.md README.md 2>/dev/null || echo "no :latest references remain"`
Expected: `no :latest references remain`. (If any appear, evaluate and migrate them.)

- [ ] **Step 2: Confirm both deployments are on baked images with explicit pull policies**

Run: `grep -nE "image:|imagePullPolicy:|git clone" k8s/deployment.yaml k8s/dev-deployment.yaml`
Expected: prod `:v0.6.8@sha256:…` + `IfNotPresent`; dev `:main` + `Always`; **no `git clone`** in either.

- [ ] **Step 3: Verify the live `imagePullPolicy` drift (#169 §3) is reconciled**

Run:
```
kubectl -n biodiversity get deploy duckdb-mcp dev-duckdb-mcp \
  -o jsonpath='{range .items[*]}{.metadata.name}{": "}{.spec.template.spec.containers[0].imagePullPolicy}{"\n"}{end}'
```
Expected: `duckdb-mcp: IfNotPresent` and `dev-duckdb-mcp: Always` — matching git (the apply in Tasks 6–7 reconciled the live drift).

- [ ] **Step 4: Close issue #169**

```bash
gh issue comment 169 --body "Resolved: code is now baked into the image, dev tracks :main (Always), prod pins vX.Y.Z@sha256 (IfNotPresent). All prod replicas converge on one digest; the two-pinning-mechanism drift and the live imagePullPolicy drift are gone. See AGENTS.md → Rollout workflow."
gh issue close 169
```

(Note: the `Fixes #169` in Task 7's commit will auto-close on merge to `main`; this step is the explicit fallback if the digest commit lands directly on `main`.)

---

## Follow-ups (out of scope — note, do not implement here)

- **Pin dependencies** (`requirements.txt` with versions + Dependabot/Renovate). The unpinned deps are why the weekly cron must `no-cache`-rebuild to refresh them. With pinned deps, dep bumps become reviewed commits and the cron could shrink to base-OS security refresh only.
- **Gate the image build on tests passing** (`workflow_run` after `test.yml`, or a single CI graph) so a red build never bakes `:main` and auto-deploys it to dev.
- **CD for dev** — trigger the dev rollout from CI on a successful `:main` build, removing the manual "wait for green then rollout" step.
