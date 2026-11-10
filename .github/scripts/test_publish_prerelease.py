#!/usr/bin/env python3
"""Verify tiny real archives and mocked publication without network access."""

import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile


SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))
import release_support as SUPPORT

SPEC = importlib.util.spec_from_file_location(
    "publish_prerelease", SCRIPT_ROOT / "publish-prerelease.py"
)
PUBLISH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PUBLISH)

REPOSITORY = "example/launcher-releases"
TAG = "v1.0.0-pre.4"
TARGET = "a" * 40
WINDOWS, LINUX = SUPPORT.runtime_names(TAG)
LINUX_ROOT = LINUX.removesuffix(".tar.gz")


def tiny_zip(entries):
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries:
                archive.writestr(name, content)
    return output.getvalue()


class ArchiveFixture:
    def __init__(self, root):
        self.root = root
        self.windows_entries = [
            ("runtime-manifest.json", json.dumps({
                "schema": 1, "version": TAG, "platform": "windows-x64", "linkage": "dynamic",
            }).encode()),
            ("mclauncher-qt.exe", b"MZsynthetic-test-executable"),
            ("SUPPORTING_MATERIALS.zip", tiny_zip([("NOTICE.txt", b"test legal material")])),
        ]
        self.linux_entries = [
            (f"{LINUX_ROOT}/mclauncher-qt", b"\x7fELFsynthetic-test-executable"),
            (f"{LINUX_ROOT}/LittleCraftLauncher", b"#!/bin/sh\nexec ./mclauncher-qt\n"),
            (f"{LINUX_ROOT}/SUPPORTING_MATERIALS.zip", tiny_zip([("NOTICE.txt", b"test legal material")])),
        ]
        self.write_windows()
        self.write_linux()
        self.qt_pins = {}
        for index, name in enumerate(SUPPORT.QT_SOURCE_ARCHIVES):
            payload = tiny_zip([("LICENSE.txt", f"synthetic upstream archive {index}".encode())])
            (root / name).write_bytes(payload)
            self.qt_pins[name] = (len(payload), hashlib.sha256(payload).hexdigest())

    def write_windows(self, entries=None):
        (self.root / WINDOWS).write_bytes(tiny_zip(self.windows_entries if entries is None else entries))

    def write_linux(self, entries=None, symlinks=()):
        with tarfile.open(self.root / LINUX, "w:gz") as archive:
            for name, content in self.linux_entries if entries is None else entries:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o755 if name.endswith(("mclauncher-qt", "LittleCraftLauncher")) else 0o644
                archive.addfile(info, io.BytesIO(content))
            for name, target in symlinks:
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                archive.addfile(info)


