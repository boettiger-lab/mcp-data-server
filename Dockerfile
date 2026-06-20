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

# Version stamp, passed by docker.yml (APP_VERSION = the git tag on release builds,
# else "main"; GIT_SHA = the commit). Last so a version/sha change rebuilds only this
# trivial layer, never the deps. Read at runtime by server.py (issue #221).
ARG APP_VERSION=dev
ARG GIT_SHA=unknown
ENV APP_VERSION=$APP_VERSION GIT_SHA=$GIT_SHA

CMD ["python", "server.py"]
