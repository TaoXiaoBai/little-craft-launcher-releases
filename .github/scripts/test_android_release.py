#!/usr/bin/env python3
"""The Android package is an optional third download of a prerelease."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))
import release_support as SUPPORT  # noqa: E402

SPEC = importlib.util.spec_from_file_location("android_release", SCRIPT_ROOT / "android_release.py")
A = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(A)

REPOSITORY = "owner/public-releases"
TAG = "v1.0.0-pre.16"
CERTIFICATE = "c" * 64
DEFINITION = "d" * 40
WORKFLOWS = SCRIPT_ROOT.parent / "workflows"


def desktop_release(extra_assets=(), extra_body=""):
    names = SUPPORT.runtime_names(TAG)
    hashes = {name: f"{index + 1:064x}" for index, name in enumerate(names)}
    assets = [{"name": name, "state": "uploaded", "size": 10, "digest": "sha256:" + hashes[name]}
              for name in names]
    return {"tag_name": TAG, "draft": False, "prerelease": True,
            "body": SUPPORT.release_notes(REPOSITORY, TAG, hashes) + extra_body,
            "assets": assets + list(extra_assets)}


class Github:
    """Owned stand-in for the gh commands the publisher runs."""

    def __init__(self, release, digest=None):
        self.release = release
        self.digest = digest
        self.commands = []

    def __call__(self, command, **options):
        self.commands.append(command[1:3])
        if command[1] == "api":
            return subprocess.CompletedProcess(command, 0, json.dumps(self.release), "")
        if command[1:3] == ["release", "upload"]:
            apk = Path(command[4])
            self.release["assets"].append({
                "name": apk.name, "state": "uploaded", "size": apk.stat().st_size,
                "digest": "sha256:" + (self.digest or A.checksum(apk))})
        elif command[1:3] == ["release", "edit"]:
            self.release["body"] = Path(command[command.index("--notes-file") + 1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")


class AndroidReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="android release ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "package"
        self.root.mkdir()
        self.apk = self.root / SUPPORT.android_name(TAG)

    def package(self, skip=None):
        with zipfile.ZipFile(self.apk, "w") as archive:
            for entry in A.REQUIRED_ENTRIES:
                if entry != skip:
                    archive.writestr(entry, b"owned fixture")
        metadata = A.describe(TAG, self.apk, CERTIFICATE, DEFINITION)
        (self.root / A.METADATA).write_text(json.dumps(metadata), encoding="utf-8")
        return metadata

    def test_version_code_grows_with_every_kind_of_release(self):
        order = ["v1.0.0-alpha.1", "v1.0.0-beta.1", "v1.0.0-pre.15", "v1.0.0-pre.16", "v1.0.0-rc.1",
                 "v1.0.1-pre.1", "v1.1.0-alpha.1", "v2.0.0-pre.1"]
        codes = [SUPPORT.android_version(tag)[1] for tag in order]
        self.assertEqual(codes, sorted(set(codes)))
        self.assertEqual(SUPPORT.android_version(TAG), ("1.0.0-pre.16", 100003016))
        self.assertLess(SUPPORT.android_version("v20.99.99-rc.999")[1], 2100000000)
        for tag in ("v1.0.0", "1.0.0-pre.1", "v1.100.0-pre.1", "v1.0.0-pre.1000", "v21.0.0-pre.1"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                SUPPORT.android_version(tag)

    def test_attach_adds_the_package_and_its_notes_to_the_desktop_release(self):
        metadata = self.package()
        github = Github(desktop_release())
        before = github.release["body"]
        self.assertEqual(A.attach(REPOSITORY, TAG, self.root, run=github), metadata)
        self.assertEqual(github.commands, [["api", f"repos/{REPOSITORY}/releases/tags/{TAG}"],
                                           ["release", "upload"], ["release", "edit"],
                                           ["api", f"repos/{REPOSITORY}/releases/tags/{TAG}"]])
        body = github.release["body"]
        self.assertTrue(body.startswith(before.rstrip("\n")))
        self.assertIn(f"| {self.apk.name} | `{metadata['sha256']}` |", body)
        self.assertIn(f"https://github.com/{REPOSITORY}/tree/{DEFINITION}", body)
        self.assertIn(CERTIFICATE, body)
        self.assertIn("LGPL-3.0", body)
        # Later cleanup accepts the third download; the desktop publication never does.
        SUPPORT.verify_published_assets(TAG, github.release["assets"], body, allow_android=True)
        with self.assertRaises(ValueError):
            SUPPORT.verify_published_assets(TAG, github.release["assets"], body)

    def test_attach_never_touches_an_unexpected_release(self):
        self.package()
        occupied = {"name": self.apk.name, "state": "uploaded", "size": 1, "digest": "sha256:" + "e" * 64}
        cases = {
            "draft": dict(desktop_release(), draft=True),
            "full release": dict(desktop_release(), prerelease=False),
            "other version": dict(desktop_release(), tag_name="v1.0.0-pre.15"),
            "already attached": desktop_release([occupied]),
            "already described": desktop_release(extra_body=self.apk.name),
            "missing desktop package": dict(desktop_release(), assets=desktop_release()["assets"][:1]),
        }
        for name, release in cases.items():
            with self.subTest(name):
                github = Github(release)
                with self.assertRaises(ValueError):
                    A.attach(REPOSITORY, TAG, self.root, run=github)
                self.assertEqual(github.commands, [["api", f"repos/{REPOSITORY}/releases/tags/{TAG}"]])

    def test_attach_reports_a_changed_upload(self):
        self.package()
        with self.assertRaises(ValueError):
            A.attach(REPOSITORY, TAG, self.root, run=Github(desktop_release(), digest="f" * 64))

    def test_package_must_match_its_build_record(self):
        self.package()
        A.validate_package(self.root, TAG)
        record = self.root / A.METADATA
        original = json.loads(record.read_text())
        for key, value in (("sha256", "0" * 64), ("size", original["size"] + 1), ("tag", "v1.0.0-pre.17"),
                           ("runtime_definition", "main"), ("certificate_sha256", ""), ("schema", 2)):
            with self.subTest(key=key):
                record.write_text(json.dumps(dict(original, **{key: value})))
                with self.assertRaises(ValueError):
                    A.validate_package(self.root, TAG)
        record.write_text(json.dumps(original))
        (self.root / "extra.txt").write_text("unexpected")
        with self.assertRaises(ValueError):
            A.validate_package(self.root, TAG)

    def test_incomplete_or_damaged_package_is_rejected(self):
        self.package(skip="lib/arm64-v8a/libpojavexec.so")
        with self.assertRaises(ValueError):
            A.validate_package(self.root, TAG)
        self.apk.write_bytes(b"not an archive")
        (self.root / A.METADATA).write_text(json.dumps(A.describe(TAG, self.apk, CERTIFICATE, DEFINITION)))
        with self.assertRaises(ValueError):
            A.validate_package(self.root, TAG)

    def test_cli_prints_identity_and_only_fixed_errors(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(A.main(["version", "--tag", TAG]), 0)
        self.assertEqual(output.getvalue(), f"{self.apk.name} 1.0.0-pre.16 100003016\n")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(A.main(["version", "--tag", "v1.0.0; echo injected"]), 1)
        self.assertEqual(errors.getvalue(), "ANDROID_RELEASE_REJECTED: A prerelease tag is required\n")

    def test_describe_writes_a_record_the_publisher_accepts(self):
        with zipfile.ZipFile(self.apk, "w") as archive:
            for entry in A.REQUIRED_ENTRIES:
                archive.writestr(entry, b"owned fixture")
        self.assertEqual(A.main(["describe", "--tag", TAG, "--apk", str(self.apk),
                                 "--certificate", CERTIFICATE, "--runtime-definition", DEFINITION]), 0)
        self.assertEqual(A.validate_package(self.root, TAG)["certificate_sha256"], CERTIFICATE)


class AndroidReleaseWorkflowTests(unittest.TestCase):
    def test_desktop_publication_neither_waits_for_nor_collects_android(self):
        text = (WORKFLOWS / "prerelease.yml").read_text(encoding="utf-8")
        publish = text[text.index("\n  publish:"):text.index("\n  # Android is an additional download")]
        self.assertIn("needs: [build-windows, build-linux]", publish)
        self.assertNotIn("android", publish.lower())
        self.assertIn("pattern: release-*", publish)
        build = (WORKFLOWS / "android-build.yml").read_text(encoding="utf-8")
        self.assertIn("name: android-package-${{ inputs.release_tag }}", build)
        self.assertNotIn("name: release-", build)

    def test_android_is_attached_only_after_publication_and_only_when_signed(self):
        text = (WORKFLOWS / "prerelease.yml").read_text(encoding="utf-8")
        build = text[text.index("\n  build-android:"):text.index("\n  publish-android:")]
        self.assertIn("uses: ./.github/workflows/android-build.yml", build)
        self.assertIn("needs: resolve-source", build)
        self.assertIn("(inputs.platform == 'both' || !inputs.platform)", build)
        self.assertIn("release_tag: ${{ inputs.tag }}", build)
        attach = text[text.index("\n  publish-android:"):]
        self.assertIn("needs: [publish, build-android]", attach)
        self.assertIn("needs.publish.result == 'success'", attach)
        self.assertIn("needs.build-android.outputs.signed == 'true'", attach)
        self.assertIn("persist-credentials: false", attach)

    def test_signing_material_never_reaches_a_log_or_an_artifact(self):
        text = (WORKFLOWS / "android-build.yml").read_text(encoding="utf-8")
        begin = text.index("      - name: Build the APK with the game runtime")
        step = text[begin:text.index("      - name: Write safe CI summary (android)")]
        self.assertEqual(text.count("secrets.ANDROID_"), 4)
        self.assertEqual(step.count("secrets.ANDROID_"), 4)
        self.assertIn("--ks-pass env:ANDROID_KEYSTORE_PASSWORD --key-pass env:ANDROID_KEY_PASSWORD", step)
        self.assertIn("""trap 'rm -rf "${keys}"' EXIT""", step)
        self.assertIn("-PlclUnsigned", step)
        self.assertIn("CN=Android Debug", step)
        # User-controlled inputs reach shell steps only through the environment.
        for line in text.splitlines():
            if "${{ inputs.release_tag" in line or "${{ inputs.source_tag" in line:
                self.assertRegex(line.strip(), r"^(group|ref|name|[A-Z_]+): ")


if __name__ == "__main__":
    unittest.main()
