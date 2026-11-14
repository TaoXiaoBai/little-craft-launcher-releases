#!/usr/bin/env python3
"""Verify tiny real archives and mocked publication without network access."""

import copy
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import struct
import sys
import tarfile
import tempfile
import time
import textwrap
import unittest
from unittest import mock
import warnings
import zipfile


SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))
import release_support as SUPPORT
import appimage_format as APPIMAGE
import test_windows_bundle_format as WINDOWS_FIXTURE

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
    def __init__(self, hashes, mutate=None, preceding_releases=0, tag=TAG):
        self.hashes = hashes
        self.mutate = mutate
        self.preceding_releases = preceding_releases
        self.calls = []
        self.release = None
        self.notes = None
        self.tag = tag

    def __call__(self, command, **options):
        self.calls.append((list(command), dict(options)))
        if command[:3] == ["gh", "release", "create"]:
            self.notes = Path(command[command.index("--notes-file") + 1]).read_text(encoding="utf-8")
            self.release = {
                "id": 31, "tag_name": self.tag, "draft": True, "prerelease": True, "body": self.notes,
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
        self.patch = mock.patch.object(PUBLISH, "dependency_archives", return_value=self.fixture.qt_pins)
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

    def test_pre4_notes_remain_byte_for_byte_frozen(self):
        fixtures = (
            (None, "635973fa6f26fa156c0d2d364b3ee3a0e3a307653af1219c5db420de6f93facb"),
            ("仅整理下载附件，应用二进制保持原样。", "4b76660d1907be4cca6b3372ba3b49342be3a66ba86668b1b7b7090fa2da813f"),
        )
        for changes, expected in fixtures:
            with self.subTest(changes=changes):
                body = SUPPORT.release_notes(REPOSITORY, TAG, self.hashes, changes)
                self.assertEqual(hashlib.sha256(body.encode("utf-8")).hexdigest(), expected)

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

    def test_new_appimage_has_five_sources_while_pre4_remains_frozen(self):
        tag = "v1.0.0-pre.5"
        windows, linux = SUPPORT.runtime_names(tag)
        self.assertTrue(linux.endswith(".AppImage"))
        self.assertTrue(LINUX.endswith(".tar.gz"))
        self.assertEqual(len(SUPPORT.QT_SOURCE_ARCHIVES), 4)
        self.assertEqual(SUPPORT.dependency_archives(TAG), SUPPORT.QT_SOURCE_ARCHIVES)
        self.assertEqual(len(SUPPORT.dependency_archives(tag)), 5)
        body = SUPPORT.release_notes(REPOSITORY, tag, {windows: "a" * 64, linux: "b" * 64})
        for name, (_, digest) in SUPPORT.dependency_archives(tag).items():
            self.assertIn(f"({SUPPORT.QT_SOURCE_BASE_URL}{name})", body)
            self.assertIn(f"`{digest}`", body)
        self.assertIn("X11 / Wayland", body)
        self.assertNotIn("qtwayland-everywhere", SUPPORT.release_notes(REPOSITORY, TAG, self.hashes))

    def test_pre5_describes_linux_capabilities_and_keyring_requirements(self):
        tag = "v1.0.0-pre.5"
        windows, linux = SUPPORT.runtime_names(tag)
        hashes = {windows: "a" * 64, linux: "b" * 64}
        body = SUPPORT.release_notes(REPOSITORY, tag, hashes)
        primary = body.split("<details>", 1)[0]
        self.assertIn("Linux x86_64 游戏启动", primary)
        self.assertIn("Java 8/17/21/25", primary)
        self.assertIn("Secret Service", primary)
        self.assertIn("不回退明文", primary)
        self.assertIn("无法解锁、取消或超时", primary)
        self.assertIn("Microsoft 登录仍等待项目客户端配置及相关服务准入", primary)
        self.assertNotIn("Linux 暂不支持游戏启动", body)
        self.assertNotIn("Linux 实验预览", body)
        self.assertNotIn(hashes[windows], primary)
        self.assertNotIn(SUPPORT.QT_SOURCE_BASE_URL, primary)
        self.assertEqual(len(SUPPORT.runtime_names(tag)), 2)

    def test_published_configured_releases_keep_their_microsoft_wording(self):
        for tag in ("v1.0.0-pre.9", "v1.0.0-pre.10", "v1.0.0-pre.13"):
            with self.subTest(tag=tag):
                names = SUPPORT.runtime_names(tag)
                hashes = dict(zip(names, ("a" * 64, "b" * 64)))
                body = SUPPORT.release_notes(REPOSITORY, tag, hashes)
                self.assertIn("Microsoft 登录已内置客户端配置", body)
                self.assertIn("拥有 Minecraft Java 版的账号测试", body)
                self.assertIn("Mojang 服务准入及实际登录结果仍待确认", body)
                self.assertNotIn("仍等待项目客户端配置", body)
                self.assertEqual(len(names), 2)
                for name, checksum in hashes.items():
                    self.assertIn(f"| {name} | `{checksum}` |", body)
                for name in SUPPORT.dependency_archives(tag):
                    self.assertIn(f"({SUPPORT.QT_SOURCE_BASE_URL}{name})", body)

    def test_compact_notes_preserve_publication_and_cleanup_evidence(self):
        changes = "- 显示所选角色的皮肤头像。\n- 改进头像缓存与切换。"
        for tag in ("v1.0.0-pre.14", "v1.0.0-pre.100", "v1.0.0-rc.1", "v1.0.1-pre.1", "v2.0.0-alpha.1"):
            with self.subTest(tag=tag):
                names = SUPPORT.runtime_names(tag)
                hashes = dict(zip(names, ("a" * 64, "b" * 64)))
                body = SUPPORT.release_notes(REPOSITORY, tag, hashes, changes)
                primary, details = body.split("<details>", 1)
                self.assertTrue(primary.startswith(changes + "\n\n"))
                self.assertEqual(body.count("<details>"), 1)
                self.assertEqual(body.count("</details>"), 1)
                self.assertNotIn("## 使用说明", primary)
                self.assertNotIn("SmartScreen", primary)
                self.assertNotIn("Mojang 服务准入及实际登录结果仍待确认", body)
                self.assertIn("SmartScreen", details)
                self.assertIn("Secret Service", details)
                self.assertIn("--extract-runtime <新目录>", details)
                self.assertIn("SUPPORTING_MATERIALS.zip", details)
                for name, digest in hashes.items():
                    self.assertIn(f"/releases/download/{tag}/{name}", primary)
                    self.assertNotIn(digest, primary)
                assets = [{"name": name, "size": 100, "state": "uploaded", "digest": "sha256:" + digest}
                          for name, digest in hashes.items()]
                SUPPORT.verify_published_assets(tag, assets, body)
                with self.assertRaises(ValueError):
                    SUPPORT.verify_published_assets(tag, assets, body.replace("`" + hashes[names[0]] + "`", ""))
                qt_name = next(iter(SUPPORT.dependency_archives(tag)))
                with self.assertRaises(ValueError):
                    SUPPORT.verify_published_assets(tag, assets, body.replace(SUPPORT.QT_SOURCE_BASE_URL + qt_name, ""))

    def test_pre7_and_pre8_keep_their_historical_microsoft_wording(self):
        for tag in ("v1.0.0-pre.7", "v1.0.0-pre.8"):
            names = SUPPORT.runtime_names(tag)
            body = SUPPORT.release_notes(REPOSITORY, tag, dict(zip(names, ("a" * 64, "b" * 64))))
            self.assertIn("Microsoft 登录仍等待项目客户端配置及相关服务准入", body)
            self.assertNotIn("Microsoft 登录已内置客户端配置", body)

    def test_pre9_release_file_is_rendered_once_with_product_changes(self):
        tag = "v1.0.0-pre.9"
        changes = (SCRIPT_ROOT.parent / "release-notes" / (tag + ".md")).read_text(encoding="utf-8").strip()
        names = SUPPORT.runtime_names(tag)
        body = SUPPORT.release_notes(REPOSITORY, tag, dict(zip(names, ("a" * 64, "b" * 64))), changes)
        self.assertTrue(body.startswith(changes + "\n"))
        self.assertEqual(body.count(changes), 1)
        self.assertIn("代表位置估算", body)
        self.assertIn("跨午夜", body)
        self.assertIn("减少动态效果", body)
        self.assertNotIn("runner", changes)
        self.assertNotIn("CI", changes)


def elf_header(appimage=False):
    identity = bytearray(b"\x7fELF\x02\x01\x01" + bytes(9))
    if appimage:
        identity[8:11] = b"AI\x02"
    return struct.pack("<16sHHIQQQIHHHHHH", bytes(identity), 3, 62, 1,
                       0, 64, 0, 0, 64, 56, 1, 0, 0, 0)


def appimage_bytes(offset=256, section_end=None):
    end = section_end or 256
    data = bytearray(max(end, offset) + 96)
    data[:64] = elf_header(appimage=True)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, 0, 0, 256, 256, 4096)
    if section_end:
        struct.pack_into("<Q", data, 40, 120)
        struct.pack_into("<HH", data, 58, 64, 2)
        struct.pack_into("<IIQQQQIIQQ", data, 184, 0, 1, 0, 0, 256, section_end - 256, 0, 0, 1, 0)
    data[offset:offset + 4] = b"hsqs"
    struct.pack_into("<I", data, offset + 4, 20)
    struct.pack_into("<I", data, offset + 12, 131072)
    struct.pack_into("<HH", data, offset + 28, 4, 0)
    struct.pack_into("<Q", data, offset + 40, 96)
    return bytes(data)


