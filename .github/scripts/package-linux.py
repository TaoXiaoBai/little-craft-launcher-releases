#!/usr/bin/env python3
"""Deploy an existing Qt build as an AppImage; never package application sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import traceback
import zipfile

from linux_toolchain import verify_tools


QT_VERSION = "6.8.3"
QT_MODULES = ("qtbase", "qtdeclarative", "qtsvg", "qtimageformats", "qtwayland")
TOOL_NAMES = ("linuxdeploy-x86_64.AppImage", "linuxdeploy-plugin-qt-x86_64.AppImage",
              "linuxdeploy-plugin-appimage-x86_64.AppImage")
PLATFORM_PLUGINS = ("libqxcb.so", "libqwayland-generic.so", "libqwayland-egl.so")
LIBRARY_NAME = re.compile(r"lib[^/]+\.so(?:\.[0-9]+)*\Z")
LEGAL_NAME = re.compile(r"(^|[._-])(license|copying|notice|copyright|authors|ftl|ofl)([._-]|$)", re.I)
PLUGIN_GROUPS = (
    "imageformats", "iconengines", "platformthemes", "platforminputcontexts", "tls",
    "networkinformation", "xcbglintegrations",
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
    """Stage explicit dlopen plugins; linuxdeploy resolves linked libraries/QML.

    Copying every SDK library or the entire QML tree makes unrelated modules
    (PrintSupport, SQL, etc.) look like application dependencies to linuxdeploy.
    The application links Core/Gui/Network/Qml/Quick/QuickControls2; their NEEDED
    closure and qmlimportscanner determine what belongs in the runtime.
    """
    version = ordinary_file(qt_root / "lib/cmake/Qt6Core/Qt6CoreConfigVersionImpl.cmake").read_text(encoding="utf-8")
    require(re.search(r'set\(PACKAGE_VERSION\s+"?6\.8\.3"?\s*\)', version) is not None,
            "The Qt SDK version is not 6.8.3.")
    for module in ("Core", "Gui", "Network", "DBus", "Qml", "Quick", "QuickControls2"):
        require_elf(ordinary_file((qt_root / "lib" / f"libQt6{module}.so.6").resolve(strict=True)))
    for name in (*PLATFORM_PLUGINS, "libqoffscreen.so"):
        source = qt_root / "plugins/platforms" / name
        require(source.is_file(), "A required Qt runtime component is missing from the SDK.")
        require_elf(source.resolve(strict=True))
        copy_sdk_entry(source, stage / "plugins/platforms" / name, qt_root, stage)
    for group in PLUGIN_GROUPS:
        directory = qt_root / "plugins" / group
        if not directory.exists():
            continue
        require(not directory.is_symlink(), "A Qt plugin directory is unexpectedly linked.")
        for plugin in sorted(directory.iterdir()):
            if LIBRARY_NAME.fullmatch(plugin.name):
                require_elf(plugin.resolve(strict=True))
                copy_sdk_entry(plugin, stage / "plugins" / group / plugin.name, qt_root, stage)
    require((qt_root / "plugins/imageformats/libqwebp.so").is_file(),
            "The Qt WebP image plugin is missing from the SDK.")
    require((qt_root / "qml").is_dir() and not (qt_root / "qml").is_symlink(),
            "The Qt QML runtime modules are missing from the SDK.")


def require_deployed_runtime(stage: Path) -> None:
    for required in (
        "usr/lib/libQt6Core.so.6", "usr/lib/libQt6Gui.so.6", "usr/lib/libQt6Network.so.6",
        "usr/lib/libQt6DBus.so.6",
        "usr/lib/libQt6Qml.so.6", "usr/lib/libQt6Quick.so.6", "usr/lib/libQt6QuickControls2.so.6",
        "usr/plugins/platforms/libqxcb.so", "usr/plugins/platforms/libqoffscreen.so",
        "usr/plugins/platforms/libqwayland-generic.so", "usr/plugins/platforms/libqwayland-egl.so",
        "usr/plugins/imageformats/libqwebp.so", "usr/qml/QtQuick/qmldir",
        "usr/qml/QtQuick/Controls/qmldir", "usr/qml/QtQuick/Dialogs/qmldir",
        "usr/qml/QtQuick/Layouts/qmldir", "usr/qml/QtQuick/Templates/qmldir",
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


def write_generated(path: Path, content: str, mode: int = 0o644) -> None:
    # linuxdeploy may have created a link here. Never write through it into an ELF.
    require(not path.is_dir(), "A generated package path is unexpectedly a directory.")
    if path.is_symlink() or path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def write_documentation(stage: Path, tag: str) -> None:
    write_generated(stage / "usr/bin/qt.conf",
                    "[Paths]\nPrefix=..\nLibraries=lib\nPlugins=plugins\nQmlImports=qml\n")
    write_generated(stage / "AppRun",
        '#!/bin/sh\nset -eu\n'
        'bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'if [ "${LD_LIBRARY_PATH+x}" = x ]; then\n'
        '  export MCL_HOST_LD_LIBRARY_PATH="$LD_LIBRARY_PATH"\n'
        'else unset MCL_HOST_LD_LIBRARY_PATH; fi\n'
        'if [ "${LD_PRELOAD+x}" = x ]; then\n'
        '  export MCL_HOST_LD_PRELOAD="$LD_PRELOAD"\n'
        'else unset MCL_HOST_LD_PRELOAD; fi\n'
        'export MCL_APPIMAGE_RUNTIME_ROOT="$bundle_dir"\n'
        # Only this child process is changed. Display, input-method settings and an
        # explicit QT_QPA_PLATFORM remain available to Qt's backend selection.
        'unset QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH QML_IMPORT_PATH QML2_IMPORT_PATH\n'
        'unset LD_LIBRARY_PATH LD_PRELOAD\n'
        'export LD_LIBRARY_PATH="$bundle_dir/usr/lib"\n'
        'export QT_PLUGIN_PATH="$bundle_dir/usr/plugins"\n'
        'export QT_QPA_PLATFORM_PLUGIN_PATH="$bundle_dir/usr/plugins/platforms"\n'
        'export QML_IMPORT_PATH="$bundle_dir/usr/qml"\n'
        'export QML2_IMPORT_PATH="$bundle_dir/usr/qml"\n'
        'exec "$bundle_dir/usr/bin/mclauncher-qt" "$@"\n', 0o755)
    (stage / "README.txt").write_text(
        f"Little Craft Launcher {tag} - Linux x86_64 prerelease\n\n"
        "AppImage: chmod +x LittleCraftLauncher-*-linux-x86_64.AppImage, then run it.\n"
        "Without FUSE, run ./LittleCraftLauncher-*-linux-x86_64.AppImage --appimage-extract\n"
        "and start ./squashfs-root/AppRun.\n"
        "Ubuntu 22.04 or newer, x86_64 and an X11/XWayland or Wayland desktop are required.\n"
        "Both XCB and Wayland Qt backends and their deployable dependencies are bundled;\n"
        "the operating system supplies glibc, graphics drivers and desktop services.\n"
        "An explicit QT_QPA_PLATFORM=xcb or QT_QPA_PLATFORM=wayland is supported.\n\n"
        "Linux x86_64 supports native game dependencies and launch, local Java discovery\n"
        "and Mojang managed Java 8/17/21/25 downloads. Game launch still requires a\n"
        "permitted account and a matching Java runtime. Microsoft sign-in is awaiting\n"
        "the project's public-client configuration and service access.\n"
        "Online credentials use the desktop Secret Service keyring (for example GNOME\n"
        "Keyring or a configured compatible provider). A missing, locked or unresponsive\n"
        "keyring produces an error, without plaintext fallback. Credential storage is\n"
        "scoped to the local user/keyring; moving profile files does not move credentials.\n"
        "By default data stays in per-user application storage. --portable keeps data\n"
        "beside the AppImage carrier, or at the root of a manually extracted AppDir.\n"
        "This unsigned prerelease does not contain Java, Minecraft, mods, account data\n"
        "or private application sources.\n\n"
        "启动：赋予 AppImage 执行权限后直接运行。没有 FUSE 时，用 --appimage-extract\n"
        "解包，再运行 squashfs-root/AppRun。需要 Ubuntu 22.04+ x86_64 和 X11/XWayland\n"
        "或 Wayland 桌面。支持 Linux x86_64 游戏启动、本机 Java 检测及 Mojang 官方\n"
        "Java 8/17/21/25 下载。启动仍需可用账号及匹配的 Java；Microsoft 登录尚待\n"
        "本项目客户端配置与服务准入。在线凭据需要桌面 Secret Service 系统密钥环，\n"
        "缺失、无法解锁或超时会明确失败，不回退明文保存。默认使用每用户数据目录；\n"
        "--portable 将数据放在 AppImage 文件旁，手动解包时放在 AppDir 根目录。\n\n"
        "Qt 6.8.3 is dynamically linked and its libraries remain replaceable. License\n"
        "texts and third-party notices are under licenses/ and SUPPORTING_MATERIALS.zip.\n"
        "See DYNAMIC-RUNTIME.txt for dependency source locations and library replacement.\n",
        encoding="utf-8")
    (stage / "DYNAMIC-RUNTIME.txt").write_text(
        "DYNAMIC QT RUNTIME AND THIRD-PARTY MATERIALS\n\n"
        "This package uses dynamically linked Qt 6.8.3 libraries, copyright The Qt Company\n"
        "Ltd. and other contributors, under the applicable LGPLv3 and dependency licenses.\n"
        "The full LGPLv3/GPLv3 and third-party texts collected from the matching official\n"
        "Qt modules are in licenses/Qt/. miniz, the unchanged Koring wallpaper, Roboto and\n"
        "Material Symbols notices and license texts are under their licenses/ folders.\n"
        "The bundled ICU 73.2 runtime is covered by licenses/ICU/LICENSE.txt.\n"
        "linuxdeploy also collects redistributable runtime dependencies. System glibc,\n"
        "graphics drivers and desktop services remain supplied by the operating system.\n\n"
        "SUPPORTING_MATERIALS.zip contains these dependency notices and this replacement\n"
        "guide only. It is not an application source snapshot or a static relinking kit.\n"
        "Complete unmodified corresponding Qt module source archives for version 6.8.3:\n"
        "https://download.qt.io/archive/qt/6.8/6.8.3/submodules/\n"
        "  qtbase-everywhere-src-6.8.3.zip\n"
        "  qtdeclarative-everywhere-src-6.8.3.zip\n"
        "  qtsvg-everywhere-src-6.8.3.zip\n"
        "  qtimageformats-everywhere-src-6.8.3.zip\n"
        "  qtwayland-everywhere-src-6.8.3.zip\n"
        "The release download page provides matching dependency-source acquisition details.\n\n"
        "To use modified, interface-compatible Qt libraries, first run the AppImage with\n"
        "--appimage-extract, then work in the writable squashfs-root directory.\n"
        "Back up usr/lib/, usr/plugins/ and usr/qml/, and replace the\n"
        "appropriate shared libraries and matching plugins/QML modules with your rebuilt\n"
        "versions. Keep SONAMEs, architecture, runtime interfaces and the relative layout\n"
        "compatible, then start ./AppRun. No application signature allow-list or launcher-\n"
        "controlled password restricts running a modified-library build. The AppRun wrapper\n"
        "uses only these bundled library/plugin/QML paths for its own child process.\n"
        "To the extent necessary to debug modifications of these LGPL libraries, reverse\n"
        "engineering of the combined work is permitted; no application condition limits\n"
        "rights granted by the applicable LGPL or other dependency licenses.\n"
        "The application remains proprietary; no general application source or unrestricted\n"
        "redistribution license is granted by these dependency notices.\n",
        encoding="utf-8")
    manifest = {
        "schema": 2, "version": tag, "platform": "linux-x86_64", "format": "appimage",
        "qt": QT_VERSION, "linkage": "dynamic", "displayBackends": ["xcb", "wayland"],
        "application": "usr/bin/mclauncher-qt",
    }
    (stage / "runtime-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(stage / "SUPPORTING_MATERIALS.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in [stage / "DYNAMIC-RUNTIME.txt", *sorted((stage / "licenses").rglob("*"))]:
            if path.is_file():
                info = zipfile.ZipInfo(path.relative_to(stage).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())


def validate_stage(stage: Path, qt_root: Path | None = None) -> None:
    for path in stage.rglob("*"):
        if path.is_symlink():
            require(path.resolve(strict=True).is_relative_to(stage), "A packaged link escapes the runtime.")
        else:
            require(path.is_file() or path.is_dir(), "An unexpected special file entered the package.")
        relative = path.relative_to(stage)
        require(not any(part in {".git", "CMakeFiles", "ApplicationSnapshot", "accounts", "private", "cache"}
                        for part in relative.parts), "A forbidden source, build or data directory entered the package.")
        require(path.suffix.lower() not in DEVELOPMENT_SUFFIXES | {".log", ".pem", ".key"},
                "A forbidden source, build, credential or evidence file entered the package.")
        # The scanner sees the private application's source directory but must
        # never deploy it. Only unchanged Qt SDK QML/JS sources may be packaged.
        if path.is_file() and (path.suffix.lower() in {".qml", ".js", ".mjs"} or path.name == "qmldir"):
            require(relative.is_relative_to("usr/qml") and qt_root is not None,
                    "Application QML sources must not enter the runtime package.")
            sdk_file = qt_root / "qml" / relative.relative_to("usr/qml")
            require(sdk_file.is_file() and sdk_file.read_bytes() == path.read_bytes(),
                    "A deployed QML source does not match the selected Qt SDK.")


def deployment_environment(qt_root: Path, tool_dir: Path, source_root: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML_IMPORT_PATH",
                                  "QML2_IMPORT_PATH", "QML_MODULES_PATHS", "QML_SOURCES_PATHS",
                                  "QMAKE", "LD_LIBRARY_PATH", "LD_PRELOAD", "OUTPUT"}
                   and not key.startswith("LDAI_")}
    environment.update({
        "PATH": str(tool_dir) + os.pathsep + os.defpath,
        "LD_LIBRARY_PATH": str(qt_root / "lib"),
        "QMAKE": str(qt_root / "bin/qmake"),
        "QML_SOURCES_PATHS": str(source_root / "qt-native/src/ui"),
        "EXTRA_PLATFORM_PLUGINS": ";".join(PLATFORM_PLUGINS),
        "APPIMAGE_EXTRACT_AND_RUN": "1", "ARCH": "x86_64",
    })
    return environment


def deploy_appdir(stage: Path, scratch: Path, source_root: Path, qt_root: Path,
                  tool_dir: Path, strip: str, patchelf: str) -> dict[str, str]:
    # Validate every pinned tool and the output runtime before starting even qmake.
    verify_tools(tool_dir)
    qmake = ordinary_file(qt_root / "bin/qmake")
    environment = deployment_environment(qt_root, tool_dir, source_root)
    version = subprocess.run([str(qmake), "-query", "QT_VERSION"], check=True,
                             capture_output=True, text=True, env=environment)
    require(version.stdout.strip() == QT_VERSION, "The deployment qmake is not Qt 6.8.3.")
    executable = stage / "usr/bin/mclauncher-qt"
    subprocess.run([strip, "--strip-unneeded", str(executable)], check=True, capture_output=True)
    copy_runtime(qt_root, stage / "usr")
    desktop = scratch / "mclauncher.desktop"
    desktop.write_text(
        "[Desktop Entry]\nType=Application\nName=Little Craft Launcher\n"
        "Exec=mclauncher-qt\nIcon=mclauncher\nTerminal=false\nCategories=Game;\n", encoding="utf-8")
    icon = scratch / "mclauncher.png"
    copy_file(source_root / "qt-native/src/branding/launcher-icon-256.png", icon)
    system_libraries = system_library_paths()
    tls_libraries = [system_libraries[name] for name in ("libssl.so.3", "libcrypto.so.3")]
    command = [str(tool_dir / TOOL_NAMES[0]), "--appdir", str(stage), "--executable", str(executable),
               "--desktop-file", str(desktop), "--icon-file", str(icon), "--plugin", "qt"]
    for library in tls_libraries:
        command.extend(("--library", str(library)))
    subprocess.run(command, check=True, capture_output=True, text=True, env=environment, cwd=scratch)
    subprocess.run([patchelf, "--set-rpath", "$ORIGIN/../lib", str(executable)],
                   check=True, capture_output=True)
    runpath = subprocess.run([patchelf, "--print-rpath", str(executable)],
                             check=True, capture_output=True, text=True)
    require(runpath.stdout.strip() == "$ORIGIN/../lib", "The application RUNPATH is not relocatable.")
    require_deployed_runtime(stage)
    require(any(stage.glob("*.desktop")) and (stage / "mclauncher.png").is_file(),
            "The AppDir desktop entry or icon is missing.")
    return environment


def system_library_paths() -> dict[str, Path]:
    result = subprocess.run(["ldconfig", "-p"], check=True, capture_output=True, text=True)
    libraries: dict[str, Path] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(lib[^ ]+\.so(?:\.[0-9]+)*) \(([^)]+)\) => (/[^\n]+)$", line)
        if match and "x86-64" in match[2]:
            libraries.setdefault(match[1], Path(match[3]))
    require(all(name in libraries for name in ("libssl.so.3", "libcrypto.so.3")),
            "The Ubuntu OpenSSL 3 runtime libraries are missing.")
    return libraries


def copy_system_legal_material(stage: Path, qt_root: Path,
                               documentation_root: Path = Path("/usr/share/doc"),
                               common_license_root: Path = Path("/usr/share/common-licenses")) -> None:
    """Record and carry notices for dependencies linuxdeploy added from Ubuntu."""
    libraries = system_library_paths()
    sdk_libraries = {path.name for path in (qt_root / "lib").iterdir() if LIBRARY_NAME.fullmatch(path.name)}
    packages: dict[str, dict] = {}
    for bundled in sorted((stage / "usr/lib").rglob("*")):
        if not bundled.is_file() or not LIBRARY_NAME.fullmatch(bundled.name) or bundled.name in sdk_libraries:
            continue
        origin = libraries.get(bundled.name)
        # linuxdeploy can preserve a fully versioned filename not in ldconfig's
        # SONAME key. Resolve cache entries before asking dpkg about ownership.
        if origin is None:
            origin = next((path for path in libraries.values()
                           if path.resolve(strict=True).name == bundled.name), None)
        require(origin is not None, "A deployed system library has no recorded system origin.")
        owner = None
        for candidate in dict.fromkeys((origin, origin.resolve(strict=True),
                                       Path(str(origin).removeprefix("/usr")) if str(origin).startswith("/usr/lib/")
                                       else Path("/usr" + str(origin)))):
            query = subprocess.run(["dpkg-query", "-S", str(candidate)], capture_output=True, text=True)
            if query.returncode == 0:
                owners = [line.rsplit(": ", 1)[0] for line in query.stdout.splitlines() if ": " in line]
                if owners:
                    owner = owners[0].split(", ")[0]
                    break
        require(owner is not None and re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?", owner) is not None,
                "A deployed system library has no unambiguous package owner.")
        if owner not in packages:
            query = subprocess.run(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n", owner],
                                   check=True, capture_output=True, text=True)
            fields = query.stdout.rstrip("\n").split("\t")
            require(len(fields) == 4 and fields[0] == owner, "A system package source record is invalid.")
            package_name = owner.split(":", 1)[0]
            copyright_file = (documentation_root / package_name / "copyright").resolve(strict=True)
            require(copyright_file.is_relative_to(documentation_root.resolve(strict=True)), "A system copyright path escapes documentation.")
            notice = Path("licenses/system-libraries") / owner.replace(":", "_") / "copyright"
            copy_file(copyright_file, stage / notice)
            packages[owner] = {
                "binaryPackage": owner, "version": fields[1],
                "sourcePackage": fields[2] or package_name, "sourceVersion": fields[3] or fields[1],
                "copyright": notice.as_posix(), "bundledFiles": [],
            }
        packages[owner]["bundledFiles"].append(bundled.relative_to(stage).as_posix())
    require(bool(packages), "No deployed system dependency notices were collected.")
    # Debian/Ubuntu copyright records refer to these standard license texts by
    # absolute host path. Carry them beside the records for offline readability.
    require(common_license_root.is_dir(), "The system common license texts are missing.")
    for license_file in sorted(common_license_root.iterdir()):
        resolved = license_file.resolve(strict=True)
        require(resolved.is_relative_to(common_license_root.resolve(strict=True)),
                "A system common license link escapes its directory.")
        if resolved.is_file():
            copy_file(resolved, stage / "licenses/system-libraries/common-licenses" / license_file.name)
    manifest = stage / "licenses/system-libraries/manifest.json"
    manifest.write_text(json.dumps({"schema": 1, "packages": list(packages.values())}, indent=2) + "\n", encoding="utf-8")


def verify_deployed_icu(stage: Path, qt_root: Path) -> list[dict]:
    """Trace deployed ICU SONAME copies back to the selected SDK's ICU 73.2.

    linuxdeploy strips/repatches libraries and can name real bytes .so.73. Compare
    full hashes first; otherwise require the original GNU build ID and SONAME to
    survive those permitted changes. The source itself must resolve to .so.73.2.
    """
    bundled_files = sorted((stage / "usr/lib").glob("libicu*.so.*"))
    require(bool(bundled_files), "The bundled ICU 73.2 runtime is missing.")
    records = []
    for bundled in bundled_files:
        source = ordinary_file((qt_root / "lib" / bundled.name).resolve(strict=True))
        name = re.fullmatch(r"(libicu[a-z0-9]+\.so\.73)\.2", source.name)
        require(name is not None, "The selected Qt SDK ICU input is not version 73.2.")
        source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        deployed_digest = hashlib.sha256(bundled.read_bytes()).hexdigest()
        build_id = None
        if source_digest != deployed_digest:
            identities = []
            for path in (source, bundled):
                notes = subprocess.run(["readelf", "--notes", str(path)], check=True,
                                       capture_output=True, text=True)
                dynamic = subprocess.run(["readelf", "--dynamic", str(path)], check=True,
                                         capture_output=True, text=True)
                ids = re.findall(r"Build ID:\s*([a-fA-F0-9]{40,128})", notes.stdout)
                sonames = re.findall(r"\(SONAME\).*?\[([^]\n]+)\]", dynamic.stdout)
                require(len(ids) == 1 and sonames == [name[1]],
                        "A deployed ICU library has no valid original build ID/SONAME.")
                identities.append(ids[0].lower())
            require(identities[0] == identities[1], "A deployed ICU library differs from the selected SDK build.")
            build_id = identities[0]
        records.append({"bundledPath": bundled.relative_to(stage).as_posix(),
                        "sdkFile": source.name, "sdkSha256": source_digest,
                        "deployedSha256": deployed_digest, "elfBuildId": build_id})
    return records


def package(args: argparse.Namespace) -> Path:
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", args.tag) is not None, "Invalid release tag.")
    source_root = Path(args.source_root).resolve(strict=True)
    build_dir = Path(args.build_dir).resolve(strict=True)
    qt_root = Path(args.qt_root).resolve(strict=True)
    supplied_tools = getattr(args, "tool_dir", None) or os.environ.get("LINUXDEPLOY_TOOL_DIR")
    tool_dir = Path(supplied_tools or Path(__file__).resolve().parents[2] / ".tools/linuxdeploy").resolve(strict=True)
    output_dir = Path(args.output_dir).absolute()
    require(not output_dir.is_symlink(), "The output directory must not be a link.")
    output_dir.mkdir(parents=True, exist_ok=True)
    require(not any(output_dir.iterdir()), "The output directory must be empty.")
    executable = ordinary_file(build_dir / "mclauncher-qt")
    require_elf(executable)
    strip, patchelf = shutil.which("strip"), shutil.which("patchelf")
    require(strip is not None and patchelf is not None, "The strip and patchelf utilities are required.")
    basename = f"LittleCraftLauncher-{args.tag}-linux-x86_64"
    final = output_dir / (basename + ".AppImage")
    with tempfile.TemporaryDirectory(prefix="mcl-linux-package-", dir=output_dir.parent) as scratch:
        scratch_root = Path(scratch)
        stage = scratch_root / basename
        stage.mkdir()
        copy_file(executable, stage / "usr/bin/mclauncher-qt")
        (stage / "usr/bin/mclauncher-qt").chmod(0o755)
        environment = deploy_appdir(stage, scratch_root, source_root, qt_root, tool_dir, strip, patchelf)
        copy_legal_material(source_root, stage)
        icu_inputs = verify_deployed_icu(stage, qt_root)
        icu_license = Path(__file__).resolve().parent / "licenses/ICU-73.2-LICENSE.txt"
        require(hashlib.sha256(icu_license.read_bytes()).hexdigest() ==
                "f3005e195ff74d8812cc1f182a1c446fab678d70a10e3dada497585befee5416",
                "The ICU 73.2 license text did not match its fixed source.")
        copy_file(icu_license, stage / "licenses/ICU/LICENSE.txt")
        (stage / "licenses/ICU/runtime-inputs.json").write_text(
            json.dumps({"schema": 1, "version": "73.2", "inputs": icu_inputs}, indent=2) + "\n",
            encoding="utf-8")
        copy_system_legal_material(stage, qt_root)
        write_documentation(stage, args.tag)
        validate_stage(stage, qt_root)
        temporary_archive = scratch_root / final.name
        environment.update({
            "LDAI_OUTPUT": str(temporary_archive), "LDAI_VERSION": args.tag,
            "LDAI_RUNTIME_FILE": str(tool_dir / "runtime-x86_64"), "LDAI_NO_APPSTREAM": "1",
        })
        custom_apprun = scratch_root / "AppRun"
        copy_file(stage / "AppRun", custom_apprun)
        custom_apprun.chmod(0o755)
        subprocess.run([str(tool_dir / TOOL_NAMES[0]), "--appdir", str(stage),
                        "--custom-apprun", str(custom_apprun), "--output", "appimage"],
                       check=True, capture_output=True, text=True, env=environment, cwd=scratch_root)
        ordinary_file(temporary_archive)
        require_elf(temporary_archive)
        with temporary_archive.open("rb") as handle:
            handle.seek(8)
            require(handle.read(3) == b"AI\x02", "The output is not a type-2 AppImage.")
        # A packaging plugin must not replace our isolated launcher or inject source.
        require((stage / "AppRun").read_bytes() == custom_apprun.read_bytes(),
                "The isolated AppRun was unexpectedly replaced.")
        validate_stage(stage, qt_root)
        temporary_archive.chmod(0o755)
        # Only the completed public runtime enters output_dir; staging stays private.
        require(not final.exists(), "An existing release archive must not be overwritten.")
        os.replace(temporary_archive, final)
    return final


def report_tool_failure(error: subprocess.CalledProcessError) -> None:
    """Keep a bounded diagnostic tail in the workflow's encrypted private log."""
    def encoded(value: str | bytes | None) -> bytes:
        if value is None:
            return b""
        return value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")

    stdout = encoded(error.stdout)
    stderr = encoded(error.stderr)
    if stdout or stderr:
        # Keep the combined tail within 16 KiB, including stream labels. Ignore
        # a split UTF-8 character instead of increasing the byte bound to replace it.
        tail = (b"stdout:\n" + stdout[-16384:] + b"\nstderr:\n" + stderr[-16384:])[-16384:]
        print("Deployment command output (last 16 KiB):", file=os.sys.stderr)
        print(tail.decode("utf-8", errors="ignore"), file=os.sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-root", "build-dir", "qt-root", "output-dir", "tag"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--tool-dir", help="Already verified linuxdeploy tools (or LINUXDEPLOY_TOOL_DIR).")
    args = parser.parse_args()
    try:
        archive = package(args)
    except Exception as error:
        if isinstance(error, subprocess.CalledProcessError):
            report_tool_failure(error)
        # The workflow captures this entire process into encrypted private logs.
        # Retain the cause there; the public step emits only a fixed status.
        traceback.print_exc()
        print("Linux runtime packaging failed; no release asset should be published.", file=os.sys.stderr)
        return 1
    print(f"Packaged {archive.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
