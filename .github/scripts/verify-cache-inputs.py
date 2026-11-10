#!/usr/bin/env python3
"""Verify only public third-party inputs before saving a reusable cache."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from release_support import WAYLAND_SOURCE_ARCHIVE


QT_SOURCES = {
    "qtbase": (74010483, "992bf7766e214a341ef793eb3665fb784787d2fd666955f5f507f4c6f1f770dd"),
    "qtdeclarative": (83650162, "3ba85e9807958bd61606ce72af189be312396030b9946ca79d9e38347306d88e"),
    "qtsvg": (2651981, "90643c1ba05245abedc2bcd9d4f745568dc27a1b37dd3ba861fd19d8ac6a7c46"),
    "qtimageformats": (2999913, "30b81e4f641cdbd608ae8c01a48677338d1c69545117c5552063b63262c7eb1b"),
}


def ordinary(path):
    path = Path(path).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)) or not path.is_file():
        raise ValueError("Cache inputs must be ordinary files")
    return path


def verify_file(path, size, digest):
    path = ordinary(path)
    if path.stat().st_size != size:
        raise ValueError("Unexpected cache input size")
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    if checksum.hexdigest() != digest:
        raise ValueError("Unexpected cache input checksum")


def windows_archives(source_root, cache_root):
    lock = json.loads(ordinary(source_root / "qt-native/ci/toolchain-lock.json").read_text())
    assets = lock.get("assets", [])
    if lock.get("schemaVersion") != 1 or lock.get("qt") != "6.8.3" or len(assets) != 13:
        raise ValueError("Unsupported public toolchain lock")
    names = set()
    for asset in assets:
        name, size, digest = asset["file"], asset["bytes"], asset["sha256"]
        origin = urlsplit(asset["url"])
        if (not re.fullmatch(r"[A-Za-z0-9.-]+", name) or name in names
                or not isinstance(size, int) or not 0 < size <= 256 * 1024 * 1024
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or origin.scheme != "https" or origin.username or origin.password
                or origin.hostname not in ("download.qt.io", "github.com")):
            raise ValueError("Malformed public toolchain input")
        names.add(name)
        verify_file(cache_root / name, size, digest)
    # The cache action saves this directory, so reject extra files or folders.
    if {item.name for item in cache_root.iterdir()} != names:
        raise ValueError("Unexpected files in public toolchain cache")


def qt_source_archives(source_root, include_wayland=False):
    downloads = source_root / "qt-native/.tools/downloads"
    for module, (size, digest) in QT_SOURCES.items():
        verify_file(downloads / f"{module}-everywhere-src-6.8.3.zip", size, digest)
    if include_wayland:
        for name, (size, digest) in WAYLAND_SOURCE_ARCHIVE.items():
            verify_file(downloads / name, size, digest)


def linux_qt_sdk(qt_root):
    version = ordinary(qt_root / "lib/cmake/Qt6Core/Qt6CoreConfigVersionImpl.cmake").read_text()
    if not re.search(r'^set\(PACKAGE_VERSION "6\.8\.3"\)$', version, re.MULTILINE):
        raise ValueError("Unexpected public SDK version")
    ordinary(qt_root / "bin/qmake")
    libraries = [f"lib/libQt6{module}.so.6.8.3" for module in (
        "Core", "Gui", "Network", "DBus", "Qml", "QmlModels", "Quick", "QuickControls2",
        "QuickTemplates2", "Svg", "Test",
    )]
    libraries += [f"lib/libicu{module}.so.73.2" for module in ("data", "i18n", "uc")]
    libraries += ["lib/libQt6WaylandClient.so.6.8.3", "plugins/imageformats/libqwebp.so",
                  "plugins/platforms/libqxcb.so", "plugins/platforms/libqwayland-generic.so",
                  "plugins/platforms/libqwayland-egl.so"]
    for name in libraries:
        with ordinary(qt_root / name).open("rb") as stream:
            header = stream.read(20)
        if header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\x3e\x00":
            raise ValueError("Incomplete or incompatible public SDK module")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("windows-archives", "qt-source-archives", "linux-qt-sdk"), required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--qt-root", type=Path)
    parser.add_argument("--include-wayland", action="store_true")
    args = parser.parse_args()
    try:
        if args.kind == "windows-archives":
            windows_archives(args.source_root, args.cache_root)
        elif args.kind == "qt-source-archives":
            qt_source_archives(args.source_root, args.include_wayland)
        else:
            linux_qt_sdk(args.qt_root)
    except Exception:
        # Never expose source paths or raw input exceptions in public logs.
        print("Public cache inputs did not pass verification; no cache will be saved.", file=sys.stderr)
        return 1
    print("Public cache inputs passed verification.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
