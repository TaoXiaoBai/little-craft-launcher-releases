"""Pinned public deployment inputs; never trust a moving release tag alone."""

import hashlib
import os
from pathlib import Path
import re
import tempfile
import urllib.request
import zipfile

from release_support import QT_SOURCE_ARCHIVES, WAYLAND_SOURCE_ARCHIVE


TOOL_ASSETS = {
    "linuxdeploy-x86_64.AppImage": (
        "https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage",
        19855864, "df908b19f1ed1cdafa79508d415aa12d6c56ce4d56d4149aa8c09286718706e5"),
    "linuxdeploy-plugin-qt-x86_64.AppImage": (
        "https://github.com/linuxdeploy/linuxdeploy-plugin-qt/releases/download/continuous/linuxdeploy-plugin-qt-x86_64.AppImage",
        12704248, "cfc1055b2b9dbc08412b579f20990b7b41a17b61beaa5847dc9477c96c9e9617"),
    "linuxdeploy-plugin-appimage-x86_64.AppImage": (
        "https://github.com/linuxdeploy/linuxdeploy-plugin-appimage/releases/download/continuous/linuxdeploy-plugin-appimage-x86_64.AppImage",
        16488952, "49d6a17160675a6bd1781699aae6bdf7692d98552e02a3671d2183d10547842e"),
    "runtime-x86_64": (
        "https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64",
        944632, "617aa77314d57c8b8e4fae9c3f0b35af7b30d5529cb69e98f64f92510ed98a46"),
}
SOURCE_PINS = {**QT_SOURCE_ARCHIVES, **WAYLAND_SOURCE_ARCHIVE}


def ordinary(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError("Deployment inputs must be ordinary files")
    return path


def verify_file(path, size, digest):
    path = ordinary(path)
    if path.stat().st_size != size:
        raise ValueError("Deployment input size differs from its fixed pin")
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    if checksum.hexdigest() != digest:
        raise ValueError("Deployment input checksum differs from its fixed pin")


def verify_tools(directory):
    directory = Path(directory).absolute()
    if not directory.is_dir() or directory.is_symlink() or {p.name for p in directory.iterdir()} != set(TOOL_ASSETS):
        raise ValueError("Deployment tool cache must contain only the four pinned public inputs")
    for name, (_, size, digest) in TOOL_ASSETS.items():
        verify_file(directory / name, size, digest)
        if not os.access(directory / name, os.X_OK):
            raise ValueError("Deployment input is not executable")


def fetch_pinned(path, url, size, digest):
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        verify_file(path, size, digest)
        return
    if any(p.is_symlink() for p in (path.parent, *path.parent.parents)):
        raise ValueError("Deployment cache must not have linked ancestors")
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "LittleCraftLauncher-build"})
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".verified-download-", delete=False) as out:
        temporary = Path(out.name)
        try:
            total = 0
            with urllib.request.urlopen(request, timeout=90) as response:
                while True:
                    block = response.read(min(1024 * 1024, size + 1 - total))
                    if not block:
                        break
                    total += len(block)
                    if total > size:
                        raise ValueError("Deployment input exceeds its pinned size")
                    out.write(block)
            out.flush()
            verify_file(temporary, size, digest)
            if path.exists() or path.is_symlink():
                raise ValueError("Deployment input appeared during download")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def fetch_tools(directory):
    directory = Path(directory).absolute()
    for name, (url, size, digest) in TOOL_ASSETS.items():
        fetch_pinned(directory / name, url, size, digest)
        (directory / name).chmod(0o755)
    verify_tools(directory)


def prepare_qt_sources(source_root):
    """Fetch only pinned public source archives; extract outside runtime staging."""
    native = Path(source_root).absolute() / "qt-native"
    downloads = native / ".tools/downloads"
    source_base = native / ".tools/Qt/6.8.3/Src"
    for name, (size, digest) in SOURCE_PINS.items():
        archive = downloads / name
        fetch_pinned(archive, "https://download.qt.io/archive/qt/6.8/6.8.3/submodules/" + name,
                     size, digest)
        module = name.removesuffix("-everywhere-src-6.8.3.zip")
        destination = source_base / module
        if destination.exists():
            config = ordinary(destination / ".cmake.conf").read_text()
            if not re.search(r'QT_REPO_MODULE_VERSION\s+"6\.8\.3"', config):
                raise ValueError("Existing Qt license source has the wrong version")
            continue
        if any(p.is_symlink() for p in (source_base, *source_base.parents)):
            raise ValueError("Qt source location has a linked ancestor")
        source_base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=source_base, prefix=".qt-source-") as scratch:
            staging = Path(scratch) / module
            staging.mkdir()
            with zipfile.ZipFile(archive) as source:
                entries = source.infolist()
                if len(entries) > 100000 or sum(i.file_size for i in entries) > 2 * 1024 ** 3:
                    raise ValueError("Qt source archive exceeds its extraction budget")
                seen = set()
                for entry in entries:
                    parts = Path(entry.filename).parts
                    if (not parts or parts[0] != module + "-everywhere-src-6.8.3"
                            or Path(entry.filename).is_absolute() or ".." in parts
                            or "\\" in entry.filename or entry.filename in seen
                            or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                        raise ValueError("Qt source archive has an unsafe member")
                    seen.add(entry.filename)
                    if len(parts) == 1:
                        continue
                    target = staging.joinpath(*parts[1:])
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with source.open(entry) as inp, target.open("xb") as out:
                            while block := inp.read(1024 * 1024):
                                out.write(block)
            config = ordinary(staging / ".cmake.conf").read_text()
            if not re.search(r'QT_REPO_MODULE_VERSION\s+"6\.8\.3"', config):
                raise ValueError("Qt source version differs from the pinned SDK")
            os.replace(staging, destination)
