#!/usr/bin/env python3
"""Pure fixtures for the Linux AppDir packager; no downloads, Qt or GUI needed."""

import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

SCRIPT_ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("package_linux", SCRIPT_ROOT / "package-linux.py")
PACKAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGE)
TAG = "v1.0.0-pre.5"
ELF = b"\x7fELF" + b"synthetic-runtime" * 4


def put(path, content=b"fixture", executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755 if executable else 0o644)
    return path


class Fixture:
    def __init__(self, root):
        self.root = root
        self.source = root / "private-source"
        self.qt = root / "qt"
        self.build = root / "build"
        self.tools = root / "tools"
        self.output = root / "output"
        put(self.build / "mclauncher-qt", ELF, True)
        put(self.source / "qt-native/src/ui/Main.qml", b"PRIVATE_APPLICATION_QML_FIXTURE_DO_NOT_PACKAGE")
        put(self.source / "qt-native/src/branding/launcher-icon-256.png", b"png fixture")
        put(self.qt / "bin/qmake", b"qmake fixture", True)
        put(self.qt / "lib/cmake/Qt6Core/Qt6CoreConfigVersionImpl.cmake", b'set(PACKAGE_VERSION "6.8.3")\n')
        for library in ("Core", "Gui", "Network", "DBus", "Qml", "Quick", "QuickControls2"):
            put(self.qt / f"lib/libQt6{library}.so.6", ELF)
        for unused in ("PrintSupport", "Sql", "OpenGLWidgets"):
            put(self.qt / f"lib/libQt6{unused}.so.6", ELF)
        put(self.qt / "qml/QtQuick/LocalStorage/qmldir", b"module QtQuick.LocalStorage\n")
        put(self.qt / "lib/libicuuc.so.73.2", ELF)
        (self.qt / "lib/libicuuc.so.73").symlink_to("libicuuc.so.73.2")
        for plugin in (*PACKAGE.PLATFORM_PLUGINS, "libqoffscreen.so"):
            put(self.qt / "plugins/platforms" / plugin, ELF)
        put(self.qt / "plugins/imageformats/libqwebp.so", ELF)
        for module in ("QtQuick", "QtQuick/Controls", "QtQuick/Dialogs", "QtQuick/Layouts", "QtQuick/Templates"):
            put(self.qt / "qml" / module / "qmldir", f"module {module.replace('/', '.')}\n".encode())
        put(self.qt / "qml/QtQuick/Controls/Button.qml", b"import QtQuick\nItem {}\n")
        for module in PACKAGE.QT_MODULES:
            base = self.source / "qt-native/.tools/Qt/6.8.3/Src" / module
            put(base / ".cmake.conf", b'set(QT_REPO_MODULE_VERSION "6.8.3")\n')
            put(base / "LICENSES/LGPL-3.0-only.txt", b"Qt legal fixture")
        for name in ("third_party/miniz/LICENSE", "third_party/koring/LICENSE", "third_party/koring/README.md",
                     "third_party/fonts/roboto/OFL.txt", "third_party/fonts/roboto/README.md",
                     "third_party/fonts/material-symbols/LICENSE", "third_party/fonts/material-symbols/README.md"):
            put(self.source / "qt-native" / name, b"third-party notice")
        for name in (*PACKAGE.TOOL_NAMES, "runtime-x86_64"):
            put(self.tools / name, ELF, True)
        self.arguments = argparse.Namespace(source_root=str(self.source), build_dir=str(self.build),
                                            qt_root=str(self.qt), output_dir=str(self.output),
                                            tool_dir=str(self.tools), tag=TAG)
        self.commands = []
        self.packaged_files = {}
        self.fail_output = False

    def run(self, command, **kwargs):
        command = [str(item) for item in command]
        self.commands.append((command, kwargs))
        stdout = ""
        if command[0] == str(self.qt / "bin/qmake"):
            stdout = "6.8.3\n"
        elif command[:2] == ["/fixture/patchelf", "--print-rpath"]:
            stdout = "$ORIGIN/../lib\n"
        elif command[0] == str(self.tools / PACKAGE.TOOL_NAMES[0]):
            stage = Path(command[command.index("--appdir") + 1])
            if "--plugin" in command:
                desktop = Path(command[command.index("--desktop-file") + 1])
                icon = Path(command[command.index("--icon-file") + 1])
                shutil.copyfile(desktop, stage / desktop.name)
                shutil.copyfile(icon, stage / icon.name)
                # Simulate linuxdeploy's NEEDED closure and QML scanner output;
                # unreferenced SDK modules remain outside the AppDir.
                for library in ("Core", "Gui", "Network", "DBus", "Qml", "Quick", "QuickControls2"):
                    PACKAGE.copy_file(self.qt / f"lib/libQt6{library}.so.6",
                                      stage / f"usr/lib/libQt6{library}.so.6")
                PACKAGE.copy_file(self.qt / "lib/libicuuc.so.73.2", stage / "usr/lib/libicuuc.so.73.2")
                for path in sorted((self.qt / "qml").rglob("*")):
                    if path.is_file() and "LocalStorage" not in path.parts:
                        PACKAGE.copy_file(path, stage / "usr/qml" / path.relative_to(self.qt / "qml"))
            else:
                if self.fail_output:
                    raise subprocess.CalledProcessError(1, command)
                for path in stage.rglob("*"):
                    if path.is_file():
                        self.packaged_files[path.relative_to(stage).as_posix()] = path.read_bytes()
                data = bytearray(128)
                data[:4] = b"\x7fELF"
                data[8:11] = b"AI\x02"
                put(Path(kwargs["env"]["LDAI_OUTPUT"]), data)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def pack(self):
        with mock.patch.object(PACKAGE, "verify_tools") as verify, \
             mock.patch.object(PACKAGE.subprocess, "run", side_effect=self.run), \
             mock.patch.object(PACKAGE.shutil, "which", side_effect=lambda name: "/fixture/" + name), \
             mock.patch.object(PACKAGE, "system_library_paths", return_value={
                 "libssl.so.3": Path("/usr/lib/x86_64-linux-gnu/libssl.so.3"),
                 "libcrypto.so.3": Path("/usr/lib/x86_64-linux-gnu/libcrypto.so.3"),
             }), mock.patch.object(PACKAGE, "copy_system_legal_material"):
            result = PACKAGE.package(self.arguments)
            verify.assert_called_once_with(self.tools)
            return result


class PackageLinuxTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "AppRun is a Linux shell entry point")
    def test_real_apprun_preserves_host_library_environment_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            PACKAGE.write_documentation(stage, TAG)
            keys = ("LD_LIBRARY_PATH", "LD_PRELOAD", "MCL_HOST_LD_LIBRARY_PATH",
                    "MCL_HOST_LD_PRELOAD", "MCL_APPIMAGE_RUNTIME_ROOT", "QT_PLUGIN_PATH")
            stub = ("#!/usr/bin/python3\nimport json, os\nprint(json.dumps({k: os.environ.get(k) "
                    "for k in " + repr(keys) + "}))\n")
            put(stage / "usr/bin/mclauncher-qt", stub.encode(), executable=True)
            for original in (None, "", "/opt/host libraries"):
                environment = {k: v for k, v in os.environ.items()
                               if k not in keys and not k.startswith("MCL_HOST_")}
                environment["MCL_HOST_LD_LIBRARY_PATH"] = "stale inherited backup"
                environment["MCL_HOST_LD_PRELOAD"] = "stale inherited backup"
                if original is not None:
                    environment["LD_LIBRARY_PATH"] = original
                    environment["LD_PRELOAD"] = ""  # Empty is distinct from absent.
                result = subprocess.run([str(stage / "AppRun")], env=environment,
                                        check=True, capture_output=True, text=True)
                actual = json.loads(result.stdout)
                self.assertEqual(actual["MCL_APPIMAGE_RUNTIME_ROOT"], str(stage))
                self.assertEqual(actual["LD_LIBRARY_PATH"], str(stage / "usr/lib"))
                self.assertEqual(actual["QT_PLUGIN_PATH"], str(stage / "usr/plugins"))
                self.assertIsNone(actual["LD_PRELOAD"])
                self.assertEqual(actual["MCL_HOST_LD_LIBRARY_PATH"], original)
                self.assertEqual(actual["MCL_HOST_LD_PRELOAD"], None if original is None else "")

    def test_icu_soname_copy_retains_sdk_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Fixture(root)
            stage = root / "AppDir"
            put(stage / "usr/lib/libicuuc.so.73", ELF)
            inputs = PACKAGE.verify_deployed_icu(stage, fixture.qt)
            self.assertEqual(inputs[0]["sdkFile"], "libicuuc.so.73.2")
            self.assertEqual(inputs[0]["sdkSha256"], inputs[0]["deployedSha256"])
            put(stage / "usr/lib/libicuuc.so.73", ELF + b"patchelf changes")
            def readelf(command, **kwargs):
                text = ("Build ID: " + "a" * 40 + "\n" if command[1] == "--notes"
                        else "(SONAME) Library soname: [libicuuc.so.73]\n")
                return subprocess.CompletedProcess(command, 0, stdout=text)
            with mock.patch.object(PACKAGE.subprocess, "run", side_effect=readelf):
                inputs = PACKAGE.verify_deployed_icu(stage, fixture.qt)
            self.assertEqual(inputs[0]["elfBuildId"], "a" * 40)
            self.assertNotEqual(inputs[0]["sdkSha256"], inputs[0]["deployedSha256"])
            def mismatching(command, **kwargs):
                result = readelf(command, **kwargs)
                if command[1] == "--notes" and Path(command[2]).is_relative_to(stage):
                    result.stdout = "Build ID: " + "b" * 40 + "\n"
                return result
            with mock.patch.object(PACKAGE.subprocess, "run", side_effect=mismatching):
                with self.assertRaisesRegex(ValueError, "differs from the selected SDK build"):
                    PACKAGE.verify_deployed_icu(stage, fixture.qt)

    def test_runtime_prestaging_does_not_promote_unused_sdk_libraries_or_qml(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            stage = fixture.root / "AppDir"
            PACKAGE.copy_runtime(fixture.qt, stage / "usr")
            self.assertFalse((stage / "usr/lib").exists())
            self.assertFalse((stage / "usr/qml").exists())
            for name in (*PACKAGE.PLATFORM_PLUGINS, "libqoffscreen.so"):
                self.assertTrue((stage / "usr/plugins/platforms" / name).is_file())
            self.assertTrue((stage / "usr/plugins/imageformats/libqwebp.so").is_file())

    def test_legal_name_discovery_collects_documents_not_upstream_test_programs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = Fixture(root)
            sources = fixture.source / "qt-native/.tools/Qt/6.8.3/Src"
            lint = "tests/auto/qml/ecmascripttests/test262/tools/lint"
            programs = [("qtdeclarative", f"{lint}/test/fixtures/{name}.js") for name in (
                "license_alternate_1", "license_alternate_2", "license_alternate_3", "license_alternate_5",
                "license_generated", "license_invalid_year", "license_missing")]
            programs += [("qtdeclarative", f"{lint}/lib/checks/license.py"),
                         ("qtbase", "coin/instructions/prepare_license_test.yaml"),
                         ("qtbase", "coin/instructions/run_license_check.yaml")]
            for module, name in programs:
                put(sources / module / name, b"upstream executable test fixture, not a license document")
            documents = ("LICENSE", "LICENSE.txt", "AUTHORS.md", "LICENSE.chromium")
            for name in documents:
                put(sources / "qtdeclarative/third-party" / name, b"legal document")
            # Exact upstream attribution references need not use a conventional
            # document suffix, and must retain their ordinary-file/path checks.
            put(sources / "qtdeclarative/third-party/terms.custom", b"referenced license text")
            put(sources / "qtdeclarative/third-party/qt_attribution.json",
                json.dumps({"LicenseFile": "terms.custom"}).encode())
            stage = root / "stage"
            stage.mkdir()
            PACKAGE.copy_legal_material(fixture.source, stage)
            for module, name in programs:
                self.assertFalse((stage / "licenses/Qt" / module / name).exists(), name)
            for name in (*documents, "terms.custom", "qt_attribution.json"):
                self.assertTrue((stage / "licenses/Qt/qtdeclarative/third-party" / name).is_file(), name)
            self.assertTrue((stage / "licenses/Qt/qtdeclarative/LICENSES/LGPL-3.0-only.txt").is_file())
            PACKAGE.validate_stage(stage, fixture.qt)

    def test_pinned_tool_failure_precedes_all_external_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            with mock.patch.object(PACKAGE, "verify_tools", side_effect=ValueError("pin mismatch")) as verify, \
                 mock.patch.object(PACKAGE.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "pin mismatch"):
                    PACKAGE.deploy_appdir(fixture.output, fixture.root, fixture.source, fixture.qt,
                                          fixture.tools, "/fixture/strip", "/fixture/patchelf")
            verify.assert_called_once_with(fixture.tools)
            run.assert_not_called()

    def test_failed_tool_diagnostic_is_a_bounded_combined_tail(self):
        error = subprocess.CalledProcessError(1, ["linuxdeploy"],
                                              output="discard-this-prefix" + "x" * 20000,
                                              stderr="qt-plugin-directory-failure")
        captured = io.StringIO()
        with mock.patch.object(PACKAGE.os.sys, "stderr", captured):
            PACKAGE.report_tool_failure(error)
        heading, tail = captured.getvalue().split("\n", 1)
        self.assertIn("last 16 KiB", heading)
        self.assertNotIn("discard-this-prefix", tail)
        self.assertIn("qt-plugin-directory-failure", tail)
        self.assertLessEqual(len(tail.rstrip("\n").encode()), 16 * 1024)

    def test_appimage_stages_only_runtime_and_deploys_both_desktops(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            result = fixture.pack()
            self.assertEqual(result.name, f"LittleCraftLauncher-{TAG}-linux-x86_64.AppImage")
            self.assertTrue(os.access(result, os.X_OK))
            self.assertEqual(list(fixture.output.iterdir()), [result])
            files = fixture.packaged_files
            self.assertFalse(any(b"PRIVATE_APPLICATION_QML_FIXTURE_DO_NOT_PACKAGE" in data for data in files.values()))
            self.assertEqual(files["usr/bin/mclauncher-qt"], ELF)
            for unused in ("PrintSupport", "Sql", "OpenGLWidgets"):
                self.assertNotIn(f"usr/lib/libQt6{unused}.so.6", files)
            self.assertNotIn("usr/qml/QtQuick/LocalStorage/qmldir", files)
            self.assertIn("licenses/Qt/qtwayland/LICENSES/LGPL-3.0-only.txt", files)
            self.assertIn("SUPPORTING_MATERIALS.zip", files)
            self.assertEqual(json.loads(files["runtime-manifest.json"]), {
                "schema": 2, "version": TAG, "platform": "linux-x86_64", "format": "appimage",
                "qt": "6.8.3", "linkage": "dynamic", "displayBackends": ["xcb", "wayland"],
                "application": "usr/bin/mclauncher-qt",
            })
            self.assertIn(b"Prefix=..", files["usr/bin/qt.conf"])
            self.assertIn(b"--appimage-extract", files["DYNAMIC-RUNTIME.txt"])
            deploy = next((cmd, opts) for cmd, opts in fixture.commands if "--plugin" in cmd)
            self.assertIn("--executable", deploy[0])
            self.assertEqual(deploy[0].count("--library"), 2)
            self.assertEqual(deploy[1]["env"]["QMAKE"], str(fixture.qt / "bin/qmake"))
            self.assertEqual(set(deploy[1]["env"]["EXTRA_PLATFORM_PLUGINS"].split(";")), set(PACKAGE.PLATFORM_PLUGINS))
            output = next(opts for cmd, opts in fixture.commands if "--output" in cmd)
            self.assertEqual(output["env"]["LDAI_RUNTIME_FILE"], str(fixture.tools / "runtime-x86_64"))
            self.assertEqual(output["env"]["LDAI_VERSION"], TAG)
            self.assertEqual(output["env"]["LDAI_NO_APPSTREAM"], "1")
            self.assertEqual(output["env"]["APPIMAGE_EXTRACT_AND_RUN"], "1")
            self.assertTrue(any(cmd[1:3] == ["--set-rpath", "$ORIGIN/../lib"] for cmd, _ in fixture.commands))

    def test_failed_output_never_publishes_partial_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.fail_output = True
            with self.assertRaises(subprocess.CalledProcessError):
                fixture.pack()
            self.assertEqual(list(fixture.output.iterdir()), [])

    def test_wayland_must_be_present_before_tools_run(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            (fixture.qt / "plugins/platforms/libqwayland-generic.so").unlink()
            with self.assertRaisesRegex(ValueError, "required Qt runtime"):
                fixture.pack()
            self.assertFalse(any("--plugin" in cmd for cmd, _ in fixture.commands))

    def test_stage_rejects_private_sources_and_link_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Fixture(root)
            stage = root / "AppDir"
            PACKAGE.copy_runtime(fixture.qt, stage / "usr")
            PACKAGE.validate_stage(stage, fixture.qt)
            private = put(stage / "usr/qml/Private/Main.qml", b"PRIVATE_APPLICATION_QML_FIXTURE_DO_NOT_PACKAGE")
            with self.assertRaisesRegex(ValueError, "selected Qt SDK"):
                PACKAGE.validate_stage(stage, fixture.qt)
            private.unlink()
            outside = put(stage / "unexpected/Main.qml", b"PRIVATE_APPLICATION_QML_FIXTURE_DO_NOT_PACKAGE")
            with self.assertRaisesRegex(ValueError, "Application QML sources") as error:
                PACKAGE.validate_stage(stage, fixture.qt)
            self.assertIn("unexpected/Main.qml", str(error.exception))
            self.assertNotIn(str(stage), str(error.exception))
            outside.unlink()
            source = put(stage / "launcher.cpp", b"PRIVATE_APPLICATION_QML_FIXTURE_DO_NOT_PACKAGE")
            with self.assertRaisesRegex(ValueError, "forbidden source"):
                PACKAGE.validate_stage(stage, fixture.qt)
            source.unlink()
            (stage / "escape").symlink_to(fixture.source, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "link escapes"):
                PACKAGE.validate_stage(stage, fixture.qt)

    def test_deployment_environment_ignores_external_paths(self):
        hostile = {"QT_PLUGIN_PATH": "/host/plugins", "QT_QPA_PLATFORM_PLUGIN_PATH": "/host/qpa",
                   "QML_IMPORT_PATH": "/private/qml", "QML_MODULES_PATHS": "/private/modules",
                   "LD_LIBRARY_PATH": "/host/lib:", "LD_PRELOAD": "/host/inject.so",
                   "LDAI_OUTPUT": "/host/output", "QT_QPA_PLATFORM": "wayland", "DISPLAY": ":2"}
        with mock.patch.dict(os.environ, hostile, clear=True):
            environment = PACKAGE.deployment_environment(Path("/sdk"), Path("/tools"), Path("/source"))
        self.assertEqual(environment["LD_LIBRARY_PATH"], "/sdk/lib")
        self.assertEqual(environment["QML_SOURCES_PATHS"], "/source/qt-native/src/ui")
        for key in ("LD_PRELOAD", "LDAI_OUTPUT", "QT_PLUGIN_PATH", "QML_MODULES_PATHS"):
            self.assertNotIn(key, environment)
        self.assertEqual(environment["QT_QPA_PLATFORM"], "wayland")
        self.assertEqual(environment["DISPLAY"], ":2")

    def test_apprun_isolates_child_and_preserves_display_input_platform_and_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "AppDir with spaces"
            put(stage / "licenses/NOTICE", b"notice")
            PACKAGE.write_documentation(stage, TAG)
            put(stage / "usr/bin/mclauncher-qt", b"#!/usr/bin/python3\nimport json,os,sys\nprint(json.dumps({'env':dict(os.environ),'args':sys.argv[1:]}))\n", True)
            environment = dict(os.environ, QT_PLUGIN_PATH="/external", QML_IMPORT_PATH="/external",
                               QML2_IMPORT_PATH="/external", QT_QPA_PLATFORM_PLUGIN_PATH="/external",
                               LD_LIBRARY_PATH="/external:", LD_PRELOAD="", QT_QPA_PLATFORM="wayland",
                               DISPLAY=":5", WAYLAND_DISPLAY="wayland-5", QT_IM_MODULE="ibus", XMODIFIERS="@im=ibus")
            before = dict(environment)
            result = subprocess.run([str(stage / "AppRun"), "--data-root", "a directory", "$(literal)"],
                                    env=environment, check=True, capture_output=True, text=True)
            child = json.loads(result.stdout)
            self.assertEqual(environment, before)
            self.assertEqual(child["args"], ["--data-root", "a directory", "$(literal)"])
            for key in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM", "QT_IM_MODULE", "XMODIFIERS"):
                self.assertEqual(child["env"][key], environment[key])
            self.assertEqual(child["env"]["LD_LIBRARY_PATH"], str(stage / "usr/lib"))
            self.assertEqual(child["env"]["QT_PLUGIN_PATH"], str(stage / "usr/plugins"))
            self.assertEqual(child["env"]["QT_QPA_PLATFORM_PLUGIN_PATH"], str(stage / "usr/plugins/platforms"))
            self.assertEqual(child["env"]["QML_IMPORT_PATH"], str(stage / "usr/qml"))
            self.assertNotIn("LD_PRELOAD", child["env"])
            with zipfile.ZipFile(stage / "SUPPORTING_MATERIALS.zip") as archive:
                self.assertIn("licenses/NOTICE", archive.namelist())
                self.assertNotIn("runtime-manifest.json", archive.namelist())

    def test_generated_apprun_does_not_write_through_old_application_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            binary = put(stage / "usr/bin/mclauncher-qt", ELF)
            (stage / "AppRun").symlink_to("usr/bin/mclauncher-qt")
            PACKAGE.write_documentation(stage, TAG)
            self.assertEqual(binary.read_bytes(), ELF)
            self.assertFalse((stage / "AppRun").is_symlink())

    def test_system_library_notices_include_package_source_and_standard_licenses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage, qt, docs, common = (root / name for name in ("AppDir", "qt", "doc", "common"))
            (qt / "lib").mkdir(parents=True)
            origin = put(root / "system/libssl.so.3", ELF)
            put(stage / "usr/lib/libssl.so.3", ELF)
            put(docs / "libssl3/copyright", b"Ubuntu OpenSSL copyright; see common licenses")
            put(common / "Apache-2.0", b"standard license fixture")
            def query(command, **kwargs):
                if command[1] == "-S":
                    return subprocess.CompletedProcess(command, 0, stdout=f"libssl3:amd64: {origin}\n")
                return subprocess.CompletedProcess(command, 0, stdout="libssl3:amd64\t3.0.2-1ubuntu1\topenssl\t3.0.2-1ubuntu1\n")
            with mock.patch.object(PACKAGE, "system_library_paths", return_value={"libssl.so.3": origin}), \
                 mock.patch.object(PACKAGE.subprocess, "run", side_effect=query):
                PACKAGE.copy_system_legal_material(stage, qt, docs, common)
            manifest = json.loads((stage / "licenses/system-libraries/manifest.json").read_text())
            self.assertEqual(manifest["packages"][0]["sourcePackage"], "openssl")
            self.assertEqual(manifest["packages"][0]["bundledFiles"], ["usr/lib/libssl.so.3"])
            self.assertEqual((stage / "licenses/system-libraries/common-licenses/Apache-2.0").read_bytes(), b"standard license fixture")


if __name__ == "__main__":
    unittest.main()