class AppImageFixture:
    def __init__(self, path, tag="v1.0.0-pre.5"):
        self.path, self.tag = path, tag
        self.path.write_bytes(appimage_bytes())
        self.manifest = {"schema": 2, "version": tag, "platform": "linux-x86_64", "format": "appimage",
                         "qt": "6.8.3", "linkage": "dynamic", "displayBackends": ["xcb", "wayland"],
                         "application": "usr/bin/mclauncher-qt"}
        self.contents = {"AppRun": b"#!/bin/sh\nexec ./usr/bin/mclauncher-qt\n",
                         "SUPPORTING_MATERIALS.zip": tiny_zip([("NOTICE.txt", b"legal material")]),
                         "usr/bin/mclauncher-qt": elf_header()}
        for name in ("libqxcb.so", "libqwayland-generic.so", "libqwayland-egl.so"):
            self.contents["usr/plugins/platforms/" + name] = elf_header()
        self.extra_entries = []
        self.linked = set()
        self.commands = []

    def reader(self, command, maximum, deadline):
        self.commands.append(command)
        if "-lln" in command:
            entries = [*self.contents, "runtime-manifest.json", *self.extra_entries]
            rows = []
            for name in entries:
                mode = "lrwxrwxrwx" if name in self.linked else "-rwxr-xr-x"
                target = " -> outside" if name in self.linked else ""
                rows.append(f"{mode} 0/0 100 2026-01-01 00:00 squashfs-root/{name}{target}")
            return "\n".join(rows).encode()
        if "-cat" not in command:
            raise AssertionError("An uploaded runtime must never be executed")
        name = command[-1]
        return json.dumps(self.manifest).encode() if name == "runtime-manifest.json" else self.contents[name]

    def patches(self, case):
        for patch in (mock.patch.object(APPIMAGE.shutil, "which", return_value="/fixture/unsquashfs"),
                      mock.patch.object(APPIMAGE, "_bounded_run", side_effect=self.reader)):
            patch.start()
            case.addCleanup(patch.stop)


class AppImageInspectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="static appimage fixture-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "launcher.AppImage"
        self.fixture = AppImageFixture(self.path)
        self.fixture.patches(self)

    def inspect(self):
        return APPIMAGE.inspect_appimage(self.path, self.fixture.tag)

    def test_static_inspection_uses_only_system_unsquashfs(self):
        self.assertEqual(self.inspect(), self.fixture.manifest)
        self.assertTrue(self.fixture.commands)
        self.assertTrue(all(command[0] == "/fixture/unsquashfs" for command in self.fixture.commands))
        self.assertTrue(all(command[command.index("-offset") + 1] == "256"
                            for command in self.fixture.commands))

    def test_local_candidate_tag_can_be_inspected_but_not_published(self):
        tag = "v1.0.0-pre.4-appimage-test"
        self.fixture.tag = tag
        self.fixture.manifest["version"] = tag
        self.assertEqual(self.inspect()["version"], tag)
        runner = mock.Mock()
        with self.assertRaises(ValueError):
            PUBLISH.publish(REPOSITORY, tag, self.path.parent, TARGET, run=runner)
        runner.assert_not_called()

    def test_unsafe_or_unbounded_local_tags_reject_before_decompression(self):
        for tag in ("../outside", "tag with spaces", "tag\n", "a" * 102, "", None):
            with self.subTest(tag=tag):
                with self.assertRaises(ValueError):
                    APPIMAGE.inspect_appimage(self.path, tag)
        self.assertEqual(self.fixture.commands, [])

    def test_section_file_end_and_page_aligned_offsets_are_supported(self):
        for offset, section_end in ((300, 300), (4096, None)):
            with self.subTest(offset=offset):
                self.path.write_bytes(appimage_bytes(offset, section_end))
                self.assertEqual(APPIMAGE._filesystem_offset(self.path), offset)

    def test_unrelated_magic_is_not_searched(self):
        self.path.write_bytes(appimage_bytes(8192))
        with self.assertRaises(ValueError):
            self.inspect()
        self.assertEqual(self.fixture.commands, [])

    def test_bad_marker_architecture_and_elf_bounds_reject_before_decompression(self):
        original = appimage_bytes()
        for offset, payload in ((8, b"BAD"), (18, b"\xb7\x00"), (64 + 32, struct.pack("<Q", 2**32))):
            with self.subTest(offset=offset):
                data = bytearray(original)
                data[offset:offset + len(payload)] = payload
                self.path.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.inspect()
        self.assertEqual(self.fixture.commands, [])

    def test_superblock_cannot_extend_outside_image(self):
        data = bytearray(appimage_bytes())
        struct.pack_into("<Q", data, 256 + 40, 100000)
        self.path.write_bytes(data)
        with self.assertRaises(ValueError):
            self.inspect()

    def test_wrong_runtime_identity_or_missing_backend_rejects(self):
        original = dict(self.fixture.manifest)
        for key, value in (("schema", 1), ("version", TAG), ("platform", "windows-x64"),
                           ("format", "tar"), ("qt", "6.8.2"), ("linkage", "static"),
                           ("displayBackends", ["xcb"]), ("application", "../outside")):
            with self.subTest(key=key):
                self.fixture.manifest = {**original, key: value}
                with self.assertRaises(ValueError):
                    self.inspect()

    def test_missing_wayland_plugin_or_linked_application_rejects(self):
        del self.fixture.contents["usr/plugins/platforms/libqwayland-egl.so"]
        with self.assertRaises(ValueError):
            self.inspect()
        self.fixture.contents["usr/plugins/platforms/libqwayland-egl.so"] = elf_header()
        self.fixture.linked.add("usr/bin/mclauncher-qt")
        with self.assertRaises(ValueError):
            self.inspect()

    def test_obvious_private_source_build_and_credentials_reject(self):
        for name in ("source/qt-native/src/main.cpp", "ApplicationSnapshot/main.cpp",
                     "usr/bin/main.cpp", "usr/secret.key"):
            with self.subTest(name=name):
                self.fixture.extra_entries = [name]
                with self.assertRaises(ValueError):
                    self.inspect()


