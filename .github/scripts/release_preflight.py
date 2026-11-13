#!/usr/bin/env python3
"""Reject an occupied release version before starting the private build jobs."""

import argparse
import os
import re
import subprocess


TIMEOUT_SECONDS = 20
MAX_HEADER_BYTES = 64 * 1024


class PreflightError(ValueError):
    pass


def response_status(result):
    # gh --silent excludes the body. Never print captured output: even API
    # failures can include server diagnostics that do not belong in build logs.
    output = result.stdout
    if not isinstance(output, str) or len(output) > MAX_HEADER_BYTES:
        raise PreflightError("RELEASE_PREFLIGHT_INVALID_RESPONSE")
    lines = output.splitlines()
    match = re.fullmatch(r"HTTP/(?:1\.[01]|2(?:\.0)?|3(?:\.0)?) ([1-5][0-9]{2})(?: [^\r\n]*)?",
                         lines[0] if lines else "")
    if not match or any(line.startswith("HTTP/") for line in lines[1:]):
        raise PreflightError("RELEASE_PREFLIGHT_INVALID_RESPONSE")
    return int(match[1])


def api_status(route, run):
    try:
        result = run([
            "gh", "api", "--hostname", "github.com", "--method", "GET",
            "--include", "--silent", "--header", "Accept: application/vnd.github+json",
            "--header", "X-GitHub-Api-Version: 2022-11-28",
            "--header", "Cache-Control: no-cache", route,
        ], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise PreflightError("RELEASE_PREFLIGHT_LOOKUP_TIMEOUT") from None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise PreflightError("RELEASE_PREFLIGHT_LOOKUP_FAILED") from None
    status = response_status(result)
    # gh returns 1 for an HTTP error, not zero. An interrupted command or any
    # contradictory return status is not proof that a release does not exist.
    if (status == 200 and result.returncode == 0) or (status == 404 and result.returncode == 1):
        return status
    raise PreflightError("RELEASE_PREFLIGHT_LOOKUP_FAILED")


def check_version(repository, tag, platform, *, run=subprocess.run):
    if (not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository)
            or repository.split("/")[1] in (".", "..")):
        raise PreflightError("RELEASE_PREFLIGHT_INVALID_REPOSITORY")
    if (not isinstance(tag, str) or len(tag) > 128
            or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+-(?:pre|alpha|beta|rc)\.[0-9]+", tag)):
        raise PreflightError("RELEASE_PREFLIGHT_INVALID_VERSION")
    if platform not in ("both", "windows", "linux"):
        raise PreflightError("RELEASE_PREFLIGHT_INVALID_PLATFORM")
    if platform != "both":
        return "RELEASE_PREFLIGHT_SINGLE_PLATFORM_TEST"
    route = f"repos/{repository}/releases"
    if api_status(f"{route}/tags/{tag}", run) == 200:
        # An existing draft also occupies the version: the publisher creates a
        # new staging release and never overwrites an existing release.
        raise PreflightError("RELEASE_VERSION_ALREADY_EXISTS")
    # A 404 can also hide a repository or a permission problem. Confirm that
    # this token can actually read this repository's release API before using
    # a missing-tag response to permit the expensive build.
    if api_status(f"{route}?per_page=1", run) != 200:
        raise PreflightError("RELEASE_PREFLIGHT_LOOKUP_FAILED")
    return "RELEASE_VERSION_AVAILABLE"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--platform", choices=("both", "windows", "linux"), required=True)
    args = parser.parse_args(argv)
    try:
        print(check_version(args.repository, args.tag, args.platform))
    except PreflightError as error:
        print(str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
