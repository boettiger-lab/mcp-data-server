"""Deployment-level S3 configuration: the default backend and the source registry.

Single source of truth (#268/#271/#264) for everything that maps STAC hrefs to
DuckDB S3 access, consumed by the query engine (server.get_isolated_db), the
tile subsystem (tiles.db.build_tile_connection), and the STAC renderer (stac.py):

- **Default backend** — one env surface (S3_DEFAULT_ENDPOINT / S3_DEFAULT_URL_STYLE /
  S3_DEFAULT_USE_SSL) for the unscoped `s3` secret, so repointing a deployment at
  another backend (MinIO, source.coop, ...) covers query AND hex tiles.

- **Source registry** — data-driven routing for every *known* non-default source
  (#264). Each entry can carry:
    name                 registry key; sanitized into the DuckDB secret name
    https_prefix         hrefs starting with this are rewritten to s3_prefix
    s3_prefix            replacement prefix (s3:// form DuckDB can glob)
    metadata_url_prefix  when set, STAC-JSON fetches for https_prefix are rerouted
                         here instead (e.g. the NRP in-cluster fast path)
    secret               when set, a prefix-scoped DuckDB secret is created on
                         every connection: {endpoint, scope, region?, url_style?,
                         use_ssl?, key_id?, secret?}
  Built-ins cover the NRP Ceph endpoint (rewrite + in-cluster metadata reroute;
  no secret of its own — its paths route via the default `s3` secret) and the
  anonymous source.coop mirror (gateway rewrite + scoped `source_coop` secret).

  Deployments extend/override via the S3_SOURCES env var: a JSON list of entries,
  merged over the built-ins by name ("disabled": true removes one). Example:
    S3_SOURCES='[{"name": "campus_minio",
                  "https_prefix": "https://minio.example.edu/",
                  "s3_prefix": "s3://",
                  "secret": {"endpoint": "minio.example.edu",
                             "scope": "s3://mirror-"}}]'

- **Route hints** — for hrefs on hosts NOT in the registry, `route_hint()` derives
  the (s3 path, endpoint, scope) a caller can pass per-request to `query`
  (s3_endpoint/s3_scope), so "carry the links" works for endpoints nobody
  pre-configured. Derivation, never a rewrite: unknown hrefs still pass through
  unchanged; the hint is surfaced alongside them by the STAC tools.

The fully STAC-native end state carries endpoint/routing on the asset itself
(storage extension); this registry is the server-side stand-in until then.
"""
import json
import os
import re
import sys


def sql_quote(value: str) -> str:
    """Escape a value for embedding in a single-quoted SQL string literal.

    Secret parameters are interpolated into CREATE SECRET statements; a stray
    quote would otherwise break the statement — and the resulting parser error
    can echo the statement (credentials included) back to the caller (#271).
    """
    return (value or "").replace("'", "''")


def default_endpoint() -> str:
    """The deployment's default S3 endpoint (bare host, no scheme).

    Unset = the NRP Ceph internal endpoint (back-compat)."""
    return os.environ.get("S3_DEFAULT_ENDPOINT", "rook-ceph-rgw-nautiluss3.rook")


def infer_use_ssl(endpoint: str) -> str:
    """In-cluster rook endpoints are plain http; everything else defaults to SSL."""
    return "false" if endpoint.startswith("rook") else "true"


def default_s3_secret_sql(name: str = "s3") -> str:
    """CREATE SECRET statement for the deployment's default (anonymous) S3 backend.

    Env is read at call time, not import time, so per-request connections pick
    up changes without a restart and tests can monkeypatch. USE_SSL is inferred
    from the endpoint unless S3_DEFAULT_USE_SSL overrides it.
    """
    endpoint = default_endpoint()
    url_style = os.environ.get("S3_DEFAULT_URL_STYLE", "path")
    use_ssl = (
        os.environ.get("S3_DEFAULT_USE_SSL") or infer_use_ssl(endpoint)
    ).strip().lower()
    return (
        f"CREATE OR REPLACE SECRET {name} ("
        f"TYPE S3, KEY_ID '', SECRET '', "
        f"ENDPOINT '{sql_quote(endpoint)}', URL_STYLE '{sql_quote(url_style)}', "
        f"USE_SSL '{sql_quote(use_ssl)}')"
    )


# -------------------------------------------------------------------------
# Source registry
# -------------------------------------------------------------------------
def _builtin_sources() -> list[dict]:
    # Computed per call (not module-level) so S3_ENDPOINT_URL is honored when it
    # changes between calls — e.g. running stac.py locally against the external
    # endpoint, or monkeypatching in tests.
    return [
        {
            # Primary NRP Ceph. The STAC catalog publishes external SSL hrefs;
            # rewriting strips the host so DuckDB can glob, and paths then route
            # via the deployment-default `s3` secret (internal endpoint in-cluster)
            # — which is why this entry has no secret of its own. Metadata (STAC
            # JSON) fetches are invisibly rerouted to the in-cluster endpoint for
            # speed; override with S3_ENDPOINT_URL outside the cluster.
            "name": "nrp_ceph",
            "https_prefix": "https://s3-west.nrp-nautilus.io/",
            "s3_prefix": "s3://",
            "metadata_url_prefix": (
                os.environ.get("S3_ENDPOINT_URL", "http://rook-ceph-rgw-nautiluss3.rook").rstrip("/")
                + "/"
            ),
        },
        {
            # Anonymous source.coop mirror (#260). STAC entries publish assets
            # under the data.source.coop HTTPS gateway, which cannot list/glob;
            # the AWS-bucket form can. The bucket name is endpoint-unique, so the
            # prefix-scoped secret routes it unambiguously alongside Ceph paths.
            "name": "source_coop",
            "https_prefix": "https://data.source.coop/",
            "s3_prefix": "s3://us-west-2.opendata.source.coop/",
            "secret": {
                "endpoint": "s3.us-west-2.amazonaws.com",
                "region": "us-west-2",
                "scope": "s3://us-west-2.opendata.source.coop",
            },
        },
    ]


