#!/usr/bin/env python3
"""Local pin/cache and source-extraction fixtures; no downloads, tools or GUI."""

import hashlib
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import warnings
import zipfile

SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))
import linux_toolchain as TOOLCHAIN


def digest(data):
    return hashlib.sha256(data).hexdigest()


def put(path, data=b"fixture", executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o755 if executable else 0o644)
    return path


class Response(io.BytesIO):
    def __init__(self, data, fail_after_first_read=False):
        super().__init__(data)
        self.read_sizes = []
        self.fail_after_first_read = fail_after_first_read

    def read(self, size=-1):
        if self.fail_after_first_read and self.read_sizes:
            raise OSError("Synthetic interrupted response")
        self.read_sizes.append(size)
        return super().read(size)


class LinuxToolchainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.network = mock.patch.object(
            TOOLCHAIN.urllib.request, "urlopen",
            side_effect=AssertionError("Unexpected network request in a local fixture"))
        self.urlopen = self.network.start()
        self.addCleanup(self.network.stop)

    def tool_cache(self):
        directory = self.root / "tools"
        payloads = {name: ("SYNTHETIC PUBLIC TOOL: " + name).encode()
                    for name in TOOLCHAIN.TOOL_ASSETS}
        pins = {name: ("https://example.invalid/" + name, len(data), digest(data))
                for name, data in payloads.items()}
        for name, data in payloads.items():
            put(directory / name, data, executable=True)
        return directory, pins, payloads

    @staticmethod
    def source_base(source):
        return source / "qt-native/.tools/Qt/6.8.3/Src"

    def archive_fixture(self, source, module="qtbase", extra=(), version="6.8.3",
                        include_config=True):
        name = module + "-everywhere-src-6.8.3.zip"
        prefix = module + "-everywhere-src-6.8.3/"
        data = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # Deliberate duplicate ZIP fixture.
            with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_STORED) as archive:
                if include_config:
                    archive.writestr(prefix + ".cmake.conf",
                                     f'set(QT_REPO_MODULE_VERSION "{version}")\n')
                archive.writestr(prefix + "LICENSES/LGPL-3.0-only.txt", b"Synthetic Qt license")
                archive.writestr(prefix + "src/public_source.cpp", b"Synthetic public Qt source")
                for member, contents, mode in extra:
                    info = zipfile.ZipInfo(member)
                    info.create_system = 3
                    info.external_attr = mode << 16
                    archive.writestr(info, contents)
        contents = data.getvalue()
        path = put(source / "qt-native/.tools/downloads" / name, contents)
        return name, (len(contents), digest(contents)), path

    def assert_no_source_output(self, source, module="qtbase"):
        base = self.source_base(source)
        self.assertFalse((base / module).exists())
        if base.exists():
            self.assertEqual(list(base.glob(".qt-source-*")), [])

    def test_public_pin_metadata_covers_four_tools_and_five_matching_qt_modules(self):
        self.assertEqual(set(TOOLCHAIN.TOOL_ASSETS), {
            "linuxdeploy-x86_64.AppImage", "linuxdeploy-plugin-qt-x86_64.AppImage",
            "linuxdeploy-plugin-appimage-x86_64.AppImage", "runtime-x86_64"})
        for name, (url, size, checksum) in TOOLCHAIN.TOOL_ASSETS.items():
            with self.subTest(tool=name):
                self.assertTrue(url.startswith("https://github.com/"))
                self.assertTrue(url.endswith("/" + name))
                self.assertGreater(size, 0)
                self.assertRegex(checksum, r"^[0-9a-f]{64}$")
        self.assertEqual(set(TOOLCHAIN.SOURCE_PINS), {
            module + "-everywhere-src-6.8.3.zip"
            for module in ("qtbase", "qtdeclarative", "qtsvg", "qtimageformats", "qtwayland")})
        for size, checksum in TOOLCHAIN.SOURCE_PINS.values():
            self.assertGreater(size, 0)
            self.assertRegex(checksum, r"^[0-9a-f]{64}$")

    def test_verified_tool_cache_accepts_only_matching_executable_bytes(self):
        cache, pins, payloads = self.tool_cache()
        with mock.patch.object(TOOLCHAIN, "TOOL_ASSETS", pins):
            TOOLCHAIN.verify_tools(cache)
            name = next(iter(pins))
            original = payloads[name]
            for contents in (original + b"extra", bytes([original[0] ^ 1]) + original[1:]):
                with self.subTest(contents=contents):
                    put(cache / name, contents, executable=True)
                    with self.assertRaises(ValueError):
                        TOOLCHAIN.verify_tools(cache)
            put(cache / name, original)
            with self.assertRaisesRegex(ValueError, "not executable"):
                TOOLCHAIN.verify_tools(cache)
        self.urlopen.assert_not_called()

    def test_tool_cache_rejects_extra_or_missing_entries(self):
        cache, pins, _ = self.tool_cache()
        with mock.patch.object(TOOLCHAIN, "TOOL_ASSETS", pins):
            extra = put(cache / "unexpected-download")
            with self.assertRaises(ValueError):
                TOOLCHAIN.verify_tools(cache)
            extra.unlink()
            (cache / next(iter(pins))).unlink()
            with self.assertRaises(ValueError):
                TOOLCHAIN.verify_tools(cache)

    def test_tool_cache_rejects_linked_file_directory_and_ancestor(self):
        cache, pins, payloads = self.tool_cache()
        name = next(iter(pins))
        outside = put(self.root / "outside-tool", payloads[name], executable=True)
        (cache / name).unlink()
        (cache / name).symlink_to(outside)
        with mock.patch.object(TOOLCHAIN, "TOOL_ASSETS", pins):
            with self.assertRaises(ValueError):
                TOOLCHAIN.verify_tools(cache)
            (cache / name).unlink()
            put(cache / name, payloads[name], executable=True)
            alias = self.root / "linked-cache"
            alias.symlink_to(cache, target_is_directory=True)
            with self.assertRaises(ValueError):
                TOOLCHAIN.verify_tools(alias)
            parent_alias = self.root / "linked-parent"
            parent_alias.symlink_to(self.root, target_is_directory=True)
            with self.assertRaises(ValueError):
                TOOLCHAIN.verify_tools(parent_alias / "tools")
        self.assertEqual(outside.read_bytes(), payloads[name])

    def test_fetch_reuses_verified_cache_without_network(self):
        data = b"Pinned cache bytes"
        path = put(self.root / "cache/tool", data)
        TOOLCHAIN.fetch_pinned(path, "https://example.invalid/tool", len(data), digest(data))
        self.assertEqual(path.read_bytes(), data)
        self.urlopen.assert_not_called()

    def test_fetch_refuses_corrupt_or_linked_cache_without_overwriting_or_network(self):
        wanted = b"expected"
        path = put(self.root / "cache/tool", b"tampered")
        with self.assertRaisesRegex(ValueError, "checksum"):
            TOOLCHAIN.fetch_pinned(path, "https://example.invalid/tool", len(wanted), digest(wanted))
        self.assertEqual(path.read_bytes(), b"tampered")
        path.unlink()
        path.symlink_to(self.root / "nonexistent-target")
        with self.assertRaises(ValueError):
            TOOLCHAIN.fetch_pinned(path, "https://example.invalid/tool", len(wanted), digest(wanted))
        self.assertTrue(path.is_symlink())
        self.urlopen.assert_not_called()

    def test_successful_fetch_publishes_only_verified_complete_file(self):
        data = b"Pinned downloaded bytes"
        path = self.root / "cache/tool"
        response = Response(data)
        self.urlopen.side_effect = None
        self.urlopen.return_value = response
        TOOLCHAIN.fetch_pinned(path, "https://example.invalid/tool", len(data), digest(data))
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(list(path.parent.iterdir()), [path])
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.invalid/tool")
        self.assertEqual(self.urlopen.call_args.kwargs["timeout"], 90)
        self.assertTrue(all(0 < size <= len(data) + 1 for size in response.read_sizes))

    def test_failed_downloads_leave_no_asset_or_temporary_file(self):
        expected = b"correct"
        cases = {
            "short": Response(expected[:-1]),
            "oversized": Response(expected + b"unexpected"),
            "checksum": Response(b"wrong!!"),
            "interrupted": Response(expected, fail_after_first_read=True),
        }
        for name, response in cases.items():
            with self.subTest(failure=name):
                path = self.root / name / "tool"
                self.urlopen.side_effect = None
                self.urlopen.return_value = response
                with self.assertRaises((ValueError, OSError)):
                    TOOLCHAIN.fetch_pinned(path, "https://example.invalid/tool", len(expected), digest(expected))
                self.assertFalse(path.exists())
                self.assertEqual(list(path.parent.iterdir()), [])

    def test_connection_failure_preserves_other_cached_inputs(self):
        path = self.root / "cache/tool"
        other = put(path.parent / "retained", b"Other verified cache entry")
        self.urlopen.side_effect = urllib.error.URLError("Synthetic connection failure")
        with self.assertRaises(urllib.error.URLError):
            TOOLCHAIN.fetch_pinned(path, "https://example.invalid/tool", 1, digest(b"x"))
        self.assertFalse(path.exists())
        self.assertEqual(list(path.parent.iterdir()), [other])
        self.assertEqual(other.read_bytes(), b"Other verified cache entry")

    def test_fetch_does_not_replace_file_created_while_downloading(self):
        data = b"Pinned bytes"
        path = self.root / "cache/tool"

        def respond(*args, **kwargs):
            put(path, b"Concurrent owner's file")
            return Response(data)

        self.urlopen.side_effect = respond
        with self.assertRaisesRegex(ValueError, "appeared"):
            TOOLCHAIN.fetch_pinned(path, "https://example.invalid/tool", len(data), digest(data))
        self.assertEqual(path.read_bytes(), b"Concurrent owner's file")
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_linked_download_ancestor_is_rejected_before_creating_directories(self):
        outside = self.root / "outside"
        outside.mkdir()
        alias = self.root / "linked-cache"
        alias.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            TOOLCHAIN.fetch_pinned(alias / "new-directory/tool", "https://example.invalid/tool", 1, digest(b"x"))
        self.assertEqual(list(outside.iterdir()), [])
        self.urlopen.assert_not_called()

    def test_fetch_tools_sets_execute_permission_and_verifies_the_complete_set(self):
        cache = self.root / "fetched-tools"
        payloads = {name: ("Public fixture " + name).encode() for name in TOOLCHAIN.TOOL_ASSETS}
        pins = {name: ("https://example.invalid/" + name, len(data), digest(data))
                for name, data in payloads.items()}
        self.urlopen.side_effect = lambda request, **kwargs: Response(payloads[request.full_url.rsplit("/", 1)[1]])
        with mock.patch.object(TOOLCHAIN, "TOOL_ASSETS", pins):
            TOOLCHAIN.fetch_tools(cache)
            TOOLCHAIN.verify_tools(cache)
        self.assertEqual(self.urlopen.call_count, 4)
        self.assertEqual({p.name for p in cache.iterdir()}, set(payloads))

    def test_five_qt_modules_keep_version_metadata_license_and_public_sources(self):
        source = self.root / "source"
        modules = ("qtbase", "qtdeclarative", "qtsvg", "qtimageformats", "qtwayland")
        pins = dict(self.archive_fixture(source, module)[:2] for module in modules)
        with mock.patch.object(TOOLCHAIN, "SOURCE_PINS", pins):
            TOOLCHAIN.prepare_qt_sources(source)
            for module in modules:
                destination = self.source_base(source) / module
                self.assertIn('QT_REPO_MODULE_VERSION "6.8.3"', (destination / ".cmake.conf").read_text())
                self.assertEqual((destination / "LICENSES/LGPL-3.0-only.txt").read_bytes(), b"Synthetic Qt license")
                self.assertEqual((destination / "src/public_source.cpp").read_bytes(), b"Synthetic public Qt source")
            self.assertEqual({p.name for p in self.source_base(source).iterdir()}, set(modules))
            with mock.patch.object(TOOLCHAIN.zipfile, "ZipFile", side_effect=AssertionError("Unnecessary re-extraction")):
                TOOLCHAIN.prepare_qt_sources(source)
        self.urlopen.assert_not_called()

    def test_qt_extraction_rejects_escaping_duplicate_and_link_members(self):
        prefix = "qtbase-everywhere-src-6.8.3/"
        cases = {
            "traversal": [(prefix + "../escaped", b"bad", stat.S_IFREG | 0o644)],
            "absolute": [(str(self.root / "escaped"), b"bad", stat.S_IFREG | 0o644)],
            "backslash": [(prefix + "sub\\escaped", b"bad", stat.S_IFREG | 0o644)],
            "wrong-root": [("other-module/file", b"bad", stat.S_IFREG | 0o644)],
            "duplicate": [(prefix + "duplicate", b"one", stat.S_IFREG | 0o644),
                          (prefix + "duplicate", b"two", stat.S_IFREG | 0o644)],
            "symlink": [(prefix + "link", b"../../escaped", stat.S_IFLNK | 0o777)],
        }
        for name, extra in cases.items():
            with self.subTest(member=name):
                source = self.root / name
                archive, pin, _ = self.archive_fixture(source, extra=extra)
                with mock.patch.object(TOOLCHAIN, "SOURCE_PINS", {archive: pin}):
                    with self.assertRaises(ValueError):
                        TOOLCHAIN.prepare_qt_sources(source)
                self.assert_no_source_output(source)
                self.assertFalse((self.root / "escaped").exists())

    def test_qt_version_mismatch_or_missing_metadata_is_not_installed(self):
        for name, version, include_config in (("old", "6.8.2", True), ("similar", "6.8.30", True),
                                              ("missing", "", False)):
            with self.subTest(metadata=name):
                source = self.root / name
                archive, pin, _ = self.archive_fixture(source, version=version, include_config=include_config)
                with mock.patch.object(TOOLCHAIN, "SOURCE_PINS", {archive: pin}):
                    with self.assertRaises(ValueError):
                        TOOLCHAIN.prepare_qt_sources(source)
                self.assert_no_source_output(source)

    def test_existing_source_still_requires_a_verified_archive_and_valid_metadata(self):
        source = self.root / "source"
        archive, pin, cached = self.archive_fixture(source)
        destination = self.source_base(source) / "qtbase"
        put(destination / ".cmake.conf", b'set(QT_REPO_MODULE_VERSION "6.8.2")\n')
        with mock.patch.object(TOOLCHAIN, "SOURCE_PINS", {archive: pin}):
            with self.assertRaisesRegex(ValueError, "wrong version"):
                TOOLCHAIN.prepare_qt_sources(source)
            put(destination / ".cmake.conf", b'set(QT_REPO_MODULE_VERSION "6.8.3")\n')
            cached.write_bytes(b"tampered archive")
            with self.assertRaises(ValueError):
                TOOLCHAIN.prepare_qt_sources(source)
        self.assertEqual(list(self.source_base(source).glob(".qt-source-*")), [])

    def test_source_directory_and_metadata_links_are_refused(self):
        source = self.root / "source"
        archive, pin, _ = self.archive_fixture(source)
        outside = self.root / "outside-source"
        put(outside / ".cmake.conf", b'set(QT_REPO_MODULE_VERSION "6.8.3")\n')
        base = self.source_base(source)
        base.mkdir(parents=True)
        (base / "qtbase").symlink_to(outside, target_is_directory=True)
        with mock.patch.object(TOOLCHAIN, "SOURCE_PINS", {archive: pin}):
            with self.assertRaises(ValueError):
                TOOLCHAIN.prepare_qt_sources(source)
            (base / "qtbase").unlink()
            (base / "qtbase").mkdir()
            (base / "qtbase/.cmake.conf").symlink_to(outside / ".cmake.conf")
            with self.assertRaises(ValueError):
                TOOLCHAIN.prepare_qt_sources(source)
        self.assertEqual({p.name for p in outside.iterdir()}, {".cmake.conf"})

    def test_linked_source_ancestor_does_not_create_directories_in_its_target(self):
        source = self.root / "source"
        archive, pin, _ = self.archive_fixture(source)
        outside = self.root / "outside-source"
        outside.mkdir()
        (source / "qt-native/.tools/Qt").symlink_to(outside, target_is_directory=True)
        with mock.patch.object(TOOLCHAIN, "SOURCE_PINS", {archive: pin}):
            with self.assertRaises(ValueError):
                TOOLCHAIN.prepare_qt_sources(source)
        self.assertEqual(list(outside.iterdir()), [])
        self.urlopen.assert_not_called()

    def test_source_archive_budget_is_checked_before_any_member_is_opened(self):
        source = self.root / "source"
        archive, pin, _ = self.archive_fixture(source)
        fake_zip = mock.MagicMock()
        oversized = zipfile.ZipInfo("qtbase-everywhere-src-6.8.3/oversized")
        oversized.file_size = 2 * 1024 ** 3 + 1
        fake_zip.__enter__.return_value.infolist.return_value = [oversized]
        with mock.patch.object(TOOLCHAIN, "SOURCE_PINS", {archive: pin}), \
                mock.patch.object(TOOLCHAIN.zipfile, "ZipFile", return_value=fake_zip):
            with self.assertRaisesRegex(ValueError, "budget"):
                TOOLCHAIN.prepare_qt_sources(source)
        fake_zip.__enter__.return_value.open.assert_not_called()
        self.assert_no_source_output(source)

    def test_source_archive_member_count_budget_precedes_extraction(self):
        source = self.root / "source"
        archive, pin, _ = self.archive_fixture(source)
        fake_zip = mock.MagicMock()
        member = zipfile.ZipInfo("qtbase-everywhere-src-6.8.3/entry")
        member.file_size = 0
        fake_zip.__enter__.return_value.infolist.return_value = [member] * 100001
        with mock.patch.object(TOOLCHAIN, "SOURCE_PINS", {archive: pin}), \
                mock.patch.object(TOOLCHAIN.zipfile, "ZipFile", return_value=fake_zip):
            with self.assertRaisesRegex(ValueError, "budget"):
                TOOLCHAIN.prepare_qt_sources(source)
        fake_zip.__enter__.return_value.open.assert_not_called()
        self.assert_no_source_output(source)

    def test_invalid_zip_cleans_staging_and_does_not_publish_a_module(self):
        source = self.root / "source"
        archive = "qtbase-everywhere-src-6.8.3.zip"
        data = b"not a zip archive"
        put(source / "qt-native/.tools/downloads" / archive, data)
        with mock.patch.object(TOOLCHAIN, "SOURCE_PINS", {archive: (len(data), digest(data))}):
            with self.assertRaises(zipfile.BadZipFile):
                TOOLCHAIN.prepare_qt_sources(source)
        self.assert_no_source_output(source)


if __name__ == "__main__":
    unittest.main()