class AppImagePublishTests(unittest.TestCase):
    release_tag = "v1.0.0-pre.5"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="appimage publisher fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        legacy = ArchiveFixture(self.root)
        self.tag = self.release_tag
        self.windows, self.linux = SUPPORT.runtime_names(self.tag)
        manifest = json.loads(legacy.windows_entries[0][1])
        manifest["version"] = self.tag
        (self.root / self.windows).write_bytes(tiny_zip([
            ("runtime-manifest.json", json.dumps(manifest).encode()), *legacy.windows_entries[1:]]))
        (self.root / WINDOWS).unlink()
        (self.root / LINUX).unlink()
        self.image = AppImageFixture(self.root / self.linux, self.tag)
        self.image.patches(self)
        name = next(iter(SUPPORT.WAYLAND_SOURCE_ARCHIVE))
        payload = tiny_zip([("LICENSE.txt", b"synthetic Wayland upstream archive")])
        (self.root / name).write_bytes(payload)
        self.pins = {**legacy.qt_pins, name: (len(payload), hashlib.sha256(payload).hexdigest())}
        patch = mock.patch.object(PUBLISH, "dependency_archives", return_value=self.pins)
        patch.start()
        self.addCleanup(patch.stop)

    def test_new_runtime_and_five_sources_validate_and_publish_only_two_assets(self):
        hashes = PUBLISH.validate_inputs(self.root, self.tag)
        self.assertEqual(set(hashes), {self.windows, self.linux})
        runner = MockGitHub(hashes, tag=self.tag)
        PUBLISH.publish(REPOSITORY, self.tag, self.root, TARGET, run=runner)
        create = runner.calls[0][0]
        self.assertEqual(set(create[4:6]), {str(self.root / self.windows), str(self.root / self.linux)})
        self.assertFalse(any(name in create for name in self.pins))
        self.assertEqual(len(runner.edits), 1)

    def test_missing_wayland_source_and_extra_private_snapshot_never_reach_github(self):
        source = self.root / next(iter(SUPPORT.WAYLAND_SOURCE_ARCHIVE))
        payload = source.read_bytes()
        for mutation in ("missing-wayland", "private-snapshot"):
            with self.subTest(mutation=mutation):
                source.write_bytes(payload)
                extra = self.root / "private-source.zip"
                if mutation == "missing-wayland":
                    source.unlink()
                else:
                    extra.write_bytes(b"private")
                runner = mock.Mock()
                with self.assertRaises(ValueError):
                    PUBLISH.publish(REPOSITORY, self.tag, self.root, TARGET, run=runner)
                runner.assert_not_called()

    def test_bad_appimage_metadata_never_reaches_github(self):
        self.image.manifest["linkage"] = "static"
        runner = mock.Mock()
        with self.assertRaises(ValueError):
            PUBLISH.publish(REPOSITORY, self.tag, self.root, TARGET, run=runner)
        runner.assert_not_called()


