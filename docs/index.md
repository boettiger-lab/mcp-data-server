---
layout: home

hero:
  name: MCP Data Server
  text: The bridge between AI agents and cloud-native data
  tagline: An open Model Context Protocol server that grounds coding agents in STAC metadata and confines them to validated cloud-native engines — so any LLM client can query terabyte-scale data over S3 without downloading it, misreading it, or silently failing at scale.
  actions:
    - theme: brand
      text: Quick Start
      link: /guide/quickstart
    - theme: alt
      text: The bigger picture
      link: /guide/vision
    - theme: alt
      text: Browse Datasets
      link: /guide/datasets

features:
  - title: Grounded in STAC metadata
    details: The agent browses a standard STAC catalog to find the right dataset and read its column schema before writing a query — so it interprets the data correctly instead of guessing.
  - title: Validated cloud-native engines
    details: Queries run against Parquet on S3 with DuckDB — fast, out-of-core, columnar. The agent reaches for streaming engines instead of in-memory libraries that silently break at scale.
  - title: Runs local or at scale
    details: Run it on your own hardware for sensitive data, or on autoscaling Kubernetes for terabyte workloads. The same server, the same tools, either way.
  - title: Drivable by small open models
    details: The query guidance is injected at call time, so even compact, locally-run open models can drive the workflow — reducing dependence on closed models and keeping data on your hardware.
  - title: Private data ready
    details: Pass S3 credentials per call to query private buckets alongside public data. Credentials are request-scoped, never logged, and never shared between clients.
  - title: Part of a larger effort
    details: One of three open-source components — with data-workflows and jupyter-geoagent — that together make the cloud-native stack reachable by the AI tools researchers already use.
---
