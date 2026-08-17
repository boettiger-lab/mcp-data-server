#!/usr/bin/env python3
"""Stage 4 of #384: gate the guidance files so provenance and size can't re-accrete.

Three checks, each guarding a failure this repo has actually shipped:

1. **Every section carries a well-formed `prov` line.** `h3-guide.md` and
   `query-optimization.md` are injected verbatim into the `query` tool description on
   every call, so a rule with no recorded motivation is unvalidated weight nobody can
   later argue about. The audit that produced this gate found eight sections whose
   provenance had to be reconstructed from closed issues and git blame; that is the cost
   of letting one merge without it. `q-opt §9` also shipped *post-policy* with no cell, so
   the discipline was leaking in the present, not only the past.

2. **A `tier=extra` demotion is spelled correctly.** `select_tiers()` treats an
   unrecognised tier as `core` (the safe default), which means a typo — `tier=exta` — is
   silently a no-op rather than an error. The flag would appear to do nothing and the
   section would keep shipping to prod.

3. **The assembled `query` description stays under a committed ceiling.** #293/#384 exist
   because it grew unnoticed. The ceiling is a ratchet, not a target: raising it is a
   one-line diff a reviewer can see and question, which is the whole point.

4. **A deferred guidance-SQL exemption names a tracking issue.** #394 is the case: the
   AOI-clip recipe sat in the harness's `FRAGMENTS` list as "those AOI geoparquets are not
   mapped" — an honest deferral — and shipped with the wrong geometry column name and
   degrees divided by 1609.344. Binding alone would have caught the first defect on day
   one. Two guards were nominally over that block and both had been opted out of, so **an
   exemption with a good reason is indistinguishable from coverage until someone checks.**
   Entries that can never bind (a bare clause, a CTE over an undefined relation) are not
   debt and are left alone; a *deferral* is, and must carry a `#NNN`.

Deliberately NOT checked: `cell=none`. It is the audit *signal*, not a violation — the
consumer sweep behind #384 found that of 5,524 ch flagged `cell=none`, only ~683 ch was a
clean demotion candidate and the rest was load-bearing but unguarded. A CI rule banning
`cell=none` would push authors to invent a cell reference rather than write a cell.

Runs in CI on any guidance change, and locally:
    python tests/check_guidance_prov.py
"""
import os
import re
import sys

GUIDES = ("h3-guide.md", "query-optimization.md")
REQUIRED_FIELDS = ("issue", "models", "added", "cell", "tier")
VALID_TIERS = ("core", "extra")

# The ceiling is the ratchet. Set from the measured v0.8.19 payload (40,476 ch) with ~5%
# headroom so ordinary wording fixes don't trip it, but a new section does. Raising this
# should come with a sentence in the PR saying what earned the space.
QUERY_DESC_CEILING = 42_500

HEADING = re.compile(r"^(#{2,6})[ \t]+(\S.*)$")
PROV = re.compile(r"^[ \t]*<!--[ \t]*prov:(.*?)-->[ \t]*$")
FIELD = re.compile(r"\b([a-z_]+)=(\S+)")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _prov_for_heading(lines, i):
    """The prov payload for the heading at `lines[i]`, or None if the next content line
    is not a prov comment. Only the first non-blank line is considered, so provenance
    cannot drift away from the heading it describes."""
    for line in lines[i + 1:]:
        if not line.strip():
            continue
        m = PROV.match(line)
        return m.group(1).strip() if m else None
    return None


