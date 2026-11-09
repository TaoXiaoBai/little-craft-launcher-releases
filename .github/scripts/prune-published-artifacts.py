#!/usr/bin/env python3
"""Remove temporary package artifacts only after a complete public prerelease."""

import argparse
import json
import os
import re
import subprocess
import sys


ARTIFACT_PLATFORMS = {
    "release-windows-x64": "windows-x64",
    "release-linux-x86_64": "linux-x86_64",
    "release-qt-dependency-sources": "windows-x64",
}
QT_SOURCE_ASSETS = {
    f"{module}-everywhere-src-6.8.3.zip"
    for module in ("qtbase", "qtdeclarative", "qtsvg", "qtimageformats")
}


def github_api(path, method="GET"):
    result = subprocess.run(
        ["gh", "api", "--method", method, path],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def paginated(api, path, key=None):
    items = []
    page = 1
    while True:
        data = api(f"{path}?per_page=100&page={page}")
        batch = data[key] if key else data
        if not isinstance(batch, list):
            raise ValueError("Unexpected API inventory")
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def positive_id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("Invalid API identity")
    return str(value)


def prune(repository, tag, run_ids, api=github_api, dry_run=False):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid public repository")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+-(pre|alpha|beta|rc)\.[0-9]+", tag):
        raise ValueError("Invalid prerelease identity")
    run_ids = list(dict.fromkeys(positive_id(value) for value in run_ids))
    if not run_ids:
        raise ValueError("No source runs supplied")
    prefix = f"repos/{repository}"
    metadata = api(prefix)
    if (metadata.get("full_name", "").casefold() != repository.casefold()
            or metadata.get("private") is not False):
        raise ValueError("Repository identity changed")
    branch = metadata.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise ValueError("Missing default branch")

    release = api(f"{prefix}/releases/tags/{tag}")
    if (release.get("tag_name") != tag or release.get("draft") is not False
            or release.get("prerelease") is not True):
        raise ValueError("A public prerelease is required")
    release_id = positive_id(release.get("id"))
    assets = paginated(api, f"{prefix}/releases/{release_id}/assets")
    expected = {
        f"LittleCraftLauncher-{tag}-windows-x64.zip",
        f"LittleCraftLauncher-{tag}-linux-x86_64.tar.gz",
        "SHA256SUMS.txt", *QT_SOURCE_ASSETS,
    }
    if (len(assets) != 7 or {asset.get("name") for asset in assets} != expected
            or any(asset.get("state") != "uploaded"
                   or not isinstance(asset.get("size"), int)
                   or isinstance(asset.get("size"), bool) or asset["size"] <= 0
                   for asset in assets)):
        raise ValueError("The published prerelease is incomplete")

    # Validate every supplied run before deleting anything. Overall run status
    # may still be in_progress while its successful build jobs are publishing.
    deletions = set()
    for run_id in run_ids:
        run_prefix = f"{prefix}/actions/runs/{run_id}"
        run = api(run_prefix)
        for key in ("repository", "head_repository"):
            if (run.get(key) or {}).get("full_name", "").casefold() != repository.casefold():
                raise ValueError("Foreign source run")
        if (positive_id(run.get("id")) != run_id
                or run.get("event") != "workflow_dispatch"
                or run.get("head_branch") != branch
                or run.get("path") != ".github/workflows/prerelease.yml"):
            raise ValueError("Unexpected source workflow")
        title = run.get("display_title", "")
        title_prefix = f"Prerelease {tag}"
        if title != title_prefix and not title.startswith(title_prefix + " "):
            raise ValueError("Source run identifies another prerelease")
        workflow_id = positive_id(run.get("workflow_id"))
        workflow = api(f"{prefix}/actions/workflows/{workflow_id}")
        if workflow.get("path") != ".github/workflows/prerelease.yml":
            raise ValueError("Unexpected source workflow")
        jobs = paginated(api, f"{run_prefix}/jobs", "jobs")
        successful = set()
        for platform in set(ARTIFACT_PLATFORMS.values()):
            matches = [job for job in jobs if job.get("name") == f"Build {platform}"]
            if (len(matches) == 1 and matches[0].get("status") == "completed"
                    and matches[0].get("conclusion") == "success"):
                successful.add(platform)
        artifacts = paginated(api, f"{run_prefix}/artifacts", "artifacts")
        for name, platform in ARTIFACT_PLATFORMS.items():
            if platform not in successful:
                continue
            matches = [item for item in artifacts if item.get("name") == name and not item.get("expired")]
            if len(matches) > 1:
                raise ValueError("Ambiguous source artifact")
            if matches:
                deletions.add(positive_id(matches[0].get("id")))

    if not dry_run:
        for artifact_id in sorted(deletions, key=int):
            api(f"{prefix}/actions/artifacts/{artifact_id}", method="DELETE")
    return len(deletions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"), required="GITHUB_REPOSITORY" not in os.environ)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        count = prune(args.repository, args.tag, args.run_id, dry_run=args.dry_run)
    except Exception:
        # Raw API/CLI errors can contain credentials or private diagnostics.
        print("Temporary artifact cleanup was skipped or incomplete; retained artifacts expire after three days.", file=sys.stderr)
        return 1
    verb = "Verified" if args.dry_run else "Removed"
    print(f"{verb} {count} temporary published-package artifacts; diagnostics were retained.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
