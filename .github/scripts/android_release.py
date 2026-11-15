#!/usr/bin/env python3
"""Name, version and attach the signed Android package of a prerelease.

The two desktop packages are published first and remain a complete release on
their own. The Android package is an additional download: it is attached only
to that already published prerelease, never replaces an upload, and a failure
here leaves the desktop release as it was.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

from release_support import android_name, android_notes, android_version, verify_published_assets

METADATA = "android-release.json"
REQUIRED_ENTRIES = (
    "AndroidManifest.xml", "classes.dex",
    "lib/arm64-v8a/libmcl-android_arm64-v8a.so", "lib/arm64-v8a/libpojavexec.so",
    "lib/arm64-v8a/libssl_3.so", "lib/arm64-v8a/libcrypto_3.so",
    "lib/arm64-v8a/libc++_shared.so", "assets/android_rcc_bundle.rcc",
)


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def describe(tag, apk, certificate, definition):
    """Metadata written by the build next to the package it has just signed."""
    if apk.name != android_name(tag) or apk.is_symlink() or not apk.is_file():
        raise ValueError("Unexpected Android package")
    return {"schema": 1, "tag": tag, "name": apk.name, "size": apk.stat().st_size,
            "sha256": checksum(apk), "certificate_sha256": certificate,
            "runtime_definition": definition}


def validate_package(root, tag):
    name = android_name(tag)
    entries = list(root.iterdir())
    if ({path.name for path in entries} != {name, METADATA} or len(entries) != 2
            or any(path.is_symlink() or not path.is_file() for path in entries)):
        raise ValueError("Expected exactly the signed Android package and its metadata")
    metadata = json.loads((root / METADATA).read_text(encoding="utf-8"))
    apk = root / name
    if (not isinstance(metadata, dict)
            or set(metadata) != {"schema", "tag", "name", "size", "sha256",
                                 "certificate_sha256", "runtime_definition"}
            or metadata["schema"] != 1 or metadata["tag"] != tag or metadata["name"] != name
            or type(metadata["size"]) is not int or metadata["size"] <= 0
            or metadata["size"] != apk.stat().st_size
            or not all(isinstance(metadata[key], str) for key in
                       ("sha256", "certificate_sha256", "runtime_definition"))
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["certificate_sha256"])
            or not re.fullmatch(r"[0-9a-f]{40}", metadata["runtime_definition"])
            or checksum(apk) != metadata["sha256"]):
        raise ValueError("The Android package differs from its build record")
    try:
        with zipfile.ZipFile(apk) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or archive.testzip() is not None:
                raise ValueError("The Android package is damaged")
            if any(entry not in names or archive.getinfo(entry).file_size <= 0
                   for entry in REQUIRED_ENTRIES):
                raise ValueError("The Android package is incomplete")
    except zipfile.BadZipFile:
        raise ValueError("The Android package is damaged") from None
    return metadata


def published_release(repository, tag, run):
    result = run(["gh", "api", f"repos/{repository}/releases/tags/{tag}"],
                 check=True, capture_output=True, text=True)
    release = json.loads(result.stdout)
    if (not isinstance(release, dict) or release.get("tag_name") != tag
            or release.get("draft") is not False or release.get("prerelease") is not True
            or not isinstance(release.get("body"), str) or not isinstance(release.get("assets"), list)):
        raise ValueError("A published prerelease is required")
    return release


def attach(repository, tag, root, run=subprocess.run):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid release repository")
    metadata = validate_package(root, tag)
    name = metadata["name"]
    release = published_release(repository, tag, run)
    # Strict: exactly the two verified desktop packages, so nothing is replaced.
    verify_published_assets(tag, release["assets"], release["body"])
    section = android_notes(repository, tag, metadata["sha256"],
                            metadata["certificate_sha256"], metadata["runtime_definition"])
    if name in release["body"]:
        raise ValueError("The release already describes an Android package")
    run(["gh", "release", "upload", tag, str(root / name), "--repo", repository], check=True)
    with tempfile.TemporaryDirectory(prefix="little-craft-android-") as temporary:
        notes = Path(temporary) / "release-notes.md"
        notes.write_text(release["body"].rstrip("\n") + "\n" + section, encoding="utf-8")
        run(["gh", "release", "edit", tag, "--repo", repository, "--notes-file", str(notes),
             "--prerelease", "--latest=false"], check=True)
    release = published_release(repository, tag, run)
    verify_published_assets(tag, release["assets"], release["body"], allow_android=True)
    uploaded = [item for item in release["assets"] if item.get("name") == name]
    if len(uploaded) != 1 or uploaded[0].get("digest") != "sha256:" + metadata["sha256"]:
        raise ValueError("The uploaded Android package differs from the signed build")
    return metadata


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    version = commands.add_parser("version", help="Print the package name, version name and version code")
    version.add_argument("--tag", required=True)
    record = commands.add_parser("describe", help="Write the build record of a signed package")
    record.add_argument("--tag", required=True)
    record.add_argument("--apk", type=Path, required=True)
    record.add_argument("--certificate", required=True)
    record.add_argument("--runtime-definition", required=True)
    publish = commands.add_parser("attach", help="Attach a signed package to its published prerelease")
    publish.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"),
                         required="GITHUB_REPOSITORY" not in os.environ)
    publish.add_argument("--tag", required=True)
    publish.add_argument("--package", type=Path, required=True)
    publish.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(arguments)
    try:
        if args.command == "version":
            name, code = android_version(args.tag)
            print(f"{android_name(args.tag)} {name} {code}")
        elif args.command == "describe":
            metadata = describe(args.tag, args.apk, args.certificate, args.runtime_definition)
            (args.apk.parent / METADATA).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            validate_package(args.apk.parent, args.tag)
        elif args.verify_only:
            validate_package(args.package, args.tag)
            print("The signed Android package matches its build record.")
        else:
            metadata = attach(args.repository, args.tag, args.package)
            print(f"Attached {metadata['name']} ({metadata['sha256']}).")
    except ValueError as error:
        # These are the fixed messages above (json errors carry no file content
        # worth hiding: the record holds only public checksums).
        print(f"ANDROID_RELEASE_REJECTED: {error}", file=sys.stderr)
        return 1
    except Exception:
        # Raw API/CLI errors can contain credentials; report a fixed category.
        print("ANDROID_RELEASE_FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
