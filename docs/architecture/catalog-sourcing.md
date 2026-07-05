# Catalog sourcing: "carry the links" vs "the whole catalog"

How this server and its client apps discover and read datasets — and what that
means for swapping, mixing, and backing up data sources (e.g. Ceph ↔ source.coop
mirror ↔ minio). Reference/design note; the actionable follow-ups are tracked in
[#263](https://github.com/boettiger-lab/mcp-data-server/issues/263) and
[#264](https://github.com/boettiger-lab/mcp-data-server/issues/264).

## Two models

- **Whole catalog** — a component holds/traverses a single root catalog (the
  server's startup pre-warm of the Ceph root `STAC_CATALOG_URL`).
- **Carry the links** — a component holds direct per-dataset links
  (`collection_url` / inline `collection`), from *any* source, needing no root.

**The architecture is fundamentally "carry the links."** The whole-catalog
pre-warm is a thin, swappable discovery/default convenience — it is *not*
load-bearing for the dominant workflows. Treat per-collection links as the
primary contract; keep the whole-catalog optional.

## Reliance map

| Surface | Whole-catalog reliance | Notes |
|---|---|---|
| MCP `query` | **None** | No catalog references in the query path; runs SQL on whatever paths it's given. ~85% of MCP calls. |
| MCP `get_stac_details` / `get_collection` | **Fallback only** | Resolution is inline `collection` → `catalog_url` → default cache. In production ~always inline (via `get_schema` + the `injectInlineStac` wrapper), bypassing the pre-warm. |
| MCP `browse_stac_catalog`, `catalog://list/{id}` | **Full** | Served from the pre-warmed `STAC_DATASETS`; Ceph-bound and Ceph-pointing. ~1.4% of calls, optional discovery. |
| MCP startup | **Soft** | Pre-warm + fail-fast; `STAC_ALLOW_DEGRADED_START` (#262) removed the hard dependency. |
| geo-agent CDN lib | **~None for its own data** | Builds its own `DatasetCatalog` from config (`collection_url`s + `catalog`, fetched client-side from any host) and forwards them inline to MCP. Touches the server's whole-catalog only via optional `browse_stac_catalog`. |

So the Ceph-root pre-warm is load-bearing for only `browse_stac_catalog` + the
`catalog://` resources + the default-resolution fallback — a thin slice.

## Key operational consequence for app resilience

**A geo-agent app's data source is its *own* config, not the MCP server's
`STAC_CATALOG_URL`.** It fetches each `collection_url`'s STAC JSON *client-side*
and builds map layers directly from it (not via the MCP `get_collection` tool);
the paths the agent reasons about come from that config, forwarded inline to MCP.

Therefore:

- The **server-side** source.coop fallback (`_href_to_s3` rewrite + `source_coop`
  secret) covers the **MCP `query` and STAC-tool** paths — the flows that go
  *through* the server. It does **not** cover an app's client-side layer fetches.
- To make an app itself resilient to a Ceph outage, **repoint its config** (the
  `catalog` + `collection_url`s in `layers-input.json`) at the backup source —
  *or* route its layer/collection fetches through the MCP so the server-side
  fallback applies. This is a per-app decision, not something the server can do
  for it.

## Swap / mix / match

- **Swap a backup catalog during an outage.** App: repoint its config (instant,
  client-side, no server change). Server: swap `STAC_CATALOG_URL` to a backup
  root to restore `browse`/resources/default-resolution (the hot path doesn't
  need it). Because there is no top-level catalog on source.coop, the source.coop
  case is handled per-collection; a minio backup can provide a real root.
- **Mix sources.** Natural *at the link level* — each dataset carries its own
  `collection_url` from any host, and MCP `get_stac_details` renders whatever
  inline STAC it's given. The two chokepoints that tripped up *novel* sources
  ([#264](https://github.com/boettiger-lab/mcp-data-server/issues/264)) are now
  handled by the **source registry** (`s3config.py`, extensible per deployment
  via `S3_SOURCES`):
  1. **Query endpoint secrets** — every registry source with a `secret` entry
     gets a prefix-scoped DuckDB secret on every connection (query *and* tiles);
     unregistered sources are reachable per-request via `s3_endpoint`/`s3_scope`
     (+ creds if private).
  2. **href → `s3://` rewriting** — registry-driven prefix rewrites (built-ins:
     `s3-west`, `data.source.coop`). Hosts outside the registry still pass
     through as HTTPS, but the STAC tools now surface a derived routing hint
     alongside them (the s3:// form + the `s3_endpoint`/`s3_scope` to pass to
     `query`), so carry-the-links works without pre-configuration.
  Composition by **linking whole catalogs** (catalog-of-catalogs) is separately
  blocked by the catalog-walk limits in
  [#263](https://github.com/boettiger-lab/mcp-data-server/issues/263); composition
  by **listing collections** (the geo-agent pattern) works today.

## Does this trip up existing workflows?

Largely no. The dominant flows (query, schema-via-inline) are
whole-catalog-independent, so swapping/mixing sources doesn't break them —
*provided query has a secret for each endpoint*. `browse` + `catalog://` +
default-resolution depend on a reachable whole catalog (empty during a Ceph
outage under degraded start; restored by swapping `STAC_CATALOG_URL`); these are
low-volume and optional. The one historical hard dependency (startup fail-fast)
is already addressed.

## Design north star

Lean into "carry the links" as the primary contract; keep the whole-catalog a
thin, swappable discovery/default. Data-driven routing shipped with #264: the
`s3config.py` source registry drives the query/tile secrets, the href rewrite,
and the STAC-metadata reroute, with per-request hints covering unregistered
hosts. Remaining: a recursive type-agnostic walk if catalog-of-catalogs
composition is ever wanted (#263), and a structured `s3_sources` query parameter
for multi-endpoint bring-your-own requests. The fully STAC-native end state
carries endpoint/routing on the asset (storage extension) rather than in
server-side tables.
