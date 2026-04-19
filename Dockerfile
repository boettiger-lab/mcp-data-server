FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        mcp \
        duckdb \
        pandas \
        uvicorn \
        tabulate \
        pystac \
        requests

# Pre-install DuckDB extensions so pods start offline-capable.
# Uses the default extension directory (~/.duckdb/extensions/) which the runtime
# will find automatically since the container runs as root.
RUN python -c "import duckdb; c = duckdb.connect(); c.sql('INSTALL httpfs; INSTALL spatial; INSTALL h3 FROM community')"

WORKDIR /app
