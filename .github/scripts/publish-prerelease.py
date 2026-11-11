#!/usr/bin/env python3
"""Validate build artifacts and publish only the two user-facing runtime packages."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
import zipfile

from appimage_format import inspect_appimage
from release_support import (LEGACY_LINUX_TAGS, dependency_archives, release_notes,
                             runtime_names, verify_published_assets, windows_bundle_required)
from windows_bundle_format import inspect_windows_zip


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_path(name):
    value = PurePosixPath(name)
    if value.is_absolute() or ".." in value.parts or "\\" in name:
        raise ValueError("Unsafe runtime archive path")
    return value


def validate_inputs(root, tag):
    windows, linux = runtime_names(tag)
    qt_archives = dependency_archives(tag)
    entries = list(root.iterdir())
    expected = {windows, linux, *qt_archives}
    if (len(entries) != len(expected) or {path.name for path in entries} != expected
            or any(path.is_symlink() or not path.is_file() or path.stat().st_size == 0
                   for path in entries)):
        raise ValueError("Expected two runtime packages and their exact verified Qt dependency archives")
    for name, (size, digest) in qt_archives.items():
        path = root / name
        if path.stat().st_size != size or checksum(path) != digest:
            raise ValueError("A Qt dependency archive differs from its pinned upstream bytes")
    if windows_bundle_required(tag):
        inspect_windows_zip(root / windows, tag)
    else:
        # Historical downloads and metadata remain accepted byte-for-byte.
        with zipfile.ZipFile(root / windows) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Duplicate Windows runtime entries")
            for name in names:
                safe_path(name)
            manifest = json.loads(archive.read("runtime-manifest.json"))
            if (manifest.get("version") != tag or manifest.get("platform") != "windows-x64"
                    or manifest.get("linkage") != "dynamic"):
                raise ValueError("Windows runtime identity differs from this release")
            for name in ("mclauncher-qt.exe", "SUPPORTING_MATERIALS.zip"):
                info = archive.getinfo(name)
                if info.is_dir() or info.file_size == 0:
                    raise ValueError("Windows runtime package is incomplete")
    if tag not in LEGACY_LINUX_TAGS:
        inspect_appimage(root / linux, tag)
    else:
        validate_legacy_linux(root / linux, tag)
    return {name: checksum(root / name) for name in (windows, linux)}


def validate_legacy_linux(path, tag):
    linux_root = f"LittleCraftLauncher-{tag}-linux-x86_64"
    with tarfile.open(path, "r:gz") as archive:
        names = set()
        ordinary_files = set()
        for entry in archive:
            value = safe_path(entry.name)
            if not value.parts or value.parts[0] != linux_root or entry.name in names:
                raise ValueError("Linux runtime root differs from this release")
            names.add(entry.name)
            if entry.isfile() and entry.size > 0:
                ordinary_files.add(entry.name)
        for name in ("mclauncher-qt", "LittleCraftLauncher", "SUPPORTING_MATERIALS.zip"):
            if f"{linux_root}/{name}" not in ordinary_files:
                raise ValueError("Linux runtime package is incomplete")


def publish(repository, tag, root, target, changes=None, run=subprocess.run):
    hashes = validate_inputs(root, tag)
    body = release_notes(repository, tag, hashes, changes)
    with tempfile.TemporaryDirectory(prefix="little-craft-release-") as temporary:
        notes = Path(temporary) / "release-notes.md"
        notes.write_text(body, encoding="utf-8")
        run(["gh", "release", "create", tag, *(str(root / name) for name in runtime_names(tag)),
             "--repo", repository, "--target", target, "--title", f"Little Craft Launcher {tag}",
             "--notes-file", str(notes), "--draft", "--prerelease", "--latest=false"], check=True)
        # An unpublished draft may not have a Git tag yet. The authenticated
        # release inventory includes drafts even when GET /tags/<tag> does not.
        page = 1
        while True:
            result = run(["gh", "api", f"repos/{repository}/releases?per_page=100&page={page}"],
                         check=True, capture_output=True, text=True)
            batch = json.loads(result.stdout)
            matches = [item for item in batch if item.get("tag_name") == tag]
            if matches:
                if len(matches) != 1:
                    raise ValueError("Ambiguous staged prerelease")
                release = matches[0]
                break
            if len(batch) < 100:
                raise ValueError("The staged prerelease could not be verified")
            page += 1
        if release.get("draft") is not True or release.get("prerelease") is not True:
            raise ValueError("The staged release must remain a prerelease draft")
        verify_published_assets(tag, release.get("assets", []), release.get("body", ""))
        if any(asset["digest"] != "sha256:" + hashes[asset["name"]]
               for asset in release["assets"]):
            raise ValueError("The uploaded runtime differs from the verified build")
        run(["gh", "release", "edit", tag, "--repo", repository,
             "--draft=false", "--prerelease", "--latest=false"], check=True)
    return hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"),
                        required="GITHUB_REPOSITORY" not in os.environ)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        runtime_names(args.tag)
        changes_file = Path(__file__).resolve().parent.parent / "release-notes" / f"{args.tag}.md"
        changes = changes_file.read_text(encoding="utf-8") if changes_file.is_file() else None
        if args.verify_only:
            hashes = validate_inputs(args.assets, args.tag)
            release_notes(args.repository, args.tag, hashes, changes)
        else:
            hashes = publish(args.repository, args.tag, args.assets, args.target, changes)
    except Exception:
        print("Release validation or publication did not finish; no unverified draft will be published.")
        return 1
    print(f"Verified {len(hashes)} runtime packages; checksums and dependency directions are in the release notes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