def check_prov(violations):
    sections = 0
    for name in GUIDES:
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            violations.append(f"{name}: missing — guidance file expected at repo root")
            continue
        lines = open(path).read().splitlines()
        for i, line in enumerate(lines):
            m = HEADING.match(line)
            if not m:
                continue
            sections += 1
            title = m.group(2).strip()
            prov = _prov_for_heading(lines, i)
            if prov is None:
                violations.append(
                    f"{name}:{i + 1}: section '{title[:60]}' has no `prov` line. Add "
                    "`<!-- prov: issue=#N models=<which models failed> added=YYYY-MM-DD "
                    "cell=<benchmark cell|none> tier=core|extra -->` directly under the heading.")
                continue
            fields = dict(FIELD.findall(prov))
            missing = [f for f in REQUIRED_FIELDS if f not in fields]
            if missing:
                violations.append(
                    f"{name}:{i + 1}: section '{title[:50]}' prov is missing "
                    f"{', '.join(missing)}")
            tier = fields.get("tier")
            if tier is not None and tier not in VALID_TIERS:
                # Silently a no-op otherwise: select_tiers() defaults anything it doesn't
                # recognise to `core`, so a typo'd demotion still ships to prod.
                violations.append(
                    f"{name}:{i + 1}: section '{title[:50]}' has tier={tier!r}; "
                    f"expected one of {VALID_TIERS}. An unrecognised tier is treated as "
                    "`core`, so a typo silently un-does the demotion.")
    return sections


def check_budget(violations):
    """Assemble the description the way the server does and hold it under the ceiling."""
    sys.path.insert(0, ROOT)
    try:
        import server  # noqa: F401  (import side-effect: builds the description)
    except Exception as e:  # pragma: no cover - import failure is its own signal
        violations.append(f"could not import server.py to measure the description: {e}")
        return None
    try:
        desc = server.mcp._tool_manager._tools["query"].description
    except Exception as e:
        violations.append(f"could not read the assembled `query` description: {e}")
        return None
    if len(desc) > QUERY_DESC_CEILING:
        violations.append(
            f"assembled `query` description is {len(desc):,} ch, over the "
            f"{QUERY_DESC_CEILING:,} ch ceiling by {len(desc) - QUERY_DESC_CEILING:,}. "
            "Either tighten the guidance or raise QUERY_DESC_CEILING in this file and say "
            "in the PR what earned the space.")
    return len(desc)


# Language that marks a FRAGMENTS entry as *deferred* rather than *not a statement*. A
# deferral is debt: the SQL is real and runnable, and only the placeholder mappings are
# missing. Those must name an issue so the exemption has an owner (#402).
DEFERRAL_PHRASES = ("not yet mapped", "not mapped", "todo", "for now", "pending")
ISSUE_REF = re.compile(r"#\d+")


def check_deferred_fragments(violations):
    fixture = os.path.join(ROOT, "tests", "guidance_sql", "fixture.py")
    if not os.path.exists(fixture):
        return 0
    sys.path.insert(0, os.path.dirname(fixture))
    try:
        import fixture as fx
    except Exception as e:  # pragma: no cover
        violations.append(f"could not import the guidance-SQL fixture: {e}")
        return 0
    deferred = 0
    for key, reason in sorted(getattr(fx, "FRAGMENTS", {}).items()):
        low = str(reason).lower()
        if not any(p in low for p in DEFERRAL_PHRASES):
            continue  # can never bind — not debt, exempt forever
        deferred += 1
        if not ISSUE_REF.search(str(reason)):
            violations.append(
                f"guidance_sql/fixture.py: FRAGMENTS[{key!r}] is a deferral "
                f"({reason!s:.60}…) with no issue reference. A deferred exemption is debt: "
                "either promote it to EXECUTABLE with mappings, or cite the issue tracking "
                "it (#402) so it has an owner.")
    return deferred


def main():
    violations = []
    sections = check_prov(violations)
    deferred = check_deferred_fragments(violations)
    size = check_budget(violations)
    if violations:
        print(f"❌ guidance gate: {len(violations)} problem(s)", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    headroom = QUERY_DESC_CEILING - size if size is not None else 0
    print(f"✅ guidance gate: {sections} sections all carry a well-formed prov line; "
          f"{deferred} deferred SQL exemption(s) all cite an issue; "
          f"`query` description {size:,} ch of {QUERY_DESC_CEILING:,} ceiling "
          f"({headroom:,} ch headroom)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
