#!/usr/bin/env python3
"""Package an existing dynamic Qt build; never archive the application sources."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import traceback
import zipfile


QT_VERSION = "6.8.3"
QT_MODULES = ("qtbase", "qtdeclarative", "qtsvg", "qtimageformats")
LIBRARY_NAME = re.compile(r"lib[^/]+\.so(?:\.[0-9]+)*\Z")
LEGAL_NAME = re.compile(r"(^|[._-])(license|copying|notice|copyright|authors|ftl|ofl)([._-]|$)", re.I)
PLUGIN_GROUPS = (
    "platforms", "imageformats", "iconengines", "platformthemes", "tls",
    "networkinformation", "xcbglintegrations", "egldeviceintegrations", "generic",
    "wayland-decoration-client", "wayland-graphics-integration-client", "wayland-shell-integration",
)
DEVELOPMENT_SUFFIXES = {".a", ".o", ".obj", ".h", ".hpp", ".c", ".cpp", ".cmake", ".pri", ".prl", ".pro", ".debug", ".pdb"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def ordinary_file(path: Path) -> Path:
    require(not path.is_symlink() and path.is_file(), "A required ordinary input file is missing.")
    require(not any(parent.is_symlink() for parent in path.parents), "An ordinary input has a linked ancestor.")
    return path


def copy_file(source: Path, destination: Path) -> None:
    ordinary_file(source)
    require(not destination.exists() and not destination.is_symlink(), "A package path would be overwritten.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o644)


def copy_sdk_entry(source: Path, destination: Path, qt_root: Path, stage: Path) -> None:
    """Keep SDK symlinks relative, and reject anything outside the selected SDK."""
    resolved = source.resolve(strict=True)
    require(resolved.is_relative_to(qt_root), "A Qt SDK link escapes its installation.")
    if source.is_symlink():
        target = stage / resolved.relative_to(qt_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(os.path.relpath(target, destination.parent), target_is_directory=resolved.is_dir())
    elif source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in sorted(source.iterdir()):
            if child.name in {"designer", ".git", "cmake", "include", "qmlcache"}:
                continue
            if child.suffix.lower() in DEVELOPMENT_SUFFIXES:
                continue
            copy_sdk_entry(child, destination / child.name, qt_root, stage)
    else:
        copy_file(source, destination)


def require_elf(path: Path) -> None:
    with path.open("rb") as handle:
        require(handle.read(4) == b"\x7fELF", "A selected runtime file is not an ELF binary.")


def copy_runtime(qt_root: Path, stage: Path) -> None:
    # The public ConfigVersion.cmake delegates to this generated version file.
    version = ordinary_file(qt_root / "lib/cmake/Qt6Core/Qt6CoreConfigVersionImpl.cmake").read_text(encoding="utf-8")
    require(re.search(r'set\(PACKAGE_VERSION\s+"?6\.8\.3"?\s*\)', version) is not None,
            "The Qt SDK version is not 6.8.3.")
    libraries = sorted(path for path in (qt_root / "lib").iterdir() if LIBRARY_NAME.fullmatch(path.name))
    require(bool(libraries), "No Qt shared libraries were found.")
    for library in libraries:
        require_elf(library.resolve(strict=True))
        copy_sdk_entry(library, stage / "lib" / library.name, qt_root, stage)
    for group in PLUGIN_GROUPS:
        directory = qt_root / "plugins" / group
        if not directory.exists():
            continue
        require(not directory.is_symlink(), "A Qt plugin directory is unexpectedly linked.")
        for plugin in sorted(directory.iterdir()):
            if LIBRARY_NAME.fullmatch(plugin.name):
                require_elf(plugin.resolve(strict=True))
                copy_sdk_entry(plugin, stage / "plugins" / group / plugin.name, qt_root, stage)
    qml = qt_root / "qml"
    require(qml.is_dir() and not qml.is_symlink(), "The Qt QML runtime modules are missing.")
    copy_sdk_entry(qml, stage / "qml", qt_root, stage)
    for required in (
        "lib/libQt6Core.so.6", "lib/libQt6Gui.so.6", "lib/libQt6Network.so.6",
        "lib/libQt6Qml.so.6", "lib/libQt6Quick.so.6", "lib/libQt6QuickControls2.so.6",
        "plugins/platforms/libqxcb.so", "plugins/platforms/libqoffscreen.so",
        "plugins/imageformats/libqwebp.so", "qml/QtQuick/qmldir",
        "qml/QtQuick/Controls/qmldir", "qml/QtQuick/Dialogs/qmldir",
        "qml/QtQuick/Layouts/qmldir", "qml/QtQuick/Templates/qmldir",
    ):
        require((stage / required).is_file(), "A required Qt runtime component was not deployed.")


def copy_legal_material(source_root: Path, stage: Path) -> None:
    native = source_root / "qt-native"
    legal_root = native / ".tools/Qt" / QT_VERSION / "Src"
    for module in QT_MODULES:
        module_root = legal_root / module
        ordinary_file(module_root / ".cmake.conf")
        require(re.search(r'QT_REPO_MODULE_VERSION\s+"6\.8\.3"',
                          (module_root / ".cmake.conf").read_text(encoding="utf-8")) is not None,
                "The Qt legal material does not match 6.8.3.")
        license_dir = module_root / "LICENSES"
        require(license_dir.is_dir() and not license_dir.is_symlink(), "Qt license texts are missing.")
        selected = {module_root / ".cmake.conf"}
        for path in module_root.rglob("*"):
            require(not path.is_symlink(), "Linked Qt legal material is not accepted.")
            if not path.is_file():
                continue
            relative = path.relative_to(module_root)
            if relative.parts[0] == "LICENSES" or path.name == "qt_attribution.json" or LEGAL_NAME.search(path.name):
                selected.add(path)
        require(any(path.is_file() for path in license_dir.iterdir()), "Qt license texts are empty.")
        # Keep exact licenses referenced by attribution, even with unusual names.
        for attribution in sorted(path for path in selected if path.name == "qt_attribution.json"):
            # Official 6.8.3 records include literal tabs/newlines in copyright
            # strings. Match the acquisition script's permissive string reader;
            # archive the original bytes and still validate every referenced path.
            entries = json.loads(attribution.read_text(encoding="utf-8-sig"), strict=False)
            if isinstance(entries, dict):
                entries = [entries]
            require(isinstance(entries, list), "A Qt attribution record has an unexpected format.")
            for entry in entries:
                require(isinstance(entry, dict), "A Qt attribution entry has an unexpected format.")
                for field in ("LicenseFile",):
                    references = entry.get(field, [])
                    if isinstance(references, str):
                        references = [references]
                    require(isinstance(references, list), "A Qt legal reference has an unexpected format.")
                    for reference in references:
                        require(isinstance(reference, str) and not Path(reference).is_absolute(),
                                "A Qt legal reference must be relative.")
                        path = attribution.parent / reference
                        require(path.resolve(strict=True).is_relative_to(module_root.resolve(strict=True)),
                                "A Qt legal reference escapes its module.")
                        ordinary_file(path)
                        selected.add(path.resolve(strict=True))
        for path in sorted(selected):
            relative = path.relative_to(module_root)
            if relative == Path(".cmake.conf"):
                relative = Path("module-version.txt")
            copy_file(path, stage / "licenses/Qt" / module / relative)
    for source, destination in (
        ("third_party/miniz/LICENSE", "licenses/miniz/LICENSE.txt"),
        ("third_party/koring/LICENSE", "licenses/koring/LICENSE"),
        ("third_party/koring/README.md", "licenses/koring/README.md"),
        ("third_party/fonts/roboto/OFL.txt", "licenses/fonts/roboto/OFL.txt"),
        ("third_party/fonts/roboto/README.md", "licenses/fonts/roboto/README.md"),
        ("third_party/fonts/material-symbols/LICENSE", "licenses/fonts/material-symbols/LICENSE"),
        ("third_party/fonts/material-symbols/README.md", "licenses/fonts/material-symbols/README.md"),
    ):
        copy_file(native / source, stage / destination)


def write_documentation(stage: Path, tag: str) -> None:
    (stage / "qt.conf").write_text("[Paths]\nPrefix=.\nLibraries=lib\nPlugins=plugins\nQmlImports=qml\n", encoding="utf-8")
    (stage / "LittleCraftLauncher").write_text(
        '#!/bin/sh\nset -eu\n'
        'bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'export LD_LIBRARY_PATH="$bundle_dir/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
        'export QT_PLUGIN_PATH="$bundle_dir/plugins"\n'
        'export QML_IMPORT_PATH="$bundle_dir/qml"\n'
        'export QML2_IMPORT_PATH="$bundle_dir/qml"\n'
        'exec "$bundle_dir/mclauncher-qt" "$@"\n', encoding="utf-8")
    (stage / "LittleCraftLauncher").chmod(0o755)
    (stage / "README.txt").write_text(
        f"Little Craft Launcher {tag} - Linux x86_64 experimental UI preview\n\n"
        "Extract the entire archive and run ./LittleCraftLauncher. Ubuntu 22.04 or newer,\n"
        "x86_64, an X11/XWayland desktop and compatible system libraries are required.\n"
        "This is a prerelease, unsigned experimental UI build. Linux online credential\n"
        "storage, managed Java installation and Minecraft game launching are not supported\n"
        "by this preview. The current game dependency resolver still targets Windows.\n"
        "Use the Windows package for those workflows; this archive does not contain Java,\n"
        "Minecraft, mods, account data or private application sources.\n\n"
        "启动：完整解压后运行 ./LittleCraftLauncher。需要 Ubuntu 22.04+ x86_64 和\n"
        "X11/XWayland 桌面。这是 Linux 界面实验预览，不支持在线凭据存储、受管 Java\n"
        "安装或 Minecraft 游戏启动；游戏相关流程请使用 Windows 包。\n\n"
        "Qt 6.8.3 is dynamically linked and its libraries remain replaceable. License\n"
        "texts and third-party notices are under licenses/ and SUPPORTING_MATERIALS.zip.\n"
        "See DYNAMIC-RUNTIME.txt for dependency source locations and library replacement.\n\n"
        "On Ubuntu, missing desktop runtime libraries can be installed with:\n"
        "sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 "
        "libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 "
        "libxcb-xinerama0 libxcb-xkb1 libxcb-sync1 libx11-xcb1 libfontconfig1 "
        "libfreetype6 libopengl0 libegl1 libgl1 libdbus-1-3 libssl3 fonts-noto-cjk\n",
        encoding="utf-8")
    (stage / "DYNAMIC-RUNTIME.txt").write_text(
        "DYNAMIC QT RUNTIME AND THIRD-PARTY MATERIALS\n\n"
        "This package uses dynamically linked Qt 6.8.3 libraries, copyright The Qt Company\n"
        "Ltd. and other contributors, under the applicable LGPLv3 and dependency licenses.\n"
        "The full LGPLv3/GPLv3 and third-party texts collected from the matching official\n"
        "Qt modules are in licenses/Qt/. miniz, the unchanged Koring wallpaper, Roboto and\n"
        "Material Symbols notices and license texts are under their licenses/ folders.\n"
        "The bundled ICU 73.2 runtime is covered by licenses/ICU/LICENSE.txt.\n"
        "Qt system dependencies (including OpenSSL, X11/XCB, graphics and font libraries)\n"
        "are supplied by the operating system and are not included in this archive.\n\n"
        "SUPPORTING_MATERIALS.zip contains these dependency notices and this replacement\n"
        "guide only. It is not an application source snapshot or a static relinking kit.\n"
        "Complete unmodified corresponding Qt module source archives for version 6.8.3:\n"
        "https://download.qt.io/archive/qt/6.8/6.8.3/submodules/\n"
        "  qtbase-everywhere-src-6.8.3.zip\n"
        "  qtdeclarative-everywhere-src-6.8.3.zip\n"
        "  qtsvg-everywhere-src-6.8.3.zip\n"
        "  qtimageformats-everywhere-src-6.8.3.zip\n"
        "The release download page provides matching dependency-source acquisition details.\n\n"
        "To use modified, interface-compatible Qt libraries, extract the package into a\n"
        "writable directory, back up lib/, plugins/ and qml/, then replace the appropriate\n"
        "shared libraries and matching plugins/QML modules with your rebuilt versions.\n"
        "Keep SONAMEs, architecture, runtime interfaces and the relative directory layout\n"
        "compatible, then start ./LittleCraftLauncher. No application signature allow-list\n"
        "or launcher-controlled password restricts running a modified-library build.\n"
        "To the extent necessary to debug modifications of these LGPL libraries, reverse\n"
        "engineering of the combined work is permitted; no application condition limits\n"
        "rights granted by the applicable LGPL or other dependency licenses.\n"
        "The application remains proprietary; no general application source or unrestricted\n"
        "redistribution license is granted by these dependency notices.\n",
        encoding="utf-8")
    with zipfile.ZipFile(stage / "SUPPORTING_MATERIALS.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in [stage / "DYNAMIC-RUNTIME.txt", *sorted((stage / "licenses").rglob("*"))]:
            if path.is_file():
                info = zipfile.ZipInfo(path.relative_to(stage).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())


def validate_stage(stage: Path) -> None:
    for path in stage.rglob("*"):
        if path.is_symlink():
            require(path.resolve(strict=True).is_relative_to(stage), "A packaged link escapes the runtime.")
        else:
            require(path.is_file() or path.is_dir(), "An unexpected special file entered the package.")
        relative = path.relative_to(stage)
        require(not any(part in {".git", "CMakeFiles", "ApplicationSnapshot", "accounts", "private", "cache"}
                        for part in relative.parts), "A forbidden source, build or data directory entered the package.")
        require(path.suffix.lower() not in {".a", ".o", ".obj", ".pdb", ".log", ".pem", ".key"},
                "A forbidden build, credential or evidence file entered the package.")


def package(args: argparse.Namespace) -> Path:
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", args.tag) is not None, "Invalid release tag.")
    source_root = Path(args.source_root).resolve(strict=True)
    build_dir = Path(args.build_dir).resolve(strict=True)
    qt_root = Path(args.qt_root).resolve(strict=True)
    output_dir = Path(args.output_dir).absolute()
    require(not output_dir.is_symlink(), "The output directory must not be a link.")
    output_dir.mkdir(parents=True, exist_ok=True)
    require(not any(output_dir.iterdir()), "The output directory must be empty.")
    executable = ordinary_file(build_dir / "mclauncher-qt")
    require_elf(executable)
    strip = shutil.which("strip")
    require(strip is not None, "The strip utility is required.")
    basename = f"LittleCraftLauncher-{args.tag}-linux-x86_64"
    final = output_dir / f"{basename}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="mcl-linux-package-", dir=output_dir.parent) as scratch:
        scratch_root = Path(scratch)
        stage = scratch_root / basename
        stage.mkdir()
        copy_file(executable, stage / executable.name)
        (stage / executable.name).chmod(0o755)
        subprocess.run([strip, "--strip-unneeded", str(stage / executable.name)], check=True, capture_output=True)
        copy_runtime(qt_root, stage)
        copy_legal_material(source_root, stage)
        icu = sorted((stage / "lib").glob("libicu*.so.*"))
        require(bool(icu) and all(path.resolve(strict=True).name.endswith(".so.73.2") for path in icu),
                "The bundled ICU runtime must match the supplied ICU 73.2 notice.")
        icu_license = Path(__file__).resolve().parent / "licenses/ICU-73.2-LICENSE.txt"
        require(hashlib.sha256(icu_license.read_bytes()).hexdigest() ==
                "f3005e195ff74d8812cc1f182a1c446fab678d70a10e3dada497585befee5416",
                "The ICU 73.2 license text did not match its fixed source.")
        copy_file(icu_license, stage / "licenses/ICU/LICENSE.txt")
        write_documentation(stage, args.tag)
        validate_stage(stage)
        temporary_archive = scratch_root / final.name
        with temporary_archive.open("wb") as output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    for path in [stage, *sorted(stage.rglob("*"))]:
                        info = archive.gettarinfo(str(path), arcname=str(path.relative_to(stage.parent)))
                        info.uid = info.gid = info.mtime = 0
                        info.uname = info.gname = ""
                        if info.isfile():
                            with path.open("rb") as handle:
                                archive.addfile(info, handle)
                        else:
                            archive.addfile(info)
        # Publish only the completed archive; source trees and staging never enter output_dir.
        require(not final.exists(), "An existing release archive must not be overwritten.")
        os.replace(temporary_archive, final)
    return final


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-root", "build-dir", "qt-root", "output-dir", "tag"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    try:
        archive = package(args)
    except Exception:
        # The workflow captures this entire process into encrypted private logs.
        # Retain the cause there; the public step emits only a fixed status.
        traceback.print_exc()
        print("Linux runtime packaging failed; no release asset should be published.", file=os.sys.stderr)
        return 1
    print(f"Packaged {archive.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
