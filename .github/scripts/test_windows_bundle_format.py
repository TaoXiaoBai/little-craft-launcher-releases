#!/usr/bin/env python3
"""Exercise the actual PE/resource/index/ZIP boundary without running Windows code.

The fixtures deliberately contain real, bounded AMD64 PE structures rather than
an MZ marker.  They are tiny synthetic programs and fake Qt files, never release
artifacts.  Publisher tests can reuse build_runtime_payload and build_bundle.
"""

import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile


SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))
import windows_bundle_format as BUNDLE


TAG = "v1.0.0-pre.10"
ENTRYPOINT = "mclauncher-qt.exe"
MAGIC = b"LCLQT_BUNDLE_V1\0"
HEADER = struct.Struct("<16sIIQII32s")
RECORD = struct.Struct("<IQ32s")
PE_OFFSET = 0x80
OPTIONAL_OFFSET = PE_OFFSET + 24
SECTION_OFFSET = OPTIONAL_OFFSET + 0xF0
RESOURCE_RAW = 0x400
RESOURCE_RVA = 0x2000


def align(value, unit):
    return (value + unit - 1) // unit * unit


def zip_bytes(entries, compression=zipfile.ZIP_DEFLATED):
    """Write real ZIP metadata; entries may use ZipInfo for hostile attributes."""
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
        with zipfile.ZipFile(output, "w", compression=compression) as archive:
            for name, content in entries:
                if isinstance(name, str):
                    info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = (stat.S_IFDIR | 0o755 if name.endswith("/")
                                          else stat.S_IFREG | 0o644) << 16
                    info.compress_type = compression
                else:
                    info = name
                archive.writestr(info, content)
    return output.getvalue()


def _resource_bytes(resources, languages):
    """Build type -> numeric ID -> neutral-language resource directories."""
    items = list(resources.items()) if isinstance(resources, dict) else list(resources)
    if not items:
        return b""
    root = 0
    type_directory = 24
    cursor = type_directory + 16 + 8 * len(items)
    language_directories = []
    for _ in items:
        language_directories.append(cursor)
        cursor += 16 + 8 * len(languages)
    data_records = []
    for _ in items:
        records = []
        for _ in languages:
            records.append(cursor)
            cursor += 16
        data_records.append(records)
    data_start = align(cursor, 8)
    output = bytearray(data_start)

    def directory(offset, count):
        struct.pack_into("<IIHHHH", output, offset, 0, 0, 0, 0, 0, count)

    directory(root, 1)
    struct.pack_into("<II", output, 16, 10, 0x80000000 | type_directory)
    directory(type_directory, len(items))
    for number, (resource_id, content) in enumerate(items):
        language_directory = language_directories[number]
        struct.pack_into("<II", output, type_directory + 16 + number * 8,
                         resource_id, 0x80000000 | language_directory)
        directory(language_directory, len(languages))
        for language_number, language in enumerate(languages):
            record = data_records[number][language_number]
            struct.pack_into("<II", output, language_directory + 16 + language_number * 8,
                             language, record)
            output.extend(b"\0" * (align(len(output), 8) - len(output)))
            payload_offset = len(output)
            output.extend(content)
            struct.pack_into("<IIII", output, record,
                             RESOURCE_RVA + payload_offset, len(content), 0, 0)
    return bytes(output)


