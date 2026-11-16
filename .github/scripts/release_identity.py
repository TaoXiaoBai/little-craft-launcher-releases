#!/usr/bin/env python3
"""The release identity a build is configured with (CMake MCL_RELEASE_IDENTITY).

The launcher compares this identity, stage and number included, with the
public release list when the user checks for updates. Published releases are
tagged vX.Y.Z-(pre|alpha|beta|rc).N (release_preflight enforces that for every
two-platform release), and such a tag is passed to CMake unchanged.

Any other label, such as a single-platform test run named after what it checks,
configures an empty identity. The launcher then says it cannot tell whether it
is current, instead of the build failing on CMake's identity check.

Every build of one run (Linux and Windows, primary and companion) must derive
the identity here from the same tag. The CI test proof binds every MCL_* cache
value, so a primary and a companion built with different identities cannot be
combined into one platform proof.
"""

import re
import sys

# Exactly what release_preflight admits for a release, so a published build
# always carries its tag. Should a tag ever exceed what CMake accepts, the build
# then fails loudly at configure time instead of shipping without an identity.
RELEASE_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+-(?:pre|alpha|beta|rc)\.[0-9]+")
MAX_TAG = 128


def release_identity(tag):
    if isinstance(tag, str) and len(tag) <= MAX_TAG and RELEASE_TAG.fullmatch(tag):
        return tag
    return ""


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: release_identity.py TAG", file=sys.stderr)
        return 2
    sys.stdout.write(release_identity(arguments[0]) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
