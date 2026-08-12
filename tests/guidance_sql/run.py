#!/usr/bin/env python3
"""Execute the SQL examples embedded in the guidance files, in CI (#369).

`h3-guide.md`, `query-optimization.md` and `query-setup.md` are runtime prompt
artifacts: `server.py` injects them verbatim into the `query` tool description.
An example that no longer runs is a defect in a shipped artifact — a moved column
(#364's `nland`), a renamed function, a path that no longer resolves — and the
model gate cannot see it (a model that can route around a broken example does,
so the gate goes green either way). This runs the examples so a broken one fails
CI before merge.

## How a block is handled

Every ```sql block in the three guides must be **classified** in `fixture.py`,
keyed by a hash of its text:

  - EXECUTABLE — a placeholder->value map. Every `<placeholder>` in the block
    must be mapped; the block is substituted and **run**. An unmapped
    placeholder is a failure, not a skip (else the check quietly passes
    everything as examples drift). Execution error is a failure.
  - FRAGMENT — an illustrative snippet that is not a standalone statement
    (a bare JOIN/WHERE clause, a CTE referencing an undefined relation, a COPY
    to a write path). Declared with a one-line reason, visible in review.

A block that is in **neither** map fails: you cannot add an example to a guide
without either mapping it (so CI runs it) or explicitly declaring it a fragment.
A fixture entry whose key matches no current block also fails (stale mapping) —
so editing a block forces re-classification rather than silently keeping the old
verdict.

Reads go to the public source.coop mirror / the public Ceph external endpoint —
no secrets, no cluster access (same posture as examples.yml). Placeholder paths
are mapped to a single `h0=` partition so a block binds every path and column
and executes cheaply, without a full-catalog scan.

Usage:
    python tests/guidance_sql/run.py            # classify + execute; non-zero on any failure
    python tests/guidance_sql/run.py --list     # print each block's key, first line, placeholders
"""
import hashlib
import os
import re
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
GUIDES = ["h3-guide.md", "query-optimization.md", "query-setup.md"]
PLACEHOLDER_RE = re.compile(r"<[^>\n]+>")
SQL_BLOCK_RE = re.compile(r"```sql\n(.*?)\n```", re.DOTALL)


def block_key(filename, text):
    """Stable id: filename + hash of the block's text. Changes when the block
    is edited, so a stale fixture verdict cannot ride along."""
    h = hashlib.sha1(text.strip().encode()).hexdigest()[:10]
    return f"{filename}:{h}"


def extract_blocks():
    blocks = []
    for fn in GUIDES:
        txt = (REPO / fn).read_text()
        for text in SQL_BLOCK_RE.findall(txt):
            blocks.append((fn, block_key(fn, text), text))
    return blocks


def substitute(text, mapping):
    for ph, val in mapping.items():
        text = text.replace(ph, val)
    return text


def make_connection():
    import duckdb

    con = duckdb.connect()
    for ext, src in (("httpfs", ""), ("spatial", ""), ("h3", " FROM community")):
        try:
            con.sql(f"INSTALL {ext}{src}")
        except Exception:
            pass  # already installed in the image / runner extension dir
        con.sql(f"LOAD {ext}")
    # Anonymous, prefix-scoped reads. The mirror is fast and public; the Ceph
    # external endpoint is the fallback for datasets not mirrored. Neither needs
    # a credential — the fixture only maps to public paths.
    con.sql(
        "CREATE SECRET scoop (TYPE S3, KEY_ID '', SECRET '', "
        "ENDPOINT 's3.us-west-2.amazonaws.com', REGION 'us-west-2', "
        "URL_STYLE 'path', USE_SSL 'true', "
        "SCOPE 's3://us-west-2.opendata.source.coop')"
    )
    con.sql(
        "CREATE SECRET ceph_ext (TYPE S3, ENDPOINT 's3-west.nrp-nautilus.io', "
        "URL_STYLE 'path', USE_SSL 'true', SCOPE 's3://public-')"
    )
    con.sql("SET THREADS=4")  # external endpoint is happier at low concurrency
    # Mirror the server preamble (server.py): a DuckDB statistics_propagation bug
    # (#378) crashes SEMI/INNER joins over some S3 hex parquet (nhd-flowline, ACE)
    # with `INTERNAL Error: SetMin or SetMax ...`. The server disables this optimizer
    # per connection; the harness must too, so CI executes the same plan the server
    # runs (else a real, server-runnable example would fail here, or vice versa).
    con.sql("SET disabled_optimizers='statistics_propagation'")
    return con


def cmd_list():
    from fixture import EXECUTABLE, FRAGMENTS

    for fn, key, text in extract_blocks():
        first = next((l for l in text.strip().splitlines() if l.strip()), "")
        phs = sorted(set(PLACEHOLDER_RE.findall(text)))
        state = "EXEC" if key in EXECUTABLE else "FRAG" if key in FRAGMENTS else "UNCLASSIFIED"
        print(f"{state:12} {key}")
        print(f"             {first[:88]}")
        if phs:
            print(f"             placeholders: {phs}")


def main():
    sys.path.insert(0, str(HERE))
    from fixture import EXECUTABLE, FRAGMENTS

    if "--list" in sys.argv:
        cmd_list()
        return 0

    blocks = extract_blocks()
    seen_keys = {key for _, key, _ in blocks}

    # Stale fixture entries (a key that no longer matches any block) are a failure:
    # a block was edited or removed and its verdict wasn't revisited.
    stale = (set(EXECUTABLE) | set(FRAGMENTS)) - seen_keys
    failures = []
    for key in sorted(stale):
        failures.append(f"STALE fixture entry {key} matches no current block — re-classify or remove it")

    con = None
    executed = fragments = 0
    for fn, key, text in blocks:
        if key in FRAGMENTS:
            fragments += 1
            continue
        if key not in EXECUTABLE:
            first = next((l for l in text.strip().splitlines() if l.strip()), "")
            failures.append(
                f"UNCLASSIFIED block {key} ({first[:70]!r}) — add to fixture.py as "
                f"EXECUTABLE (with placeholder mappings) or FRAGMENT (with a reason)"
            )
            continue
        sql = substitute(text, EXECUTABLE[key])
        left = sorted(set(PLACEHOLDER_RE.findall(sql)))
        if left:
            failures.append(f"UNMAPPED placeholder(s) in {key}: {left}")
            continue
        if con is None:
            con = make_connection()
        try:
            con.sql(sql)
            executed += 1
            print(f"  ok   {key}")
        except Exception as e:
            failures.append(f"EXECUTION FAILED {key}: {str(e).splitlines()[0][:200]}")
            print(f"  FAIL {key}: {str(e).splitlines()[0][:120]}")

    print(f"\n{executed} executed, {fragments} fragments, {len(failures)} failures")
    for f in failures:
        print(f"  ✗ {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