def build_pe(resources=None, imports=("KERNEL32.dll",), *, machine=0x8664,
             subsystem=2, languages=(0,)):
    """A valid PE32+ GUI with executable entrypoint and actual import descriptors."""
    resource = _resource_bytes(resources or {}, languages)
    resource_size = align(max(1, len(resource)), 0x200)
    import_rva = align(RESOURCE_RVA + max(1, len(resource)), 0x1000)
    import_raw = RESOURCE_RAW + resource_size
    imports = tuple(imports)
    idata = bytearray((len(imports) + 1) * 20)
    for number, name in enumerate(imports):
        name_offset = len(idata)
        idata.extend(name.encode("ascii") + b"\0")
        while len(idata) % 8:
            idata.append(0)
        hint_offset = len(idata)
        idata.extend(b"\0\0GetLastError\0")
        while len(idata) % 8:
            idata.append(0)
        thunk_offset = len(idata)
        idata.extend(struct.pack("<QQ", import_rva + hint_offset, 0))
        first_thunk = len(idata)
        idata.extend(struct.pack("<QQ", import_rva + hint_offset, 0))
        struct.pack_into("<IIIII", idata, number * 20, import_rva + thunk_offset,
                         0, 0, import_rva + name_offset, import_rva + first_thunk)
    import_size = align(max(1, len(idata)), 0x200)
    output = bytearray(import_raw + import_size)
    output[:2] = b"MZ"
    struct.pack_into("<I", output, 0x3C, PE_OFFSET)
    output[PE_OFFSET:PE_OFFSET + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", output, PE_OFFSET + 4,
                     machine, 3, 0, 0, 0, 0xF0, 0x22)
    struct.pack_into("<H", output, OPTIONAL_OFFSET, 0x20B)
    struct.pack_into("<III", output, OPTIONAL_OFFSET + 4, 0x200,
                     resource_size + import_size, 0)
    struct.pack_into("<IIQII", output, OPTIONAL_OFFSET + 16,
                     0x1000, 0x1000, 0x140000000, 0x1000, 0x200)
    struct.pack_into("<HHHHHH", output, OPTIONAL_OFFSET + 40, 6, 0, 0, 0, 6, 0)
    struct.pack_into("<II", output, OPTIONAL_OFFSET + 56,
                     align(import_rva + len(idata), 0x1000), 0x200)
    struct.pack_into("<HH", output, OPTIONAL_OFFSET + 68, subsystem, 0x8160)
    struct.pack_into("<QQQQII", output, OPTIONAL_OFFSET + 72,
                     0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    if imports:
        struct.pack_into("<II", output, OPTIONAL_OFFSET + 112 + 8,
                         import_rva, (len(imports) + 1) * 20)
    if resource:
        struct.pack_into("<II", output, OPTIONAL_OFFSET + 112 + 16,
                         RESOURCE_RVA, len(resource))

    def section(number, name, virtual_size, rva, raw_size, raw, characteristics):
        position = SECTION_OFFSET + number * 40
        output[position:position + 8] = name.ljust(8, b"\0")
        struct.pack_into("<IIIIIIHHI", output, position + 8,
                         virtual_size, rva, raw_size, raw, 0, 0, 0, 0, characteristics)

    section(0, b".text", 1, 0x1000, 0x200, 0x200, 0x60000020)
    section(1, b".rsrc", max(1, len(resource)), RESOURCE_RVA,
            resource_size, RESOURCE_RAW, 0x40000040)
    section(2, b".idata", len(idata), import_rva, import_size, import_raw, 0xC0000040)
    output[0x200] = 0xC3  # ret: this fixture is inspected, never executed.
    output[RESOURCE_RAW:RESOURCE_RAW + len(resource)] = resource
    output[import_raw:import_raw + len(idata)] = idata
    return bytes(output)


def runtime_entries(tag=TAG, overrides=None, *, manifest_overrides=None, sums_override=None):
    files = {
        ENTRYPOINT: build_pe(),
        "Qt6Core.dll": b"synthetic Qt core",
        "Qt6Gui.dll": b"synthetic Qt gui",
        "Qt6Network.dll": b"synthetic Qt network",
        "Qt6Qml.dll": b"synthetic Qt qml",
        "Qt6Quick.dll": b"synthetic Qt quick",
        "platforms/qwindows.dll": b"synthetic platform plugin",
        "imageformats/qwebp.dll": b"synthetic WebP plugin",
        "qml/QtQuick/qmldir": b"module QtQuick\n",
        "licenses/Qt/LICENSE.LGPL3": b"synthetic licence text",
        "SUPPORTING_MATERIALS.zip": zip_bytes([("NOTICE.txt", b"synthetic supporting materials")]),
    }
    for name, content in (overrides or {}).items():
        if content is None:
            files.pop(name, None)
        else:
            files[name] = content
    manifest = {
        "schema": 1, "version": tag, "platform": "windows-x64", "qt": "6.8.3",
        "linkage": "dynamic",
        "files": [{"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                  for name, content in sorted(files.items())],
    }
    manifest.update(manifest_overrides or {})
    files["runtime-manifest.json"] = json.dumps(manifest, sort_keys=True).encode("utf-8")
    sums = "".join(f"{hashlib.sha256(content).hexdigest()}  {name}\n"
                   for name, content in sorted(files.items())).encode("utf-8")
    files["SHA256SUMS.txt"] = sums if sums_override is None else sums_override
    return files


def build_runtime_payload(tag=TAG, overrides=None, extra_entries=(), *,
                          manifest_overrides=None, sums_override=None,
                          compression=zipfile.ZIP_DEFLATED):
    files = runtime_entries(tag, overrides, manifest_overrides=manifest_overrides,
                            sums_override=sums_override)
    return zip_bytes([*sorted(files.items()), *extra_entries], compression=compression)


def reference_index(payload, tag=TAG, entries=None):
    """An independent encoder, also able to wrap intentionally hostile payloads."""
    if entries is None:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = [(info.filename, archive.read(info)) for info in archive.infolist()
                       if not info.is_dir()]
    entries = sorted(entries)
    tag_bytes, entry_bytes = tag.encode("utf-8"), ENTRYPOINT.encode("utf-8")
    output = bytearray(HEADER.pack(MAGIC, 1, len(entries), len(payload), len(tag_bytes),
                                  len(entry_bytes), hashlib.sha256(payload).digest()))
    output.extend(tag_bytes + entry_bytes)
    for name, content in entries:
        name = name.encode("utf-8")
        output.extend(RECORD.pack(len(name), len(content), hashlib.sha256(content).digest()))
        output.extend(name)
    return bytes(output)


def build_bundle(payload=None, tag=TAG, index=None, **pe_options):
    if payload is None:
        payload = build_runtime_payload(tag)
    if index is None:
        index = reference_index(payload, tag)
    return build_pe({21001: index, 21002: payload}, **pe_options)


def patch_u16(data, offset, value):
    output = bytearray(data)
    struct.pack_into("<H", output, offset, value)
    return bytes(output)


def patch_u32(data, offset, value):
    output = bytearray(data)
    struct.pack_into("<I", output, offset, value)
    return bytes(output)


def patch_zip_field(payload, local_offset, central_offset, value):
    """Patch matching local/central headers rather than mocking ZipFile."""
    output = bytearray(payload)
    local = output.index(b"PK\x03\x04")
    central = output.index(b"PK\x01\x02")
    struct.pack_into("<H", output, local + local_offset, value)
    struct.pack_into("<H", output, central + central_offset, value)
    return bytes(output)


class WindowsBundleFormatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bundle fixture 空格-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.payload = build_runtime_payload()
        self.payload_path = self.root / "runtime payload.zip"
        self.payload_path.write_bytes(self.payload)
        self.index = reference_index(self.payload)
        self.exe = build_bundle(self.payload, index=self.index)

    def rejects_payload(self, payload):
        self.payload_path.write_bytes(payload)
        with self.assertRaises(BUNDLE.BundleError):
            BUNDLE.create_index(self.payload_path, TAG)

    def rejects_exe(self, exe):
        with self.assertRaises(BUNDLE.BundleError):
            BUNDLE.inspect_bundle(exe, TAG)

    def test_index_matches_independent_encoder_byte_for_byte(self):
        self.assertEqual(BUNDLE.create_index(self.payload_path, TAG), self.index)
        result = BUNDLE.parse_index(self.index, TAG)
        self.assertEqual(result["tag"], TAG)
        self.assertEqual(result["entrypoint"], ENTRYPOINT)
        self.assertEqual(result["file_count"], len(runtime_entries()))
        self.assertEqual(result["payload_size"], len(self.payload))
        self.assertEqual(result["payload_sha256"], hashlib.sha256(self.payload).hexdigest())

    def test_real_pe_resources_and_full_runtime_validate_from_bytes_and_path(self):
        path = self.root / ENTRYPOINT
        path.write_bytes(self.exe)
        for source in (self.exe, path):
            with self.subTest(source=type(source).__name__):
                result = BUNDLE.inspect_bundle(source, TAG)
                self.assertEqual(result["tag"], TAG)
                self.assertEqual(result["entrypoint"], ENTRYPOINT)
                self.assertEqual(result["payload_sha256"], hashlib.sha256(self.payload).hexdigest())
                self.assertEqual([name.lower() for name in result["stub_imports"]], ["kernel32.dll"])

    def test_stored_zip_members_and_safe_directory_entries_are_supported(self):
        payload = build_runtime_payload(extra_entries=[("qml/", b""), ("qml/QtQuick/", b"")],
                                        compression=zipfile.ZIP_STORED)
        self.payload_path.write_bytes(payload)
        index = BUNDLE.create_index(self.payload_path, TAG)
        result = BUNDLE.inspect_bundle(build_bundle(payload, index=index), TAG)
        self.assertEqual(result["file_count"], len(runtime_entries()))

    def test_unicode_runtime_paths_are_rejected_but_artifact_paths_can_be_unicode(self):
        payload = build_runtime_payload(overrides={"licenses/说明 文件.txt": b"hello"})
        self.rejects_payload(payload)
        exe_path = self.root / "启动器 空格.exe"
        exe_path.write_bytes(self.exe)
        self.assertEqual(BUNDLE.inspect_bundle(exe_path, TAG)["tag"], TAG)

    def test_tag_mismatch_rejects_index_and_embedded_bundle(self):
        with self.assertRaises(BUNDLE.BundleError):
            BUNDLE.parse_index(self.index, "v1.0.0-pre.11")
        with self.assertRaises(BUNDLE.BundleError):
            BUNDLE.inspect_bundle(self.exe, "v1.0.0-pre.11")

    def test_index_header_and_record_bounds_are_checked(self):
        first_record = HEADER.size + len(TAG.encode()) + len(ENTRYPOINT.encode())
        cases = {
            "truncated header": self.index[:HEADER.size - 1],
            "bad magic": b"X" + self.index[1:],
            "unknown schema": patch_u32(self.index, 16, 2),
            "zero files": patch_u32(self.index, 20, 0),
            "too many files": patch_u32(self.index, 20, 20001),
            "missing record": patch_u32(self.index, 20, len(runtime_entries()) + 1),
            "oversize payload": self.index[:24] + struct.pack("<Q", 512 * 1024 * 1024 + 1) + self.index[32:],
            "oversize tag": patch_u32(self.index, 32, 0xFFFFFFFF),
            "empty entrypoint": patch_u32(self.index, 36, 0),
            "empty path": patch_u32(self.index, first_record, 0),
            "oversize path": patch_u32(self.index, first_record, 4097),
            "truncated record": self.index[:-1],
            "trailing bytes": self.index + b"unindexed",
        }
        for label, value in cases.items():
            with self.subTest(label=label), self.assertRaises(BUNDLE.BundleError):
                BUNDLE.parse_index(value, TAG)

    def test_index_rejects_invalid_utf8_tag_and_entrypoint(self):
        invalid_utf8 = bytearray(self.index)
        invalid_utf8[HEADER.size] = 0xFF
        wrong_entry = bytearray(self.index)
        wrong_entry[HEADER.size + len(TAG)] = ord("x")
        for value in (bytes(invalid_utf8), bytes(wrong_entry)):
            with self.assertRaises(BUNDLE.BundleError):
                BUNDLE.parse_index(value, TAG)

    def test_index_requires_sorted_unique_and_safe_paths(self):
        for entries in [
            [("../escape", b"a")], [("NUL.txt", b"a")],
            [("a", b"a"), ("A", b"b")], [("a", b"a"), ("a", b"b")],
        ]:
            entries = [*entries, (ENTRYPOINT, build_pe())]
            with self.subTest(entries=[name for name, _ in entries]), self.assertRaises(BUNDLE.BundleError):
                BUNDLE.parse_index(reference_index(self.payload, entries=entries), TAG)
        index = reference_index(self.payload, entries=[("a", b"a"), ("b", b"b"),
                                                       (ENTRYPOINT, build_pe())])
        start = HEADER.size + len(TAG) + len(ENTRYPOINT)
        record_size = RECORD.size + 1
        unsorted = (index[:start] + index[start + record_size:start + 2 * record_size]
                    + index[start:start + record_size] + index[start + 2 * record_size:])
        with self.assertRaises(BUNDLE.BundleError):
            BUNDLE.parse_index(unsorted, TAG)

    def test_index_expanded_total_is_bounded(self):
        index = bytearray(reference_index(self.payload, entries=[("a", b"a"),
                                                                 (ENTRYPOINT, build_pe())]))
        start = HEADER.size + len(TAG) + len(ENTRYPOINT)
        struct.pack_into("<Q", index, start + 4, 1024 * 1024 * 1024)
        with self.assertRaises(BUNDLE.BundleError):
            BUNDLE.parse_index(bytes(index), TAG)

    def test_index_digest_inventory_and_payload_length_are_verified(self):
        corrupt_hash = bytearray(self.index)
        corrupt_hash[40] ^= 1
        first_record = HEADER.size + len(TAG) + len(ENTRYPOINT)
        wrong_file_hash = bytearray(self.index)
        wrong_file_hash[first_record + 12] ^= 1
        wrong_file_size = bytearray(self.index)
        struct.pack_into("<Q", wrong_file_size, first_record + 4, 123456)
        with zipfile.ZipFile(io.BytesIO(self.payload)) as archive:
            missing_entries = [(info.filename, archive.read(info)) for info in archive.infolist()][1:]
        for index in [bytes(corrupt_hash), bytes(wrong_file_hash), bytes(wrong_file_size),
                      reference_index(self.payload, entries=missing_entries),
                      self.index[:24] + struct.pack("<Q", len(self.payload) - 1) + self.index[32:]]:
            with self.subTest(index_hash=hashlib.sha256(index).hexdigest()[:10]):
                self.rejects_exe(build_bundle(self.payload, index=index))

    def test_windows_unsafe_paths_are_rejected_before_index_creation(self):
        names = ["../escape.txt", "/absolute.txt", "C:/drive.txt", "C:drive.txt",
                 "\\\\server\\share\\file", "folder/file:stream", "CON", "nul.txt",
                 "COM1.log", "LPT9", "trailing.", "trailing ", "parent./child",
                 "parent /child", "a//b", "./relative", "a\\b", "line\nbreak"]
        for name in names:
            with self.subTest(name=name):
                self.rejects_payload(build_runtime_payload(extra_entries=[(name, b"unsafe")]))

    def test_duplicate_case_collisions_and_file_directory_conflicts_are_rejected(self):
        for extra in [
            [("Qt6Core.dll", b"duplicate")], [("qt6core.DLL", b"case collision")],
            [("Qt6Core.dll/child", b"under a file")],
            [("qml/", b""), ("QML/", b"")],
            [("qml/QtQuick/qmldir/", b"")],
        ]:
            with self.subTest(extra=[name for name, _ in extra]):
                self.rejects_payload(build_runtime_payload(extra_entries=extra))

    def test_symlink_and_special_zip_entries_are_rejected(self):
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o644, stat.S_IFCHR | 0o644):
            info = zipfile.ZipInfo("unsafe-node")
            info.create_system = 3
            info.external_attr = mode << 16
            with self.subTest(mode=oct(mode)):
                self.rejects_payload(build_runtime_payload(extra_entries=[(info, b"target")]))

    def test_encrypted_and_unsupported_compression_entries_are_rejected(self):
        for payload in [patch_zip_field(self.payload, 6, 8, 1),
                        patch_zip_field(self.payload, 6, 8, 0x40),
                        patch_zip_field(self.payload, 6, 8, 0x2000),
                        patch_zip_field(self.payload, 8, 10, 99)]:
            self.rejects_payload(payload)

    def test_invalid_utf8_zip_filename_is_rejected_cleanly(self):
        output = bytearray(self.payload)
        local = output.index(b"PK\x03\x04")
        central = output.index(b"PK\x01\x02")
        for position, flag_offset, name_offset in [(local, 6, 30), (central, 8, 46)]:
            flags = struct.unpack_from("<H", output, position + flag_offset)[0]
            struct.pack_into("<H", output, position + flag_offset, flags | 0x800)
            output[position + name_offset] = 0xFF
        self.rejects_payload(bytes(output))

    def test_corrupt_zip_crc_is_not_hidden_by_correct_payload_digest(self):
        payload = build_runtime_payload(compression=zipfile.ZIP_STORED)
        index = reference_index(payload)
        damaged = bytearray(payload)
        name_size, extra_size = struct.unpack_from("<HH", damaged, 26)
        damaged[30 + name_size + extra_size] ^= 1
        damaged = bytes(damaged)
        index = index[:40] + hashlib.sha256(damaged).digest() + index[72:]
        self.rejects_exe(build_bundle(damaged, index=index))
        self.rejects_payload(damaged)

    def test_directory_crc_and_local_header_are_also_validated(self):
        payload = build_runtime_payload(extra_entries=[("qml/", b"")])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            directory = archive.infolist()[-1]
        bad_crc = bytearray(payload)
        struct.pack_into("<I", bad_crc, directory.header_offset + 14, 1)
        struct.pack_into("<I", bad_crc, bad_crc.rfind(b"PK\x01\x02") + 16, 1)
        bad_name = bytearray(payload)
        bad_name[directory.header_offset + 30] = ord("x")
        for label, damaged in [("crc", bytes(bad_crc)), ("local name", bytes(bad_name))]:
            with self.subTest(label=label):
                self.rejects_payload(damaged)
                self.rejects_exe(build_bundle(damaged))

    def test_malformed_deflate_stream_is_reported_as_bundle_error(self):
        damaged = bytearray(self.payload)
        name_size, extra_size = struct.unpack_from("<HH", damaged, 26)
        # BFINAL=1, BTYPE=3 is forbidden by the deflate format.
        damaged[30 + name_size + extra_size] = 0xFF
        damaged = bytes(damaged)
        index = self.index[:40] + hashlib.sha256(damaged).digest() + self.index[72:]
        self.rejects_exe(build_bundle(damaged, index=index))
        self.rejects_payload(damaged)

    def test_local_crc_and_sizes_must_match_the_central_directory(self):
        # ZipFile uses the central sizes/CRC and tolerates these local-only
        # changes.  The embedded bundle must have one consistent description.
        for label, offset in [("CRC", 14), ("compressed size", 18), ("expanded size", 22)]:
            original = struct.unpack_from("<I", self.payload, offset)[0]
            damaged = patch_u32(self.payload, offset, original ^ 1)
            with self.subTest(field=label):
                self.rejects_payload(damaged)

    def test_eocd_counts_and_central_offset_are_not_silently_repaired(self):
        eocd = len(self.payload) - 22
        self.assertEqual(self.payload[eocd:eocd + 4], b"PK\x05\x06")
        for label, offset in [("disk entry count", 8), ("total entry count", 10)]:
            original = struct.unpack_from("<H", self.payload, eocd + offset)[0]
            with self.subTest(field=label):
                self.rejects_payload(patch_u16(self.payload, eocd + offset, original + 1))
        central = struct.unpack_from("<I", self.payload, eocd + 16)[0]
        self.rejects_payload(patch_u32(self.payload, eocd + 16, central + 1))

    def test_nonseekable_writer_data_descriptors_remain_supported(self):
        class NonseekableBytesIO(io.BytesIO):
            def seekable(self):
                return False

            def seek(self, *args):
                raise OSError("This fixture models a streaming ZIP writer")

        output = NonseekableBytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(runtime_entries().items()):
                archive.writestr(name, content)
        payload = output.getvalue()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertTrue(all(info.flag_bits & 8 for info in archive.infolist()))
        self.assertIn(b"PK\x07\x08", payload)
        self.payload_path.write_bytes(payload)
        index = BUNDLE.create_index(self.payload_path, TAG)
        self.assertEqual(BUNDLE.inspect_bundle(build_bundle(payload, index=index), TAG)["tag"], TAG)

    def test_zip_truncation_and_non_zip_are_rejected(self):
        for payload in (self.payload[:-16], b"not a zip", b"", self.payload + b"hidden",
                        b"hidden" + self.payload):
            self.rejects_payload(payload)

    def test_required_runtime_files_and_valid_supporting_archive_are_enforced(self):
        for name in [ENTRYPOINT, "Qt6Core.dll", "Qt6Gui.dll", "Qt6Network.dll", "Qt6Qml.dll",
                     "Qt6Quick.dll", "platforms/qwindows.dll", "imageformats/qwebp.dll",
                     "SUPPORTING_MATERIALS.zip"]:
            with self.subTest(missing=name):
                self.rejects_payload(build_runtime_payload(overrides={name: None}))
        for supporting in (b"not zip", zip_bytes([]), zip_bytes([("../escape", b"unsafe")])):
            with self.subTest(supporting=hashlib.sha256(supporting).hexdigest()[:8]):
                self.rejects_payload(build_runtime_payload(overrides={"SUPPORTING_MATERIALS.zip": supporting}))

    def test_runtime_manifest_identity_and_exact_inventory_are_enforced(self):
        for field, value in [("schema", 2), ("version", "v1.0.0-pre.11"),
                             ("platform", "linux-x86_64"), ("linkage", "static"),
                             ("qt", "5.15.2"), ("files", [])]:
            with self.subTest(field=field):
                self.rejects_payload(build_runtime_payload(manifest_overrides={field: value}))
        original = json.loads(runtime_entries()["runtime-manifest.json"])["files"]
        for records in [original[:-1], original + [original[0]],
                        [dict(original[0], sha256="0" * 64), *original[1:]],
                        [dict(original[0], size=999), *original[1:]],
                        [dict(original[0], path="../escape"), *original[1:]]]:
            self.rejects_payload(build_runtime_payload(manifest_overrides={"files": records}))

    def test_deep_and_nonfinite_json_are_reported_as_bundle_errors(self):
        original = runtime_entries()["runtime-manifest.json"]
        # CPython 3.12's C decoder has a separate guard and can accept nesting
        # deeper than sys.getrecursionlimit(); 10,000 reaches either guard.
        depth = max(10000, sys.getrecursionlimit() + 100)
        values = [("deep JSON", b"[" * depth + b"0" + b"]" * depth),
                  ("NaN", b"NaN"), ("Infinity", b"Infinity"), ("negative Infinity", b"-Infinity")]
        for label, value in values:
            files = runtime_entries()
            # A harmless extra key would otherwise be accepted.  Keep identity,
            # inventory and checksum bytes correct so parsing is the failure.
            files["runtime-manifest.json"] = original[:-1] + b', "extra": ' + value + b"}"
            files["SHA256SUMS.txt"] = "".join(
                f"{hashlib.sha256(content).hexdigest()}  {name}\n"
                for name, content in sorted(files.items()) if name != "SHA256SUMS.txt"
            ).encode("ascii")
            payload = zip_bytes(sorted(files.items()))
            with self.subTest(value=label):
                self.rejects_payload(payload)
                self.rejects_exe(build_bundle(payload))

    def test_checksum_manifest_requires_exact_hashes_and_inventory(self):
        sums = runtime_entries()["SHA256SUMS.txt"]
        lines = sums.splitlines(keepends=True)
        cases = [b"", b"malformed\n", b"0" * 64 + lines[0][64:] + b"".join(lines[1:]),
                 b"".join(lines[1:]), sums + lines[0],
                 sums + b"0" * 64 + b"  unknown.txt\n",
                 sums + b"0" * 64 + b"  SHA256SUMS.txt\n"]
        for number, value in enumerate(cases):
            with self.subTest(case=number):
                self.rejects_payload(build_runtime_payload(sums_override=value))

    def test_inner_executable_accepts_console_or_gui_but_requires_amd64_pe(self):
        console_payload = build_runtime_payload(overrides={ENTRYPOINT: build_pe(subsystem=3)})
        self.payload_path.write_bytes(console_payload)
        index = BUNDLE.create_index(self.payload_path, TAG)
        BUNDLE.inspect_bundle(build_bundle(console_payload, index=index), TAG)
        for inner in [b"MZfake", build_pe(machine=0x14C), build_pe(subsystem=0)]:
            self.rejects_payload(build_runtime_payload(overrides={ENTRYPOINT: inner}))

    def test_outer_requires_pe32plus_amd64_gui_and_executable_entrypoint(self):
        cases = [b"MZfake", self.exe[:-1],
                 patch_u32(self.exe, 0x3C, len(self.exe) + 1),
                 patch_u16(self.exe, PE_OFFSET + 4, 0x14C),
                 patch_u16(self.exe, PE_OFFSET + 22, 0),
                 patch_u16(self.exe, PE_OFFSET + 22, 0x2022),
                 patch_u16(self.exe, OPTIONAL_OFFSET, 0x10B),
                 patch_u16(self.exe, OPTIONAL_OFFSET + 68, 3),
                 patch_u32(self.exe, OPTIONAL_OFFSET + 108, 17),
                 patch_u32(self.exe, OPTIONAL_OFFSET + 16, 0x7FFFFFFF),
                 patch_u32(self.exe, SECTION_OFFSET + 36, 0x40000020),
                 patch_u32(self.exe, SECTION_OFFSET + 20, len(self.exe) + 1),
                 patch_u32(self.exe, SECTION_OFFSET + 40 + 12, 0x1000),
                 patch_u32(self.exe, SECTION_OFFSET + 40 + 20, 0x200),
                 patch_u32(self.exe, SECTION_OFFSET + 40 + 8, 0xFFFFFFFF)]
        for number, value in enumerate(cases):
            with self.subTest(case=number):
                self.rejects_exe(value)

    def test_outer_rejects_non_system_or_absent_imports(self):
        for imports in [(), ("Qt6Core.dll",), ("KERNEL32.dll", "libstdc++-6.dll"),
                        ("KERNEL32.dll", "evil.dll"), ("C:\\Windows\\kernel32.dll",),
                        ("KERNEL32.dll", "KERNEL32.dll")]:
            with self.subTest(imports=imports):
                self.rejects_exe(build_bundle(self.payload, imports=imports))

    def test_pe_import_rva_and_descriptor_bounds_are_checked(self):
        import_directory = OPTIONAL_OFFSET + 112 + 8
        cases = [patch_u32(self.exe, import_directory, 0x7FFFFFFF),
                 patch_u32(self.exe, import_directory + 4, 19)]
        import_raw = struct.unpack_from("<I", self.exe, SECTION_OFFSET + 2 * 40 + 20)[0]
        cases += [patch_u32(self.exe, import_raw + 12, 0x7FFFFFFF)]
        delayed = bytearray(self.exe)
        struct.pack_into("<II", delayed, OPTIONAL_OFFSET + 112 + 13 * 8,
                         *struct.unpack_from("<II", self.exe, import_directory))
        cases.append(bytes(delayed))
        for value in cases:
            self.rejects_exe(value)

    def test_resource_tree_must_have_unique_neutral_index_and_payload(self):
        for resources in [{21001: self.index}, {21002: self.payload},
                          {21001: self.index, 21002: self.payload, 21003: b"undeclared RCDATA"},
                          [(21001, self.index), (21001, self.index), (21002, self.payload)]]:
            self.rejects_exe(build_pe(resources))
        for languages in [(1033,), (0, 0), (0, 1033)]:
            with self.subTest(languages=languages):
                self.rejects_exe(build_bundle(self.payload, languages=languages))

    def test_resource_rvas_sizes_tree_cycles_and_data_bounds_are_checked(self):
        # With two resource IDs and one language each the first data entry is at 104.
        resource_directory = OPTIONAL_OFFSET + 112 + 16
        cases = [patch_u32(self.exe, resource_directory, 0x7FFFFFFF),
                 patch_u32(self.exe, resource_directory + 4, 24),
                 patch_u32(self.exe, RESOURCE_RAW + 20, 0x80000000),
                 patch_u32(self.exe, RESOURCE_RAW + 104, 0x7FFFFFFF),
                 patch_u32(self.exe, RESOURCE_RAW + 108, len(self.exe) + 1),
                 patch_u32(self.exe, RESOURCE_RAW + 112, 65001)]
        for number, value in enumerate(cases):
            with self.subTest(case=number):
                self.rejects_exe(value)

    def test_final_windows_zip_contains_exactly_one_regular_named_executable(self):
        archive = self.root / "windows.zip"
        archive.write_bytes(zip_bytes([(ENTRYPOINT, self.exe)]))
        self.assertEqual(BUNDLE.inspect_windows_zip(archive, TAG)["tag"], TAG)
        symlink = zipfile.ZipInfo(ENTRYPOINT)
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = [[], [("launcher.exe", self.exe)], [("nested/" + ENTRYPOINT, self.exe)],
                 [(ENTRYPOINT, self.exe), ("README.txt", b"extra")],
                 [(ENTRYPOINT, self.exe), ("empty-directory/", b"")],
                 [(ENTRYPOINT, self.exe), (ENTRYPOINT, self.exe)], [(symlink, self.exe)],
                 [(ENTRYPOINT, b"MZnot-a-bundle")]]
        for number, entries in enumerate(cases):
            archive.write_bytes(zip_bytes(entries))
            with self.subTest(case=number), self.assertRaises(BUNDLE.BundleError):
                BUNDLE.inspect_windows_zip(archive, TAG)

    def test_outer_executable_uses_its_bundle_limit_not_the_inner_app_limit(self):
        archive = self.root / "outer-limit.zip"
        archive.write_bytes(zip_bytes([(ENTRYPOINT, self.exe)]))
        read_inventory = BUNDLE._zip_inventory
        # Scale down only the default app capture bound, keeping real ZIP and PE
        # reads.  The wrapper is several times larger than the embedded app, just
        # as with an actual Qt runtime, without allocating a 128 MiB fixture.
        app_limit = len(build_pe()) + 1
        self.assertLess(app_limit, len(self.exe))

        def reduced_app_capture(data, capture=(), capture_limits=None):
            limits = {ENTRYPOINT: app_limit}
            limits.update(capture_limits or {})
            return read_inventory(data, capture, limits)

        with mock.patch.object(BUNDLE, "_zip_inventory", side_effect=reduced_app_capture):
            self.assertEqual(BUNDLE.inspect_windows_zip(archive, TAG)["tag"], TAG)

    def test_command_line_creates_and_inspects_actual_artifacts(self):
        index = self.root / "bundle index.bin"
        command = [sys.executable, str(SCRIPT_ROOT / "windows_bundle_format.py")]
        result = subprocess.run([*command, "create-index", "--payload", str(self.payload_path),
                                 "--tag", TAG, "--output", str(index)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(index.read_bytes(), self.index)
        exe = self.root / ENTRYPOINT
        exe.write_bytes(self.exe)
        archive = self.root / "outer.zip"
        archive.write_bytes(zip_bytes([(ENTRYPOINT, self.exe)]))
        for arguments in [["inspect", "--exe", str(exe)],
                          ["inspect-zip", "--archive", str(archive)]]:
            result = subprocess.run([*command, *arguments, "--tag", TAG], capture_output=True,
                                    text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["tag"], TAG)
        exe.write_bytes(b"MZbad")
        result = subprocess.run([*command, "inspect", "--exe", str(exe), "--tag", TAG],
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
