#!/usr/bin/env python3
"""Prepare hash-verified public Linux deployment tools and Qt license inputs."""

import argparse
from pathlib import Path
import sys

from linux_toolchain import fetch_tools, prepare_qt_sources, verify_tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.verify_only:
            verify_tools(args.tool_dir)
        else:
            fetch_tools(args.tool_dir)
            if args.source_root:
                prepare_qt_sources(args.source_root)
    except Exception:
        print("Pinned Linux deployment inputs failed verification.", file=sys.stderr)
        return 1
    print("Pinned Linux deployment inputs verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
