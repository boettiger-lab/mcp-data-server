# The bigger picture

The MCP Data Server is one part of a larger open-source effort to make
**terabyte-scale science data reachable by the AI tools researchers already use.**

## The problem

The open-source stack for large-scale data is mature and widely adopted: cloud-native
serializations (Parquet, Zarr), out-of-core query engines (DuckDB, Polars),
machine-readable metadata ([STAC](https://stacspec.org)), and object stores streamed by
range-request instead of downloaded. Yet in practice it stays underused, for two reasons:

1. **Much data sits in legacy or proprietary formats** that lack the standardized
   metadata these tools — and AI agents — need to interpret it.
2. **Coding agents reach for the wrong tools.** As assistants like Claude Code become the
   everyday interface for analysis, they default to the in-memory libraries most familiar
   to them (e.g. `pandas`), which silently fail at scale and, without metadata, misread the
   data. The mature stack sits idle not for lack of an audience, but because the agents that
   audience now turns to don't reach for it.

## Where this server fits

This server is the **link that lets a coding agent drive the cloud-native stack.** It does
two things a raw database connection cannot:

- **Points the agent at STAC metadata**, so the model finds the right dataset and reads its
  schema before querying — instead of guessing column names and silently returning wrong
  answers.
- **Exposes validated cloud-native engines** (DuckDB today; see the [roadmap](./roadmap))
  so the model uses out-of-core, streaming query paths instead of loading everything into
  memory.

It runs **locally** for sensitive data or on **autoscaling Kubernetes** for scale, and the
query guidance is injected at call time so even **small, locally-run open models** can drive
it — reducing dependence on closed models and keeping data on the researcher's hardware.

## The three components

| Component | Role |
|---|---|
| **[data-workflows](https://boettiger-lab.github.io/data-workflows/)** | Transforms disparate legacy and proprietary datasets into AI-ready, cloud-native formats (Parquet, Zarr) with rich STAC metadata. |
| **mcp-data-server** (this project) | Connects coding agents to that AI-ready data — grounding them in the STAC metadata and confining them to validated cloud-native engines. |
| **[jupyter-geoagent](https://jupyter-geoagent.readthedocs.io/)** | A JupyterLab extension integrating with Jupyter-AI — a data persona users drive with commercial *or* fully open models run locally. |

Each is independently useful; together they form an **AI-native bridge** to the
cloud-native stack the sciences are converging on.

## Why a bridge, not a fork

The pieces this connects — Parquet, Zarr, DuckDB, Polars, STAC, Jupyter, and the
[Model Context Protocol](https://modelcontextprotocol.io) — are individually mature
open-source projects with wide adoption. What is new is the **AI-native layer** that lets
researchers and their agents drive them *together*, at scale.

The leverage is that Jupyter and agentic coding assistants are already everyday tools, so
the bridge meets that existing base without asking anyone to learn a new toolchain: the
agent a scientist already uses makes the move from in-memory libraries to the cloud-native
stack on the user's behalf.

These methods were built and proven at the largest **earth-observation** scales — working
with partners including NASA, The Nature Conservancy, and California Fish & Wildlife. The
same architecture is **general-purpose**, and is being extended to the **life sciences**,
where spatially-resolved data (spatial transcriptomics, whole-slide microscopy,
3D-genome imaging) now arrives at terabyte scale, beyond the in-memory tools most
researchers rely on.
