#!/usr/bin/env python3
"""Fail if any of our Deployment images in k8s/*.yaml is not pinned by digest (#366).

A mutable tag (`:main`, `:vX.Y.Z`) resolves to a different build per pod at each
(re)start, so multi-replica pods silently drift onto different images — the
cross-pod skew dev's >=2 replicas exist to catch (#341). Every `image:` that
references this repo's image must carry an `@sha256:` digest; the tag prefix is
for humans, the digest is what's enforced.

A deployment that deliberately tracks a moving tag (e.g. a single-replica,
throwaway test endpoint where cross-pod skew cannot arise) opts out with a
`# ci-image-pin: allow-mutable — <reason>` comment on the image line or the line
directly above it. The exception is then greppable and justified in-tree.

Runs in CI on any k8s/*.yaml change, and locally: `python tests/check_k8s_image_pins.py`.
"""
import glob
import os
import re
import sys

REPO_IMAGE = "ghcr.io/boettiger-lab/mcp-data-server"
IMAGE_LINE = re.compile(r"^\s*image:\s*(\S+)")
ALLOW_MUTABLE = "ci-image-pin: allow-mutable"


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    violations = []
    checked = allowed = 0
    for path in sorted(glob.glob(os.path.join(root, "k8s", "*.yaml"))):
        lines = open(path).read().splitlines()
        for i, line in enumerate(lines):
            m = IMAGE_LINE.match(line)
            if not m:
                continue
            ref = m.group(1).strip().strip("'\"")
            if not ref.startswith(REPO_IMAGE):
                continue  # sidecars / other images are out of scope
            checked += 1
            if "@sha256:" in ref:
                continue
            # Opt-out marker on the image line or anywhere in the contiguous
            # comment block directly above it.
            window = [line]
            j = i - 1
            while j >= 0 and lines[j].lstrip().startswith("#"):
                window.append(lines[j])
                j -= 1
            if any(ALLOW_MUTABLE in w for w in window):
                allowed += 1
                continue
            rel = os.path.relpath(path, root)
            violations.append(f"{rel}:{i + 1}: not digest-pinned -> {ref}")

    if violations:
        print("k8s image-pin check FAILED — pin these by @sha256: (see AGENTS.md rollout, #366):")
        for v in violations:
            print(f"  ✗ {v}")
        return 1
    print(
        f"k8s image-pin check OK — {checked} repo image reference(s): "
        f"{checked - allowed} digest-pinned, {allowed} explicit allow-mutable"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