class MockGitHub:
    def __init__(self, hashes, mutate=None, preceding_releases=0):
        self.hashes = hashes
        self.mutate = mutate
        self.preceding_releases = preceding_releases
        self.calls = []
        self.release = None
        self.notes = None

    def __call__(self, command, **options):
        self.calls.append((list(command), dict(options)))
        if command[:3] == ["gh", "release", "create"]:
            self.notes = Path(command[command.index("--notes-file") + 1]).read_text(encoding="utf-8")
            self.release = {
                "id": 31, "tag_name": TAG, "draft": True, "prerelease": True, "body": self.notes,
                "assets": [{"id": 41 + index, "name": name, "size": 100 + index,
                            "state": "uploaded", "digest": "sha256:" + digest}
                           for index, (name, digest) in enumerate(self.hashes.items())],
            }
            if self.mutate:
                self.mutate(self.release)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:2] == ["gh", "api"]:
            prefix = f"repos/{REPOSITORY}/releases?per_page=100&page="
            if len(command) != 3 or not command[2].startswith(prefix):
                raise AssertionError("Publication must discover its draft through release inventory")
            page = int(command[2].removeprefix(prefix))
            inventory = [{"tag_name": f"v9.0.0-pre.{index}"}
                         for index in range(self.preceding_releases)] + [self.release]
            return subprocess.CompletedProcess(command, 0,
                                               stdout=json.dumps(inventory[(page - 1) * 100:page * 100]),
                                               stderr="")
        if command[:3] == ["gh", "release", "edit"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected external command: {command}")

    @property
    def edits(self):
        return [command for command, _ in self.calls if command[:3] == ["gh", "release", "edit"]]


class ArchiveAndPublishTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="release fixture with spaces-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = ArchiveFixture(self.root)
        self.patch = mock.patch.object(PUBLISH, "QT_SOURCE_ARCHIVES", self.fixture.qt_pins)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def invalid_inputs_do_not_invoke_github(self):
        runner = mock.Mock(side_effect=AssertionError("Invalid artifacts reached GitHub"))
        with self.assertRaises((ValueError, KeyError, zipfile.BadZipFile, tarfile.TarError)):
            PUBLISH.publish(REPOSITORY, TAG, self.root, TARGET, run=runner)
        runner.assert_not_called()

    def fixture_hashes(self):
        return {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                for name in (WINDOWS, LINUX)}

    def test_real_zip_and_tar_with_four_pinned_inputs_validate(self):
        self.assertEqual(PUBLISH.validate_inputs(self.root, TAG), self.fixture_hashes())

    def test_missing_qt_input_rejects_before_github(self):
        (self.root / next(iter(self.fixture.qt_pins))).unlink()
        self.invalid_inputs_do_not_invoke_github()

    def test_missing_runtime_rejects_before_github(self):
        (self.root / LINUX).unlink()
        self.invalid_inputs_do_not_invoke_github()

    def test_extra_input_rejects_before_github(self):
        (self.root / "unexpected.txt").write_text("extra")
        self.invalid_inputs_do_not_invoke_github()

    def test_symlink_input_rejects_before_github(self):
        qt = self.root / next(iter(self.fixture.qt_pins))
        outside = self.root.parent / (self.root.name + "-outside.zip")
        outside.write_bytes(qt.read_bytes())
        self.addCleanup(outside.unlink)
        qt.unlink()
        qt.symlink_to(outside)
        self.invalid_inputs_do_not_invoke_github()

    def test_wrong_qt_checksum_rejects_even_when_length_matches(self):
        qt = self.root / next(iter(self.fixture.qt_pins))
        payload = bytearray(qt.read_bytes())
        payload[-1] ^= 1
        qt.write_bytes(payload)
        self.invalid_inputs_do_not_invoke_github()

    def test_wrong_qt_size_rejects_before_github(self):
        qt = self.root / next(iter(self.fixture.qt_pins))
        qt.write_bytes(qt.read_bytes() + b"extra")
        self.invalid_inputs_do_not_invoke_github()

    def test_wrong_windows_version_rejects_before_github(self):
        manifest = json.loads(self.fixture.windows_entries[0][1])
        manifest["version"] = "v2.0.0-pre.1"
        self.fixture.write_windows([("runtime-manifest.json", json.dumps(manifest).encode()),
                                    *self.fixture.windows_entries[1:]])
        self.invalid_inputs_do_not_invoke_github()

    def test_wrong_windows_platform_rejects_before_github(self):
        manifest = json.loads(self.fixture.windows_entries[0][1])
        manifest["platform"] = "linux-x86_64"
        self.fixture.write_windows([("runtime-manifest.json", json.dumps(manifest).encode()),
                                    *self.fixture.windows_entries[1:]])
        self.invalid_inputs_do_not_invoke_github()

    def test_static_windows_package_rejects_before_github(self):
        manifest = json.loads(self.fixture.windows_entries[0][1])
        manifest["linkage"] = "static"
        self.fixture.write_windows([("runtime-manifest.json", json.dumps(manifest).encode()),
                                    *self.fixture.windows_entries[1:]])
        self.invalid_inputs_do_not_invoke_github()

    def test_duplicate_windows_entry_rejects_before_github(self):
        self.fixture.write_windows([*self.fixture.windows_entries, self.fixture.windows_entries[1]])
        self.invalid_inputs_do_not_invoke_github()

    def test_missing_windows_executable_rejects_before_github(self):
        self.fixture.write_windows([item for item in self.fixture.windows_entries
                                    if item[0] != "mclauncher-qt.exe"])
        self.invalid_inputs_do_not_invoke_github()

    def test_empty_windows_executable_rejects_before_github(self):
        self.fixture.write_windows([(name, b"" if name == "mclauncher-qt.exe" else content)
                                    for name, content in self.fixture.windows_entries])
        self.invalid_inputs_do_not_invoke_github()

    def test_windows_traversal_rejects_before_github(self):
        self.fixture.write_windows([*self.fixture.windows_entries, ("../outside", b"bad")])
        self.invalid_inputs_do_not_invoke_github()

    def test_windows_absolute_path_rejects_before_github(self):
        self.fixture.write_windows([*self.fixture.windows_entries, ("/outside", b"bad")])
        self.invalid_inputs_do_not_invoke_github()

    def test_windows_backslash_path_rejects_before_github(self):
        self.fixture.write_windows([*self.fixture.windows_entries, (r"directory\outside", b"bad")])
        self.invalid_inputs_do_not_invoke_github()

    def test_linux_wrong_root_rejects_before_github(self):
        self.fixture.write_linux([(name.replace(LINUX_ROOT, "unexpected", 1), content)
                                  for name, content in self.fixture.linux_entries])
        self.invalid_inputs_do_not_invoke_github()

    def test_linux_duplicate_entry_rejects_before_github(self):
        self.fixture.write_linux([*self.fixture.linux_entries, self.fixture.linux_entries[0]])
        self.invalid_inputs_do_not_invoke_github()

    def test_linux_traversal_rejects_before_github(self):
        self.fixture.write_linux([*self.fixture.linux_entries, (f"{LINUX_ROOT}/../outside", b"bad")])
        self.invalid_inputs_do_not_invoke_github()

    def test_linux_missing_launcher_rejects_before_github(self):
        self.fixture.write_linux([item for item in self.fixture.linux_entries
                                  if item[0] != f"{LINUX_ROOT}/LittleCraftLauncher"])
        self.invalid_inputs_do_not_invoke_github()

    def test_linux_empty_required_file_rejects_before_github(self):
        self.fixture.write_linux([(name, b"" if name.endswith("/mclauncher-qt") else content)
                                  for name, content in self.fixture.linux_entries])
        self.invalid_inputs_do_not_invoke_github()

    def test_linux_required_symlink_is_not_accepted_as_executable(self):
        self.fixture.write_linux([item for item in self.fixture.linux_entries
                                  if item[0] != f"{LINUX_ROOT}/mclauncher-qt"],
                                 symlinks=[(f"{LINUX_ROOT}/mclauncher-qt", "other-executable")])
        self.invalid_inputs_do_not_invoke_github()

    def test_publish_uploads_only_two_files_then_verifies_draft_before_edit(self):
        hashes = self.fixture_hashes()
        runner = MockGitHub(hashes)
        result = PUBLISH.publish(REPOSITORY, TAG, self.root, TARGET, run=runner)
        self.assertEqual(result, hashes)
        self.assertEqual([command[:3] for command, _ in runner.calls],
                         [["gh", "release", "create"], ["gh", "api", runner.calls[1][0][2]],
                          ["gh", "release", "edit"]])
        create = runner.calls[0][0]
        self.assertEqual(create[4:create.index("--repo")], [str(self.root / WINDOWS), str(self.root / LINUX)])
        self.assertEqual(create[create.index("--target") + 1], TARGET)
        self.assertIn("--draft", create)
        self.assertIn("--prerelease", create)
        self.assertIn("--latest=false", create)
        self.assertIn("--draft=false", runner.edits[0])
        self.assertIn("--prerelease", runner.edits[0])
        self.assertIn("--latest=false", runner.edits[0])
        self.assertTrue(all(options.get("check") is True for _, options in runner.calls))

    def test_publish_finds_unpublished_draft_on_second_inventory_page(self):
        runner = MockGitHub(self.fixture_hashes(), preceding_releases=100)
        PUBLISH.publish(REPOSITORY, TAG, self.root, TARGET, run=runner)
        requests = [command[2] for command, _ in runner.calls if command[:2] == ["gh", "api"]]
        self.assertEqual(requests, [f"repos/{REPOSITORY}/releases?per_page=100&page=1",
                                    f"repos/{REPOSITORY}/releases?per_page=100&page=2"])
        self.assertEqual(len(runner.edits), 1)

    def assert_remote_rejected(self, mutate):
        runner = MockGitHub(self.fixture_hashes(), mutate=mutate)
        with self.assertRaises(ValueError):
            PUBLISH.publish(REPOSITORY, TAG, self.root, TARGET, run=runner)
        self.assertEqual(runner.edits, [])

    def test_remote_checksum_mismatch_prevents_edit(self):
        self.assert_remote_rejected(lambda release: release["assets"][0].update(digest="sha256:" + "c" * 64))

    def test_consistent_but_wrong_remote_bytes_and_notes_prevent_edit(self):
        def mutate(release):
            old_digest = release["assets"][0]["digest"][7:]
            release["assets"][0]["digest"] = "sha256:" + "c" * 64
            release["body"] = release["body"].replace(old_digest, "c" * 64)
        self.assert_remote_rejected(mutate)

    def test_missing_remote_asset_prevents_edit(self):
        self.assert_remote_rejected(lambda release: release["assets"].pop())

    def test_extra_remote_asset_prevents_edit(self):
        self.assert_remote_rejected(lambda release: release["assets"].append({
            "name": "SHA256SUMS.txt", "state": "uploaded", "size": 1,
        }))

    def test_remote_upload_still_pending_prevents_edit(self):
        self.assert_remote_rejected(lambda release: release["assets"][0].update(state="new"))

    def test_remote_release_not_draft_prevents_edit(self):
        self.assert_remote_rejected(lambda release: release.update(draft=False))

    def test_remote_release_not_prerelease_prevents_edit(self):
        self.assert_remote_rejected(lambda release: release.update(prerelease=False))

    def test_missing_runtime_checksum_in_remote_notes_prevents_edit(self):
        self.assert_remote_rejected(lambda release: release.update(body="missing validation metadata"))

    def test_inventory_without_matching_draft_prevents_edit(self):
        self.assert_remote_rejected(lambda release: release.update(tag_name="v9.0.0-pre.1"))

    def test_duplicate_matching_drafts_prevent_edit(self):
        runner = MockGitHub(self.fixture_hashes())
        def run(command, **options):
            result = runner(command, **options)
            if command[:2] == ["gh", "api"]:
                release = json.loads(result.stdout)[0]
                result.stdout = json.dumps([release, copy.deepcopy(release)])
            return result
        with self.assertRaises(ValueError):
            PUBLISH.publish(REPOSITORY, TAG, self.root, TARGET, run=run)
        self.assertEqual(runner.edits, [])


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        self.hashes = {WINDOWS: "a" * 64, LINUX: "b" * 64}

    def test_hashes_and_official_qt_directions_are_folded(self):
        body = SUPPORT.release_notes(REPOSITORY, TAG, self.hashes)
        primary = body.split("<details>", 1)[0]
        self.assertNotIn(self.hashes[WINDOWS], primary)
        self.assertNotIn(SUPPORT.QT_SOURCE_BASE_URL, primary)
        hashes = body.split("<summary>SHA-256 校验</summary>", 1)[1].split("</details>", 1)[0]
        sources = body.split("<summary>第三方依赖与许可</summary>", 1)[1].split("</details>", 1)[0]
        for name, digest in self.hashes.items():
            self.assertIn(f"| {name} | `{digest}` |", hashes)
        for name, (_, digest) in SUPPORT.QT_SOURCE_ARCHIVES.items():
            self.assertIn(f"({SUPPORT.QT_SOURCE_BASE_URL}{name})", sources)
            self.assertIn(f"`{digest}`", sources)

    def test_actual_change_intro_is_used_without_inventing_ui_upgrades(self):
        changes = "仅整理下载附件，应用二进制保持原样。"
        body = SUPPORT.release_notes(REPOSITORY, TAG, self.hashes, changes)
        self.assertTrue(body.startswith(changes + "\n"))
        self.assertEqual(body.count(changes), 1)
        self.assertNotIn("#27", body)
        self.assertNotIn("UI 升级", body)
        self.assertNotIn("动画升级", body)
        self.assertIn("Linux 暂不支持游戏启动", body)
        self.assertIn("Microsoft 登录暂不可用", body)


if __name__ == "__main__":
    unittest.main()