def get_sources() -> list[dict]:
    """The active source registry: built-ins merged with the S3_SOURCES env JSON.

    Env entries merge over built-ins by name (top-level keys replace; new names
    append; {"disabled": true} removes). A malformed S3_SOURCES is reported and
    ignored rather than taking the server down. Parsed per call — the inputs are
    tiny and this keeps env changes and tests cheap and correct.
    """
    sources = {s["name"]: s for s in _builtin_sources()}
    raw = os.environ.get("S3_SOURCES", "").strip()
    if raw:
        try:
            for entry in json.loads(raw):
                name = entry.get("name")
                if not name:
                    print(f"⚠️ S3_SOURCES entry without a name skipped: {entry!r}", file=sys.stderr)
                    continue
                if entry.get("disabled"):
                    sources.pop(name, None)
                    continue
                sources[name] = {**sources.get(name, {}), **entry}
        except Exception as e:
            print(f"⚠️ S3_SOURCES is not valid JSON — ignored: {e}", file=sys.stderr)
    return list(sources.values())


def rewrite_href(href: str) -> str:
    """Registry-driven https→s3 prefix rewrite for data access (read_parquet).

    Returns the href unchanged when no source matches — unknown hosts pass
    through, they are never guessed at (see route_hint for the advisory path).
    """
    for src in get_sources():
        hp, sp = src.get("https_prefix"), src.get("s3_prefix")
        if hp and sp and href.startswith(hp):
            return sp + href[len(hp):]
    return href


def metadata_href(href: str) -> str:
    """Registry-driven reroute for STAC-metadata (JSON) fetches.

    Distinct from rewrite_href: data reads want s3:// paths for DuckDB, while
    metadata reads stay HTTP — just pointed at a faster/internal host when the
    registry knows one (NRP in-cluster fast path).
    """
    for src in get_sources():
        hp, mp = src.get("https_prefix"), src.get("metadata_url_prefix")
        if hp and mp and href.startswith(hp):
            return mp + href[len(hp):]
    return href


# Virtual-hosted AWS S3: BUCKET.s3.amazonaws.com or BUCKET.s3.REGION.amazonaws.com.
_VHOST_S3 = re.compile(r"^(?P<bucket>[^/]+?)\.(?P<endpoint>s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com)$")


def route_hint(href: str) -> dict | None:
    """Derive per-request routing for an https href on a host outside the registry.

    Assumes path-style S3 (https://HOST/BUCKET/KEY → s3://BUCKET/KEY @ HOST),
    plus the documented virtual-hosted AWS form. Returns
    {"path", "endpoint", "scope"} — the s3:// path and the s3_endpoint/s3_scope
    a caller passes to `query` — or None when the href isn't https or has no
    bucket/key to split. A hint, not a rewrite: callers surface it alongside the
    original href, they don't substitute silently.
    """
    m = re.match(r"^https://([^/]+)/(.+)$", href)
    if not m:
        return None
    host, rest = m.groups()
    vhost = _VHOST_S3.match(host)
    if vhost:
        bucket, key = vhost.group("bucket"), rest
        endpoint = vhost.group("endpoint")
    else:
        bucket, _, key = rest.partition("/")
        endpoint = host
    if not bucket or not key:
        return None
    return {
        "path": f"s3://{bucket}/{key}",
        "endpoint": endpoint,
        "scope": f"s3://{bucket}",
    }


def _secret_identifier(name: str) -> str:
    """Sanitize a registry name into a DuckDB identifier for CREATE SECRET."""
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return ident if not ident[:1].isdigit() else f"s_{ident}"


def source_secret_sql() -> list[str]:
    """CREATE SECRET statements for every registry source that declares one.

    All values are quote-escaped; USE_SSL is inferred from the endpoint unless
    the entry says otherwise. Run on every DuckDB connection (query and tiles)
    so scoped routing is identical everywhere.
    """
    stmts = []
    for src in get_sources():
        sec = src.get("secret")
        if not sec or not sec.get("endpoint"):
            continue
        use_ssl = str(sec.get("use_ssl", infer_use_ssl(sec["endpoint"]))).strip().lower()
        parts = [
            "TYPE S3",
            f"KEY_ID '{sql_quote(sec.get('key_id', ''))}'",
            f"SECRET '{sql_quote(sec.get('secret', ''))}'",
            f"ENDPOINT '{sql_quote(sec['endpoint'])}'",
            f"URL_STYLE '{sql_quote(sec.get('url_style', 'path'))}'",
            f"USE_SSL '{sql_quote(use_ssl)}'",
        ]
        if sec.get("region"):
            parts.append(f"REGION '{sql_quote(sec['region'])}'")
        if sec.get("scope"):
            parts.append(f"SCOPE '{sql_quote(sec['scope'])}'")
        stmts.append(
            f"CREATE OR REPLACE SECRET {_secret_identifier(src['name'])} ("
            + ", ".join(parts)
            + ")"
        )
    return stmts
