#!/usr/bin/env python3
"""Build and inspect the Windows runtime bundle without executing Windows code.

The native bootstrap and this reader share the bounded v1 RCDATA index.  This
module deliberately uses only the Python standard library so the publication
gate also runs on the Linux release runner.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import struct
import zipfile
import zlib


MAGIC = b"LCLQT_BUNDLE_V1\x00"
HEADER = struct.Struct("<16sIIQII32s")
RECORD = struct.Struct("<IQ32s")
MAX_PAYLOAD = 512 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_INDEX = 16 * 1024 * 1024
MAX_FILES = 20000
MAX_PATH = 4096
MAX_EXE = MAX_PAYLOAD + MAX_INDEX + 64 * 1024 * 1024
ENTRYPOINT = "mclauncher-qt.exe"
QT_VERSION = "6.8.3"
INDEX_RESOURCE = 21001
PAYLOAD_RESOURCE = 21002
PATH_PATTERN = re.compile(r"[A-Za-z0-9._+@/ -]+\Z")
TAG_PATTERN = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?\Z")
SHA_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
SYSTEM_DLLS = frozenset({
    "advapi32.dll", "bcrypt.dll", "comctl32.dll", "comdlg32.dll",
    "crypt32.dll", "gdi32.dll", "imm32.dll", "iphlpapi.dll", "kernel32.dll",
    "msvcrt.dll", "normaliz.dll", "ntdll.dll", "ole32.dll", "oleaut32.dll",
    "secur32.dll", "setupapi.dll", "shell32.dll", "shlwapi.dll", "ucrtbase.dll",
    "user32.dll", "userenv.dll", "uxtheme.dll", "version.dll", "winmm.dll",
    "winspool.drv", "ws2_32.dll",
})
REQUIRED_FILES = frozenset({
    ENTRYPOINT, "Qt6Core.dll", "Qt6Gui.dll", "Qt6Network.dll", "Qt6Qml.dll",
    "Qt6Quick.dll", "platforms/qwindows.dll", "imageformats/qwebp.dll",
    "SUPPORTING_MATERIALS.zip", "runtime-manifest.json", "SHA256SUMS.txt",
})


class BundleError(ValueError):
    """An input is not a complete, safe v1 Windows runtime bundle."""


def _require(condition, message):
    if not condition:
        raise BundleError(message)


def _tag(tag):
    _require(isinstance(tag, str) and len(tag) <= 128 and TAG_PATTERN.fullmatch(tag),
             "Invalid bundle release tag")
    return tag.encode("ascii")


def _path(name, directory=False):
    _require(isinstance(name, str) and 0 < len(name) <= MAX_PATH,
             "Invalid runtime path length")
    _require(PATH_PATTERN.fullmatch(name), "Runtime paths must use the v1 ASCII path alphabet")
    value = name[:-1] if directory and name.endswith("/") else name
    _require(value and not value.startswith("/") and not value.endswith("/"), "Unsafe runtime path")
    for part in value.split("/"):
        _require(part and part not in (".", "..") and part[-1] not in " .", "Unsafe runtime path component")
        base = part.split(".", 1)[0].strip(" .").upper()
        _require(base not in {"CON", "PRN", "AUX", "NUL"}
                 and not re.fullmatch(r"(?:COM|LPT)[1-9]", base), "Reserved Windows runtime path")
    return value


class _Paths:
    def __init__(self):
        self.entries = set()
        self.files = set()
        self.dirs = set()
        self.spellings = {}

    def add(self, name, directory=False):
        value = _path(name, directory)
        key = value.lower()
        _require(key not in self.entries, "Duplicate or case-colliding runtime path")
        parts = value.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            folded = prefix.lower()
            _require(self.spellings.get(folded, prefix) == prefix, "Case-colliding runtime directory")
            self.spellings[folded] = prefix
            if index < len(parts) or directory:
                _require(folded not in self.files, "Runtime file/directory prefix conflict")
                self.dirs.add(folded)
        if not directory:
            _require(key not in self.dirs, "Runtime file/directory prefix conflict")
            self.files.add(key)
        self.entries.add(key)
        return value


def _read(path, limit):
    path = Path(path)
    _require(not path.is_symlink() and path.is_file() and 0 < path.stat().st_size <= limit,
             "Expected a bounded ordinary input file")
    data = path.read_bytes()
    _require(0 < len(data) <= limit, "Input file changed or exceeded its limit")
    return data


def _data(value, limit):
    if isinstance(value, (bytes, bytearray, memoryview)):
        _require(0 < len(value) <= limit, "Input exceeds its size bound")
        return bytes(value)
    return _read(value, limit)


def parse_index(data, tag):
    """Read the complete binary index, checking bounds and path inventories."""
    _tag(tag)
    _require(HEADER.size <= len(data) <= MAX_INDEX, "Invalid bundle index size")
    magic, schema, count, size, tag_size, entry_size, digest = HEADER.unpack_from(data)
    _require(magic == MAGIC and schema == 1, "Unsupported bundle index")
    _require(0 < count <= MAX_FILES and 0 < size <= MAX_PAYLOAD,
             "Bundle index exceeds its inventory bounds")
    _require(0 < tag_size <= 128 and 0 < entry_size <= MAX_PATH,
             "Invalid bundle identity lengths")
    offset = HEADER.size

    def take(length):
        nonlocal offset
        _require(length <= len(data) - offset, "Truncated bundle index")
        value = data[offset:offset + length]
        offset += length
        return value

    try:
        indexed_tag = take(tag_size).decode("utf-8", "strict")
        entrypoint = take(entry_size).decode("utf-8", "strict")
    except UnicodeError as error:
        raise BundleError("Invalid UTF-8 bundle identity") from error
    _require(indexed_tag == tag and entrypoint == ENTRYPOINT, "Bundle index identity differs from release")
    paths = _Paths()
    records = []
    total = 0
    previous = ""
    for unused in range(count):
        _require(RECORD.size <= len(data) - offset, "Truncated bundle index record")
        length, file_size, file_digest = RECORD.unpack(take(RECORD.size))
        _require(0 < length <= MAX_PATH and file_size <= MAX_EXPANDED,
                 "Bundle file exceeds its bounds")
        try:
            name = take(length).decode("utf-8", "strict")
        except UnicodeError as error:
            raise BundleError("Invalid UTF-8 bundle path") from error
        paths.add(name)
        _require(name > previous, "Bundle index paths are not strictly ordinal sorted")
        previous = name
        total += file_size
        _require(total <= MAX_EXPANDED, "Bundle expanded size exceeds its bound")
        records.append({"path": name, "size": file_size, "sha256": file_digest.hex()})
    _require(offset == len(data), "Trailing bytes after bundle index")
    _require(ENTRYPOINT in {record["path"] for record in records}, "Bundle entrypoint is missing")
    return {"schema": schema, "tag": tag, "entrypoint": entrypoint,
            "file_count": count, "payload_size": size, "payload_sha256": digest.hex(),
            "expanded_size": total, "files": records}


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON metadata key")
        result[key] = value
    return result


def _json_constant(unused):
    raise BundleError("Non-finite JSON runtime metadata value")


def _zip_layout(data, archive, entries):
    """Check local headers as well as the central directory ZipFile exposes.

    ZipFile intentionally accepts prepended data and repairs incorrect central
    offsets for self-extracting ZIPs.  Our embedded runtime is an ordinary ZIP,
    with no multiple disks or ZIP64 need at the v1 byte/file-count limits.
    """
    _require(len(data) >= 22 and data[-22:-18] == b"PK\x05\x06", "Invalid runtime ZIP end record")
    signature, disk, central_disk, disk_count, count, central_size, central, comment = struct.unpack_from(
        "<4s4H2IH", data, len(data) - 22)
    _require(disk == central_disk == comment == 0 and disk_count == count == len(entries)
             and central + central_size == len(data) - 22 and archive.start_dir == central,
             "Invalid or multi-disk runtime ZIP directory bounds")
    spans = []
    for info in entries:
        offset = info.header_offset
        _require(0 <= offset <= central - 30, "Runtime ZIP local header exceeds bounds")
        local = struct.unpack_from("<4s5H3I2H", data, offset)
        signature, version, flags, method, modified, date, crc, compressed, expanded, namesize, extrasize = local
        _require(signature == b"PK\x03\x04" and flags == info.flag_bits and method == info.compress_type,
                 "Runtime ZIP local header differs from directory")
        name_start = offset + 30
        start = name_start + namesize + extrasize
        end = start + info.compress_size
        _require(namesize <= MAX_PATH and start <= central and end <= central,
                 "Runtime ZIP compressed data exceeds bounds")
        _require(data[name_start:name_start + namesize] == info.orig_filename.encode("ascii", "strict"),
                 "Runtime ZIP local filename differs from directory")
        if flags & 8:
            _require(crc in (0, info.CRC) and compressed in (0, info.compress_size)
                     and expanded in (0, info.file_size), "Invalid ZIP data descriptor header")
            descriptor = end + (4 if data[end:end + 4] == b"PK\x07\x08" else 0)
            _require(descriptor + 12 <= central
                     and struct.unpack_from("<III", data, descriptor) == (info.CRC, info.compress_size, info.file_size),
                     "ZIP data descriptor differs from directory")
            end = descriptor + 12
        else:
            _require((crc, compressed, expanded) == (info.CRC, info.compress_size, info.file_size),
                     "Runtime ZIP local size or checksum differs from directory")
        spans.append((offset, end))
    previous = 0
    for start, end in sorted(spans):
        _require(start == previous and end > start, "Runtime ZIP contains gaps or overlapping local entries")
        previous = end
    _require(previous == central, "Runtime ZIP has unlisted local data")


def _zip_inventory(data, capture=(), capture_limits=None):
    """Hash all members and retain only explicitly requested bounded metadata."""
    paths = _Paths()
    records = {}
    captured = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            _require(not archive.comment, "ZIP comments are not runtime files")
            entries = archive.infolist()
            _require(0 < len(entries) <= MAX_FILES * 2, "Invalid runtime ZIP entry count")
            # Validate names before the layout reader encodes their exact local
            # bytes. This also converts non-ASCII or NUL names to BundleError.
            for info in entries:
                _require(info.filename == info.orig_filename, "NUL in runtime ZIP name")
                _path(info.filename, info.is_dir())
            _zip_layout(data, archive, entries)
            for info in entries:
                directory = info.is_dir()
                _require(info.filename == info.orig_filename, "NUL in runtime ZIP name")
                name = paths.add(info.filename, directory)
                _require(not info.flag_bits & ~(0x800 | 8 | 6)
                         and info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                         "Encrypted or unsupported runtime ZIP member")
                mode = (info.external_attr >> 16) & 0xFFFF
                kind = stat.S_IFMT(mode)
                _require(kind in (0, stat.S_IFDIR if directory else stat.S_IFREG),
                         "Runtime ZIP contains a link or special file")
                _require(not info.external_attr & 0x400, "Runtime ZIP contains a reparse point")
                _require(directory or not info.external_attr & 0x10, "Runtime ZIP file has a directory attribute")
                _require(info.file_size >= 0 and info.compress_size >= 0
                         and info.file_size <= MAX_EXPANDED and info.compress_size <= len(data),
                         "Runtime ZIP member exceeds bounds")
                if directory:
                    _require(info.file_size == 0, "Runtime ZIP directory has file data")
                    with archive.open(info) as stream:
                        _require(stream.read(1) == b"", "Runtime ZIP directory has file data")
                    continue
                _require(len(records) < MAX_FILES, "Runtime ZIP has too many files")
                total += info.file_size
                _require(total <= MAX_EXPANDED, "Runtime ZIP expanded size exceeds its bound")
                keep = name in capture
                limit = (capture_limits or {}).get(name, 128 * 1024 * 1024 if name == ENTRYPOINT else MAX_INDEX)
                _require(not keep or info.file_size <= limit,
                         "Runtime metadata exceeds its bound")
                digest = hashlib.sha256()
                blocks = [] if keep else None
                actual_size = 0
                with archive.open(info) as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        actual_size += len(block)
                        _require(actual_size <= info.file_size, "Runtime ZIP member size is inconsistent")
                        digest.update(block)
                        if keep:
                            blocks.append(block)
                _require(actual_size == info.file_size, "Truncated runtime ZIP member")
                records[name] = {"path": name, "size": actual_size, "sha256": digest.hexdigest()}
                if keep:
                    captured[name] = b"".join(blocks)
    except (OSError, EOFError, RuntimeError, NotImplementedError, zipfile.BadZipFile, UnicodeError, zlib.error) as error:
        raise BundleError("Invalid runtime ZIP") from error
    return records, captured


def _runtime(data, tag):
    _require(0 < len(data) <= MAX_PAYLOAD, "Runtime payload exceeds its bound")
    records, captured = _zip_inventory(data, REQUIRED_FILES & {
        ENTRYPOINT, "SUPPORTING_MATERIALS.zip", "runtime-manifest.json", "SHA256SUMS.txt"})
    _require(REQUIRED_FILES <= records.keys()
             and all(records[name]["size"] > 0 for name in REQUIRED_FILES), "Runtime payload is incomplete")
    try:
        manifest = json.loads(captured["runtime-manifest.json"].decode("utf-8", "strict"),
                              object_pairs_hook=_json_object, parse_constant=_json_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise BundleError("Invalid runtime manifest JSON") from error
    _require(isinstance(manifest, dict) and type(manifest.get("schema")) is int
             and manifest["schema"] == 1 and manifest.get("version") == tag
             and manifest.get("platform") == "windows-x64" and manifest.get("linkage") == "dynamic"
             and manifest.get("qt") == QT_VERSION,
             "Runtime manifest identity differs from this release")
    listed = manifest.get("files")
    _require(isinstance(listed, list) and len(listed) <= MAX_FILES, "Invalid runtime manifest inventory")
    inventory = {}
    paths = _Paths()
    for record in listed:
        _require(isinstance(record, dict) and set(record) == {"path", "size", "sha256"},
                 "Invalid runtime manifest file record")
        name = paths.add(record["path"])
        _require(type(record["size"]) is int and record["size"] >= 0
                 and isinstance(record["sha256"], str) and SHA_PATTERN.fullmatch(record["sha256"]),
                 "Invalid runtime manifest hash or size")
        inventory[name] = record
    expected = {name: record for name, record in records.items()
                if name not in {"runtime-manifest.json", "SHA256SUMS.txt"}}
    _require(inventory == expected, "Runtime manifest inventory differs from ZIP bytes")
    sums = {}
    try:
        lines = captured["SHA256SUMS.txt"].decode("utf-8", "strict").splitlines()
    except UnicodeError as error:
        raise BundleError("Invalid runtime checksum encoding") from error
    for line in lines:
        _require(len(line) > 66 and line[64:66] == "  " and SHA_PATTERN.fullmatch(line[:64]),
                 "Invalid runtime checksum line")
        name = line[66:]
        _path(name)
        _require(name not in sums, "Duplicate runtime checksum path")
        sums[name] = line[:64]
    _require(sums == {name: record["sha256"] for name, record in records.items() if name != "SHA256SUMS.txt"},
             "Runtime checksums differ from ZIP bytes")
    # The existing dynamic Qt target is a console-subsystem PE; the GUI-only
    # requirement applies to the native outer bootstrap, which hides its loader.
    _PE(captured[ENTRYPOINT], require_gui=False)
    legal, unused = _zip_inventory(captured["SUPPORTING_MATERIALS.zip"])
    _require(bool(legal), "Supporting materials are empty")
    return records, manifest


def create_index(payload, tag):
    """Validate a deployed runtime ZIP and return its canonical binary index."""
    tag_bytes = _tag(tag)
    data = _data(payload, MAX_PAYLOAD)
    records, unused = _runtime(data, tag)
    entry = ENTRYPOINT.encode("ascii")
    result = bytearray(HEADER.pack(MAGIC, 1, len(records), len(data), len(tag_bytes), len(entry), hashlib.sha256(data).digest()))
    result.extend(tag_bytes)
    result.extend(entry)
    for name, record in sorted(records.items()):
        encoded = name.encode("ascii")
        result.extend(RECORD.pack(len(encoded), record["size"], bytes.fromhex(record["sha256"])))
        result.extend(encoded)
    _require(len(result) <= MAX_INDEX, "Bundle index exceeds its bound")
    return bytes(result)


class _PE:
    """Bounded PE32+ reader; all mapped input comes from ordinary file bytes."""
    def __init__(self, data, require_gui=True):
        self.data = data
        _require(len(data) >= 64 and data[:2] == b"MZ", "Missing Windows executable header")
        pe = self.u32(60)
        _require(pe >= 64 and pe <= len(data) - 24 and data[pe:pe + 4] == b"PE\x00\x00", "Invalid PE signature")
        machine, count = self.unpack("<HH", pe + 4)
        characteristics = self.u16(pe + 22)
        opt_size = self.u16(pe + 20)
        opt = pe + 24
        _require(machine == 0x8664 and 0 < count <= 96 and opt_size >= 112
                 and opt_size <= len(data) - opt and self.u16(opt) == 0x20B,
                 "Expected an AMD64 PE32+ executable")
        _require(characteristics & 2 and not characteristics & 0x2000,
                 "Expected an executable PE image rather than a DLL")
        _require(self.u16(opt + 68) in ((2,) if require_gui else (2, 3)),
                 "Expected a Windows GUI executable" if require_gui else "Unsupported Windows application subsystem")
        directories = self.u32(opt + 108)
        _require(directories <= 16 and 112 + directories * 8 <= opt_size, "Invalid PE data directories")
        self.directories = [self.unpack("<II", opt + 112 + index * 8) for index in range(directories)]
        self.sections = []
        table = opt + opt_size
        self.bounds(table, count * 40)
        for index in range(count):
            start = table + index * 40
            virtual_size, rva, raw_size, raw = self.unpack("<IIII", start + 8)
            flags = self.u32(start + 36)
            _require(rva and max(virtual_size, raw_size) <= 0x100000000 - rva, "Invalid PE section virtual bounds")
            if raw_size:
                self.bounds(raw, raw_size)
                _require(raw >= table + count * 40, "PE section overlaps its headers")
            section = (rva, max(virtual_size, raw_size), raw, raw_size, flags)
            for old in self.sections:
                _require(not self.overlaps(rva, section[1], old[0], old[1]), "Overlapping PE virtual sections")
                if raw_size and old[3]:
                    _require(not self.overlaps(raw, raw_size, old[2], old[3]), "Overlapping PE raw sections")
            self.sections.append(section)
        entry = self.u32(opt + 16)
        self.rva(entry, 1)
        _require(any(rva <= entry < rva + raw_size and flags & 0x20000000
                     for rva, unused, raw, raw_size, flags in self.sections), "PE entrypoint is not executable raw code")

    @staticmethod
    def overlaps(a, size, b, other):
        return a < b + other and b < a + size

    def bounds(self, offset, size):
        _require(0 <= offset <= len(self.data) and 0 <= size <= len(self.data) - offset, "PE field exceeds file bounds")

    def unpack(self, fmt, offset):
        self.bounds(offset, struct.calcsize(fmt))
        return struct.unpack_from(fmt, self.data, offset)

    def u16(self, offset):
        return self.unpack("<H", offset)[0]

    def u32(self, offset):
        return self.unpack("<I", offset)[0]

    def rva(self, address, size):
        _require(0 < address <= 0xFFFFFFFF and size <= 0x100000000 - address, "Invalid PE RVA")
        for rva, unused, raw, raw_size, flags in self.sections:
            if rva <= address and size <= raw_size - (address - rva):
                offset = raw + address - rva
                self.bounds(offset, size)
                return offset
        raise BundleError("PE RVA does not map to file bytes")

    def directory(self, index):
        address, size = self.directories[index] if index < len(self.directories) else (0, 0)
        _require(bool(address) == bool(size), "Incomplete PE data directory")
        return address, size

    def cstring(self, address, limit=512):
        offset = self.rva(address, 1)
        remaining = next(raw_size - (address - rva) for rva, unused, raw, raw_size, flags in self.sections
                         if rva <= address < rva + raw_size)
        end = self.data.find(b"\x00", offset, offset + min(remaining, limit))
        _require(end > offset, "Invalid PE import string")
        try:
            value = self.data[offset:end].decode("ascii", "strict")
        except UnicodeError as error:
            raise BundleError("Non-ASCII PE import string") from error
        _require(all(32 < ord(char) < 127 for char in value), "Invalid PE import string")
        return value

    def imports(self):
        _require(self.directory(13) == (0, 0), "Bootstrap may not delay-load external libraries")
        address, size = self.directory(1)
        _require(address and 20 <= size <= 1024 * 1024, "Bootstrap import directory is missing or oversized")
        self.rva(address, size)
        result = []
        thunk_count = 0
        for index in range(min(size // 20, 4096)):
            descriptor = self.unpack("<IIIII", self.rva(address + index * 20, 20))
            if not any(descriptor):
                _require(bool(result), "Bootstrap has no Windows system imports")
                return sorted(result)
            original, timestamp, forwarder, name_rva, first = descriptor
            name = self.cstring(name_rva).lower()
            _require(name in SYSTEM_DLLS or re.fullmatch(r"(?:api|ext)-ms-win-[a-z0-9-]+\.dll", name),
                     "Bootstrap depends on a non-system DLL")
            _require(name not in result, "Duplicate bootstrap import descriptor")
            result.append(name)
            _require(first != 0, "Missing PE import address table")
            # Names/ordinal entries must also be file-backed and terminated.
            for table in {first, original or first}:
                for thunk in range(65536):
                    thunk_count += 1
                    _require(thunk_count <= 65536, "Bootstrap import inventory exceeds its bound")
                    value = self.unpack("<Q", self.rva(table + thunk * 8, 8))[0]
                    if value == 0:
                        break
                    if value & (1 << 63):
                        _require(value & 0x7FFFFFFFFFFF0000 == 0, "Invalid PE ordinal import")
                    else:
                        _require(value <= 0xFFFFFFFF, "Invalid PE import name RVA")
                        self.rva(value, 2)
                        self.cstring(value + 2)
                else:
                    raise BundleError("Unterminated PE import table")
        raise BundleError("Unterminated PE import directory")

    def resources(self):
        root, length = self.directory(2)
        _require(root and 16 <= length <= len(self.data), "Missing PE resources")
        base = self.rva(root, length)
        visited = set()
        metadata = []
        selected = {}
        count = 0

        def bound(offset, size):
            _require(0 <= offset <= length and 0 <= size <= length - offset, "PE resource exceeds directory bounds")
            metadata.append((offset, size))
            return base + offset

        def walk(offset, route):
            nonlocal count
            _require(offset not in visited and len(route) <= 2, "Cyclic or over-nested PE resource directory")
            visited.add(offset)
            directory = bound(offset, 16)
            named, ids = self.unpack("<HH", directory + 12)
            number = named + ids
            count += number
            _require(count <= 8192, "Too many PE resource entries")
            entries = bound(offset + 16, number * 8)
            seen = set()
            for index in range(number):
                key, target = self.unpack("<II", entries + index * 8)
                if key & 0x80000000:
                    text = key & 0x7FFFFFFF
                    chars = self.u16(bound(text, 2))
                    text_offset = bound(text + 2, chars * 2)
                    try:
                        name = self.data[text_offset:text_offset + chars * 2].decode("utf-16-le", "strict")
                    except UnicodeError as error:
                        raise BundleError("Invalid PE resource name") from error
                    _require(index < named and bool(name), "Invalid named PE resource ordering")
                    identity = ("name", name)
                else:
                    _require(index >= named and key <= 0xFFFF, "Invalid numeric PE resource identifier")
                    identity = ("id", key)
                _require(identity not in seen, "Duplicate PE resource identifier")
                seen.add(identity)
                path = route + (identity,)
                if target & 0x80000000:
                    _require(len(path) < 3, "Invalid PE resource directory depth")
                    walk(target & 0x7FFFFFFF, path)
                else:
                    _require(len(path) == 3, "Invalid PE resource leaf depth")
                    leaf = bound(target, 16)
                    data_rva, size, codepage, reserved = self.unpack("<IIII", leaf)
                    _require(reserved == 0 and size > 0 and root <= data_rva
                             and size <= root + length - data_rva, "Invalid PE resource data bounds")
                    start = self.rva(data_rva, size)
                    if path[0] == ("id", 10):
                        _require(path[1][0] == "id" and path[1][1] in (INDEX_RESOURCE, PAYLOAD_RESOURCE),
                                 "Unexpected bootstrap RCDATA resource")
                        _require(codepage == 0 and size <= (MAX_INDEX if path[1][1] == INDEX_RESOURCE else MAX_PAYLOAD),
                                 "Bundle resource exceeds its bound or has a non-neutral codepage")
                        _require(path[2] == ("id", 0) and path[1][1] not in selected,
                                 "Bundle resource must have one neutral language")
                        selected[path[1][1]] = (data_rva - root, size, start)

        walk(0, ())
        _require(set(selected) == {INDEX_RESOURCE, PAYLOAD_RESOURCE}, "Missing bundle RCDATA resources")
        for offset, size, unused in selected.values():
            _require(all(not self.overlaps(offset, size, start, count) for start, count in metadata),
                     "Bundle resource overlaps resource metadata")
        first, second = selected.values()
        _require(not self.overlaps(first[0], first[1], second[0], second[1]), "Bundle resources overlap")
        return {key: self.data[start:start + size] for key, (unused, size, start) in selected.items()}


def inspect_bundle(exe, tag):
    """Validate the complete outer PE, index, runtime ZIP and metadata."""
    _tag(tag)
    pe = _PE(_data(exe, MAX_EXE))
    imports = pe.imports()
    resources = pe.resources()
    index = parse_index(resources[INDEX_RESOURCE], tag)
    payload = resources[PAYLOAD_RESOURCE]
    _require(len(payload) == index["payload_size"]
             and hashlib.sha256(payload).hexdigest() == index["payload_sha256"], "Embedded runtime payload hash differs from index")
    records, manifest = _runtime(payload, tag)
    _require(index["files"] == [record for name, record in sorted(records.items())],
             "Bundle index inventory differs from embedded runtime")
    return {key: value for key, value in index.items() if key != "files"} | {
        "stub_imports": imports, "manifest": manifest,
    }


def inspect_windows_zip(archive_path, tag):
    """Require the published Windows ZIP to contain only the standalone EXE."""
    data = _data(archive_path, MAX_EXE + 16 * 1024 * 1024)
    records, captured = _zip_inventory(data, {ENTRYPOINT}, {ENTRYPOINT: MAX_EXE})
    _require(set(records) == {ENTRYPOINT}, "Windows download must contain only mclauncher-qt.exe")
    # No directory entries may be hidden around the otherwise single file.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        _require(len(archive.infolist()) == 1, "Windows download must have exactly one ZIP entry")
    return inspect_bundle(captured[ENTRYPOINT], tag)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-index")
    create.add_argument("--payload", type=Path, required=True)
    create.add_argument("--tag", required=True)
    create.add_argument("--output", type=Path, required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--exe", type=Path, required=True)
    inspect.add_argument("--tag", required=True)
    archive = commands.add_parser("inspect-zip")
    archive.add_argument("--archive", type=Path, required=True)
    archive.add_argument("--tag", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create-index":
            value = create_index(args.payload, args.tag)
            with args.output.open("xb") as stream:
                stream.write(value)
            result = parse_index(value, args.tag)
            result.pop("files")
        elif args.command == "inspect":
            result = inspect_bundle(args.exe, args.tag)
        else:
            result = inspect_windows_zip(args.archive, args.tag)
    except (BundleError, OSError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Windows bundle validation failed: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
