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

Second check (#413): a Deployment pinned to a `:vX.Y.Z` tag must also set an
`APP_VERSION` env var equal to that tag. Release identity is now supplied at deploy
time rather than baked into the image — a release adds a tag to the *existing* `main`
manifest instead of rebuilding, so the image's own `ENV APP_VERSION` still reads
"main" and the Deployment must override it. Two places now have to agree, so this
checks they do; otherwise `/version` would quietly report the wrong release and the
audit trail this whole scheme exists for would be worthless. Files pinned to a moving
tag (`:main`, dev) are exempt — they have no release identity to state.

Assumes one Deployment of this repo's image per k8s/*.yaml file, which holds today
and keeps this check dependency-free.

Runs in CI on any k8s/*.yaml change, and locally: `python tests/check_k8s_image_pins.py`.
"""
import glob
import os
import re
import sys

REPO_IMAGE = "ghcr.io/boettiger-lab/mcp-data-server"
IMAGE_LINE = re.compile(r"^\s*image:\s*(\S+)")
ALLOW_MUTABLE = "ci-image-pin: allow-mutable"
VERSION_TAG = re.compile(r":(v\d+\.\d+\.\d+)@sha256:")
APP_VERSION_ENV = re.compile(
    r"^\s*-\s*name:\s*APP_VERSION\s*$\n\s*value:\s*[\"']?(\S+?)[\"']?\s*$", re.M
)


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    violations = []
    checked = allowed = 0
    stamped = 0
    for path in sorted(glob.glob(os.path.join(root, "k8s", "*.yaml"))):
        text = open(path).read()
        lines = text.splitlines()
        rel = os.path.relpath(path, root)
        # #413: a release-pinned Deployment must state the same version in APP_VERSION.
        for tag in set(VERSION_TAG.findall(text)):
            env = APP_VERSION_ENV.search(text)
            if env is None:
                violations.append(
                    f"{rel}: pinned to {tag} but sets no APP_VERSION env — /version would "
                    f"report the image's own stamp ('main' after a retag), not the release"
                )
            elif env.group(1) != tag:
                violations.append(
                    f"{rel}: image tag {tag} != APP_VERSION {env.group(1)} — /version would "
                    f"misreport the running release"
                )
            else:
                stamped += 1
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
            violations.append(f"{rel}:{i + 1}: not digest-pinned -> {ref}")

    if violations:
        print("k8s image-pin check FAILED (see AGENTS.md rollout; #366 digest pinning, #413 release stamp):")
        for v in violations:
            print(f"  ✗ {v}")
        return 1
    print(
        f"k8s image-pin check OK — {checked} repo image reference(s): "
        f"{checked - allowed} digest-pinned, {allowed} explicit allow-mutable; "
        f"{stamped} release pin(s) with a matching APP_VERSION"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