class LastLegacyWindowsPublishTests(AppImagePublishTests):
    """The previously published pre.9 still takes its frozen deployment path."""
    release_tag = "v1.0.0-pre.9"


class OneFileWindowsPublishTests(AppImagePublishTests):
    release_tag = "v1.0.0-pre.10"

    def setUp(self):
        super().setUp()
        self.payload = WINDOWS_FIXTURE.build_runtime_payload(self.tag)
        self.bundle = WINDOWS_FIXTURE.build_bundle(self.payload, self.tag)
        self.write_windows([(WINDOWS_FIXTURE.ENTRYPOINT, self.bundle)])

    def write_windows(self, entries):
        (self.root / self.windows).write_bytes(WINDOWS_FIXTURE.zip_bytes(entries))

    def assert_rejected_before_github(self):
        runner = mock.Mock()
        with self.assertRaises(ValueError):
            PUBLISH.publish(REPOSITORY, self.tag, self.root, TARGET, run=runner)
        runner.assert_not_called()

    def test_new_preview_cannot_silently_fall_back_to_legacy_windows_zip(self):
        self.write_windows([
            ("runtime-manifest.json", json.dumps({
                "version": self.tag, "platform": "windows-x64", "linkage": "dynamic"}).encode()),
            ("mclauncher-qt.exe", WINDOWS_FIXTURE.build_pe()),
            ("SUPPORTING_MATERIALS.zip", tiny_zip([("NOTICE.txt", b"legal")])),
        ])
        self.assert_rejected_before_github()

    def test_single_inner_executable_without_embedded_runtime_is_rejected(self):
        self.write_windows([(WINDOWS_FIXTURE.ENTRYPOINT, WINDOWS_FIXTURE.build_pe())])
        self.assert_rejected_before_github()

    def test_top_level_sidecars_directories_and_links_are_rejected(self):
        link = zipfile.ZipInfo("linked.exe")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        for extra in (("Qt6Core.dll", b"unexpected sidecar"), ("runtime/", b""),
                      ("private-source.zip", b"unpublished"), (link, b"mclauncher-qt.exe")):
            with self.subTest(name=str(extra[0])):
                self.write_windows([(WINDOWS_FIXTURE.ENTRYPOINT, self.bundle), extra])
                self.assert_rejected_before_github()

    def test_non_system_stub_imports_and_wrong_release_identity_are_rejected(self):
        for bundle in (WINDOWS_FIXTURE.build_bundle(self.payload, self.tag, imports=("Qt6Core.dll",)),
                       WINDOWS_FIXTURE.build_bundle(tag="v1.0.0-pre.11")):
            with self.subTest(digest=hashlib.sha256(bundle).hexdigest()):
                self.write_windows([(WINDOWS_FIXTURE.ENTRYPOINT, bundle)])
                self.assert_rejected_before_github()

    def test_payload_inventory_manifest_checksums_and_legal_material_are_verified(self):
        cases = [
            WINDOWS_FIXTURE.build_runtime_payload(self.tag, manifest_overrides={"linkage": "static"}),
            WINDOWS_FIXTURE.build_runtime_payload(self.tag, sums_override=b"0" * 64 + b"  nonexistent.dll\n"),
            WINDOWS_FIXTURE.build_runtime_payload(self.tag, overrides={"SUPPORTING_MATERIALS.zip": b"not a zip"}),
            WINDOWS_FIXTURE.build_runtime_payload(self.tag, overrides={"platforms/qwindows.dll": None}),
        ]
        for payload in cases:
            with self.subTest(digest=hashlib.sha256(payload).hexdigest()):
                # The independent encoder supplies valid PE and outer hashes;
                # publication must still inspect the actual inner inventory.
                self.write_windows([(WINDOWS_FIXTURE.ENTRYPOINT,
                                     WINDOWS_FIXTURE.build_bundle(payload, self.tag))])
                self.assert_rejected_before_github()

    def test_replaced_payload_is_rejected_even_with_valid_inner_metadata(self):
        index = WINDOWS_FIXTURE.reference_index(self.payload, self.tag)
        changed = WINDOWS_FIXTURE.build_runtime_payload(self.tag, overrides={"Qt6Core.dll": b"changed valid runtime"})
        self.write_windows([(WINDOWS_FIXTURE.ENTRYPOINT,
                             WINDOWS_FIXTURE.build_bundle(changed, self.tag, index=index))])
        self.assert_rejected_before_github()

    def test_onefile_inspection_never_executes_the_uploaded_windows_program(self):
        with mock.patch.object(PUBLISH.subprocess, "run", side_effect=AssertionError("Do not execute a runtime")):
            hashes = PUBLISH.validate_inputs(self.root, self.tag)
        self.assertEqual(set(hashes), {self.windows, self.linux})

    def test_uploaded_onefile_digest_must_match_locally_validated_bytes(self):
        hashes = PUBLISH.validate_inputs(self.root, self.tag)
        runner = MockGitHub(hashes, tag=self.tag,
                            mutate=lambda release: release["assets"][0].update(digest="sha256:" + "0" * 64))
        with self.assertRaises(ValueError):
            PUBLISH.publish(REPOSITORY, self.tag, self.root, TARGET, run=runner)
        self.assertEqual(runner.edits, [])


