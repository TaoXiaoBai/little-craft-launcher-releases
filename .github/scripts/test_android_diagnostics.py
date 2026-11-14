#!/usr/bin/env python3
"""Exercise the actual Android shell steps with owned, non-network tools."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parent.parent / "workflows/android-build.yml"
WINDOWS_WORKFLOW = WORKFLOW.with_name("windows-build.yml")
PRIVATE_MARKER = "synthetic-private-compiler-source-line"


def step(name, workflow=WORKFLOW):
    lines = workflow.read_text(encoding="utf-8").splitlines()
    begin = lines.index(f"      - name: {name}") + 1
    end = next((i for i in range(begin, len(lines))
                if lines[i].startswith("      - name:")), len(lines))
    return lines[begin:end]


def step_script(name):
    lines = step(name)
    start = lines.index("        run: |") + 1
    # Github expands inputs before invoking the shell. This fixture never
    # uploads an APK and does not need the workflow expression engine.
    return "\n".join(line[10:] if line.startswith("          ") else line
                     for line in lines[start:]).replace("${{ inputs.upload_apk }}", "false") + "\n"


FIXTURE = r'''
import os
from pathlib import Path
import sys
from zipfile import ZipFile
command = Path(sys.argv[0]).name
if command == "git":
    print("b" * 12)
    raise SystemExit(0)
stage = ("build" if "--build" in sys.argv else "configure") if command == "cmake" else (
    "test" if command == "ctest" else "apk")
with Path(os.environ["FIXTURE_CALLS"]).open("a") as log:
    log.write(stage + "\n")
print("synthetic-private-compiler-source-line stdout " + stage)
print("synthetic-private-compiler-source-line stderr " + stage, file=sys.stderr)
if os.environ.get("FIXTURE_FAIL") == stage:
    raise SystemExit(9)
if stage == "apk":
    root = Path(sys.argv[sys.argv.index("--build-dir") + 1])
    root.mkdir(parents=True)
    with ZipFile(root / "owned-debug.apk", "w") as archive:
        for library in os.environ.get("FIXTURE_LIBRARIES", "ssl,crypto").split(","):
            if library:
                archive.writestr("lib/arm64-v8a/lib" + library + "_3.so", b"owned TLS fixture")
'''


SHELL_TOOLS = all(shutil.which(name) for name in ("bash", "unzip", "sha256sum", "stat"))


@unittest.skipUnless(os.name == "posix" and SHELL_TOOLS, "Android job runs on Linux")
class AndroidPrivateOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="android private output ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in ("cmake", "ctest", "python3", "git"):
            script = self.tools / name
            script.write_text("#!" + sys.executable + "\n" + textwrap.dedent(FIXTURE))
            script.chmod(0o755)
        self.environment = dict(os.environ, PATH=str(self.tools) + os.pathsep + os.environ["PATH"],
                                RUNNER_TEMP=str(self.root / "runtime"), QT_VERSION="6.8.3",
                                ANDROID_SDK_ROOT=str(self.root / "sdk"), ANDROID_NDK_VERSION="26.1.10909125",
                                JAVA_HOME_17_X64=str(self.root / "jdk"),
                                GITHUB_STEP_SUMMARY=str(self.root / "summary.md"),
                                FIXTURE_CALLS=str(self.root / "calls.txt"))

    def execute(self, name, *, fail="", libraries="ssl,crypto"):
        environment = dict(self.environment, FIXTURE_FAIL=fail, FIXTURE_LIBRARIES=libraries)
        result = subprocess.run([shutil.which("bash"), "-euo", "pipefail"],
                                input=step_script(name), text=True, cwd=self.root,
                                env=environment, capture_output=True, timeout=15)
        self.assertNotIn(PRIVATE_MARKER, result.stdout + result.stderr)
        return result

    def calls(self):
        return (self.root / "calls.txt").read_text().splitlines()

    def assert_private_log(self, name):
        content = (self.root / "runtime/private-build-logs" / name).read_text()
        self.assertIn(PRIVATE_MARKER + " stdout", content)
        self.assertIn(PRIVATE_MARKER + " stderr", content)

    def test_host_success_runs_configuration_build_and_all_ctest(self):
        result = self.execute("Build and run host client tests")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), ["configure", "build", "test"])
        self.assert_private_log("android-host.log")

    def test_host_failures_stop_later_commands_without_printing_private_output(self):
        expected = {"configure": ["configure"], "build": ["configure", "build"],
                    "test": ["configure", "build", "test"]}
        for failed, calls in expected.items():
            with self.subTest(stage=failed):
                path = self.root / "calls.txt"
                if path.exists():
                    path.unlink()
                result = self.execute("Build and run host client tests", fail=failed)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Android host tests failed", result.stdout)
                self.assertEqual(self.calls(), calls)
                self.assert_private_log("android-host.log")

    def test_apk_compile_failure_remains_private_and_does_not_stage_a_download(self):
        result = self.execute("Build debug APK", fail="apk")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("APK build or validation failed", result.stdout)
        self.assertFalse((self.root / "runtime/apk-out").exists())
        self.assert_private_log("android-apk.log")

    def test_apk_success_requires_both_tls_libraries(self):
        result = self.execute("Build debug APK")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), ["apk"])
        self.assertEqual(len(list((self.root / "runtime/apk-out").glob("*.apk"))), 1)
        self.assert_private_log("android-apk.log")
        summary = (self.root / "summary.md").read_text()
        self.assertIn("SHA-256", summary)
        self.assertNotIn(PRIVATE_MARKER, summary)

    def test_apk_rejects_missing_crypto_library(self):
        result = self.execute("Build debug APK", libraries="ssl")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "summary.md").exists())

    def test_apk_rejects_missing_ssl_library(self):
        result = self.execute("Build debug APK", libraries="crypto")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "summary.md").exists())


class AndroidDiagnosticArtifactTests(unittest.TestCase):
    def test_tools_checkout_precedes_private_source_and_keeps_no_credentials(self):
        content = WORKFLOW.read_text()
        self.assertLess(content.index("name: Checkout public diagnostic tools"),
                        content.index("name: Checkout source"))
        self.assertIn("          persist-credentials: false", step("Checkout public diagnostic tools"))
        self.assertIn("          persist-credentials: false", step("Checkout source"))

    def test_encryption_always_uses_only_the_public_certificate(self):
        block = "\n".join(step("Encrypt private Android diagnostics"))
        self.assertIn("if: always()", block)
        self.assertIn("builder/.github/scripts/encrypt-diagnostics.py", block)
        self.assertIn("--certificate builder/.github/scripts/diagnostics-certificate.pem", block)
        self.assertIn('--logs-dir "${RUNNER_TEMP}/private-build-logs"', block)
        self.assertNotIn("-inkey", block)

    def test_only_safe_summary_ciphertext_and_opt_in_apk_are_uploaded(self):
        encrypted = "\n".join(step("Retain encrypted Android diagnostics only"))
        self.assertIn("if: always()", encrypted)
        self.assertIn("path: ${{ runner.temp }}/diagnostics-android.p7m", encrypted)
        self.assertIn("retention-days: 3", encrypted)
        apk = "\n".join(step("Upload debug APK"))
        self.assertIn("if: success() && inputs.upload_apk", apk)
        self.assertIn("path: ${{ runner.temp }}/apk-out/*.apk", apk)
        uploads = [line.strip() for line in WORKFLOW.read_text().splitlines()
                   if line.strip().startswith("path:")]
        self.assertEqual(uploads, ["path: builder", "path: source",
                                  "path: ${{ runner.temp }}/safe-diagnostics/android/summary.json",
                                  "path: ${{ runner.temp }}/diagnostics-android.p7m",
                                  "path: ${{ runner.temp }}/apk-out/*.apk"])


class WindowsDiagnosticArtifactTests(unittest.TestCase):
    def test_windows_keeps_dynamic_full_suite_and_private_capture(self):
        block = "\n".join(step("Build and test source", WINDOWS_WORKFLOW))
        self.assertIn("'-Mode', 'dynamic'", block)
        self.assertNotIn("-TestShard", block)
        self.assertIn('*> $log', block)
        self.assertIn("'mcl-ci-evidence-dynamic'", block)
        self.assertIn("'ctest'", block)

    def test_windows_tools_are_public_and_access_only_does_not_build_or_encrypt(self):
        block = "\n".join(step("Checkout public diagnostic tools", WINDOWS_WORKFLOW))
        self.assertIn("if: ${{ !inputs.verify_access_only }}", block)
        self.assertIn("persist-credentials: false", block)
        for name in ("Encrypt private Windows diagnostics", "Retain encrypted Windows diagnostics only"):
            with self.subTest(step=name):
                self.assertIn("if: ${{ always() && !inputs.verify_access_only }}",
                              "\n".join(step(name, WINDOWS_WORKFLOW)))

    def test_windows_diagnostics_use_public_cert_and_only_ciphertext_is_uploaded(self):
        encrypt = "\n".join(step("Encrypt private Windows diagnostics", WINDOWS_WORKFLOW))
        self.assertIn("builder/.github/scripts/encrypt-diagnostics.py", encrypt)
        self.assertIn("--certificate builder/.github/scripts/diagnostics-certificate.pem", encrypt)
        self.assertNotIn("-inkey", encrypt)
        artifact = "\n".join(step("Retain encrypted Windows diagnostics only", WINDOWS_WORKFLOW))
        self.assertIn("path: ${{ runner.temp }}/diagnostics-windows-build.p7m", artifact)
        self.assertIn("retention-days: 3", artifact)
        paths = [line.strip() for line in WINDOWS_WORKFLOW.read_text().splitlines()
                 if line.strip().startswith("path:")]
        self.assertEqual(paths, ["path: builder", "path: source",
                                 "path: ${{ runner.temp }}/mcl-ci-archives",
                                 "path: ${{ runner.temp }}/safe-diagnostics/windows-build/summary.json",
                                 "path: ${{ runner.temp }}/diagnostics-windows-build.p7m"])


if __name__ == "__main__":
    unittest.main()
