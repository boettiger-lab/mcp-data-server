---
layout: home

hero:
  name: MCP Data Server
  text: SQL access to geospatial data — for AI agents
  tagline: An MCP server that lets any LLM client query petabytes of H3-indexed environmental and biodiversity data via DuckDB and S3. No database setup required.
  actions:
    - theme: brand
      text: Quick Start
      link: /guide/quickstart
    - theme: alt
      text: Browse Datasets
      link: /guide/datasets
    - theme: alt
      text: GitHub
      link: https://github.com/boettiger-lab/mcp-data-server

features:
  - title: Zero Configuration
    details: Point any MCP-compatible LLM client at the hosted endpoint. No database to install or configure.
  - title: DuckDB on S3
    details: Queries run directly against Parquet files in S3 using DuckDB — fast columnar analytics without moving data.
  - title: H3 Spatial Indexing
    details: All datasets use Uber's H3 hexagonal grid for efficient spatial joins and area calculations across resolutions.
  - title: Isolated Execution
    details: Each query runs in a fresh DuckDB instance. Credentials are request-scoped and never shared between clients.
  - title: STAC Catalog
    details: Datasets are discoverable through a standard STAC catalog. The agent browses and resolves S3 paths dynamically.
  - title: Private Data Ready
    details: Pass S3 credentials per-call to query private buckets alongside public data in the same query.
---