class BoundedReaderTests(unittest.TestCase):
    def test_known_reader_output_is_returned(self):
        result = APPIMAGE._bounded_run([sys.executable, "-c", "print('bounded')"], 64,
                                       time.monotonic() + 5)
        self.assertEqual(result, b"bounded\n")

    def test_excess_stdout_and_stderr_are_stopped(self):
        for stream, maximum in (("stdout", 16), ("stderr", 1024)):
            with self.subTest(stream=stream):
                command = [sys.executable, "-c", f"import sys; sys.{stream}.write('x' * 100000)"]
                with self.assertRaisesRegex(ValueError, "exceeds its bound"):
                    APPIMAGE._bounded_run(command, maximum, time.monotonic() + 5)

    def test_reader_deadline_and_failure_are_not_accepted(self):
        with self.assertRaisesRegex(ValueError, "timed out"):
            APPIMAGE._bounded_run([sys.executable, "-c", "import time; time.sleep(2)"], 64,
                                   time.monotonic() + 0.05)
        with self.assertRaisesRegex(ValueError, "Could not read"):
            APPIMAGE._bounded_run([sys.executable, "-c", "raise SystemExit(1)"], 64,
                                   time.monotonic() + 5)


class VerifiedWorkflowTests(unittest.TestCase):
    def setUp(self):
        workflow = SCRIPT_ROOT.parent / "workflows/publish-verified-prerelease.yml"
        self.text = workflow.read_text(encoding="utf-8")
        self.program = textwrap.dedent(self.text.split("python3 <<'PY'\n", 1)[1].split("          PY\n", 1)[0])

    def verify(self, tag, wayland_count=0, linux_success=True, *, missing_receipt=None,
               receipt_attempt=1, current_attempt=1):
        def api(command, **options):
            self.assertEqual(command[:2], ["gh", "api"])
            path = command[2]
            if "/actions/workflows/" in path:
                result = {"path": ".github/workflows/prerelease.yml"}
            else:
                run_id = path.split("/actions/runs/", 1)[1].split("/", 1)[0].split("?", 1)[0]
                if path.endswith("/" + run_id):
                    result = {"repository": {"full_name": REPOSITORY},
                              "head_repository": {"full_name": REPOSITORY},
                              "event": "workflow_dispatch", "head_branch": "main", "workflow_id": 51,
                              "run_attempt": current_attempt,
                              "display_title": f"Prerelease {tag} (both)"}
                elif "/jobs?" in path:
                    platform = "windows-x64" if run_id == "1" else "linux-x86_64"
                    result = {"jobs": [{"name": "Build " + platform, "status": "completed",
                                        "conclusion": "success" if run_id == "1" or linux_success else "failure"}]}
                elif "/artifacts?" in path:
                    names = (["release-windows-x64", "release-qt-dependency-sources"] if run_id == "1" else
                             ["release-linux-x86_64"] + ["release-qt-wayland-source"] * wayland_count)
                    platform = "windows-x64" if run_id == "1" else "linux-x86_64"
                    if platform != missing_receipt:
                        names.append(f"provenance-{platform}-attempt-{receipt_attempt}")
                    result = {"artifacts": [{"name": name, "expired": False} for name in names]}
                else:
                    raise AssertionError("Unexpected provenance API request")
            return json.dumps(result)

        environment = {"GITHUB_REPOSITORY": REPOSITORY, "VERSION_TAG": tag, "DEFAULT_BRANCH": "main",
                       "WINDOWS_RUN_ID": "1", "LINUX_RUN_ID": "2"}
        with mock.patch.dict(PUBLISH.os.environ, environment), mock.patch.object(subprocess, "check_output", side_effect=api):
            with contextlib.redirect_stdout(io.StringIO()):
                exec(compile(self.program, "verified-workflow-fixture", "exec"), {})

    def test_pre4_accepts_its_historical_runtime_and_four_source_artifact(self):
        self.verify(TAG)

    def test_new_tag_requires_exactly_one_wayland_source_from_linux_run(self):
        self.verify("v1.0.0-pre.5", wayland_count=1)
        for count in (0, 2):
            with self.subTest(count=count):
                with self.assertRaisesRegex(RuntimeError, "absent, expired or ambiguous"):
                    self.verify("v1.0.0-pre.5", wayland_count=count)

    def test_present_sources_cannot_override_a_failed_linux_build(self):
        with self.assertRaisesRegex(RuntimeError, "did not succeed"):
            self.verify("v1.0.0-pre.5", wayland_count=1, linux_success=False)

    def test_both_runtime_packages_and_source_archives_cannot_replace_a_missing_receipt(self):
        for platform in ("windows-x64", "linux-x86_64"):
            with self.subTest(platform=platform):
                with self.assertRaisesRegex(RuntimeError, "absent, expired or ambiguous"):
                    self.verify("v1.0.0-pre.5", wayland_count=1, missing_receipt=platform)

    def test_a_previous_attempt_receipt_does_not_authorize_the_current_attempt(self):
        with self.assertRaisesRegex(RuntimeError, "absent, expired or ambiguous"):
            self.verify("v1.0.0-pre.5", wayland_count=1, current_attempt=2, receipt_attempt=1)
        self.verify("v1.0.0-pre.5", wayland_count=1, current_attempt=2, receipt_attempt=2)

    def test_a_valid_current_attempt_is_required_before_receipt_lookup(self):
        for value in (None, True, 0, -1, "1"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "current build attempt"):
                    self.verify("v1.0.0-pre.5", wayland_count=1, current_attempt=value)

    @unittest.skipIf(sys.platform == "win32", "The publisher is an Ubuntu Bash step")
    def test_verify_only_checks_provenance_and_packages_without_publishing_or_cleanup(self):
        step = self.text.split("      - name: Verify packages and publish two downloads\n", 1)[1]
        step = step.split("\n      - name:", 1)[0]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands_file = root / "commands.jsonl"
            executable = root / "python3"
            executable.write_text(
                "#!" + sys.executable + "\n"
                "import json, os, sys\n"
                "with open(os.environ['COMMANDS_FILE'], 'a') as output:\n"
                "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n", encoding="utf-8")
            executable.chmod(0o755)
            for verify_only in (True, False):
                with self.subTest(verify_only=verify_only):
                    commands_file.write_text("", encoding="utf-8")
                    environment = dict(PUBLISH.os.environ, COMMANDS_FILE=str(commands_file),
                        PATH=str(root) + PUBLISH.os.pathsep + PUBLISH.os.environ.get("PATH", ""),
                        GITHUB_REPOSITORY=REPOSITORY, VERSION_TAG="v1.0.0-pre.13",
                        GITHUB_SHA=TARGET, WINDOWS_RUN_ID="1", LINUX_RUN_ID="2",
                        VERIFY_ONLY="true" if verify_only else "false")
                    subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                                   env=environment, check=True, capture_output=True, text=True)
                    commands = [json.loads(line) for line in commands_file.read_text().splitlines()]
                    self.assertEqual(len(commands), 2)
                    self.assertTrue(commands[0][0].endswith("/release_provenance.py"))
                    self.assertEqual(commands[0][1], "verify-runs")
                    self.assertEqual("--verify-only" in commands[0], verify_only)
                    self.assertIn("--windows-run-id", commands[0])
                    self.assertIn("--linux-run-id", commands[0])
                    self.assertTrue(commands[1][0].endswith("/publish-prerelease.py"))
                    self.assertEqual("--verify-only" in commands[1], verify_only)
        cleanup = self.text.split("      - name: Remove temporary packages after verifying the published prerelease\n", 1)[1]
        condition = next(line for line in cleanup.splitlines() if line.lstrip().startswith("if:"))
        self.assertIn("success()", condition)
        self.assertIn("!inputs.verify_only", condition)

    def test_dispatch_only_download_and_static_reader_conditions(self):
        trigger = self.text.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertTrue(trigger.startswith("  workflow_dispatch:\n"))
        self.assertEqual([line for line in trigger.splitlines() if line.startswith("  ") and not line.startswith("    ")],
                         ["  workflow_dispatch:"])
        download = self.text.split("      - name: Download matching Qt Wayland dependency source\n", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("if: ${{ inputs.tag != 'v1.0.0-pre.4' }}", download)
        self.assertIn("name: release-qt-wayland-source", download)
        self.assertIn("run-id: ${{ inputs.linux_run_id }}", download)
        reader = self.text.split("      - name: Prepare static AppImage inspection\n", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("if: ${{ inputs.tag != 'v1.0.0-pre.4' }}", reader)
        self.assertIn("command -v unsquashfs", reader)
        self.assertIn("squashfs-tools", reader)


if __name__ == "__main__":
    unittest.main()
