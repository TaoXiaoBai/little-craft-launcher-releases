"""Inspect a type-2 AppImage without executing its embedded runtime or files."""

import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import selectors
import shutil
import signal
import struct
import subprocess
import time

MAX_RUNTIME_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 20000
MAX_LIST_BYTES = 8 * 1024 * 1024


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _read_at(stream, offset, size, file_size):
    _require(0 <= offset <= file_size and 0 <= size <= file_size - offset,
             "An AppImage ELF record extends outside its file")
    stream.seek(offset)
    result = stream.read(size)
    _require(len(result) == size, "An AppImage changed during inspection")
    return result


def _elf_header(data, appimage=False):
    _require(len(data) >= 64 and data[:7] == b"\x7fELF\x02\x01\x01",
             "Expected a little-endian ELF64 executable")
    if appimage:
        _require(data[8:11] == b"AI\x02", "Expected a type-2 AppImage")
    header = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    _require(header[0] in (2, 3) and header[1] == 62 and header[2] == 1 and header[7] == 64,
             "Expected an x86_64 ELF executable")
    return header


def _filesystem_offset(path):
    file_size = path.stat().st_size
    _require(160 <= file_size <= MAX_IMAGE_BYTES, "AppImage size is outside the inspection bound")
    with path.open("rb") as stream:
        header = _elf_header(_read_at(stream, 0, 64, file_size), appimage=True)
        phoff, shoff = header[4:6]
        phsize, phnum, shsize, shnum = header[8:12]
        _require(0 < phnum <= 1024 and phsize == 56, "Invalid AppImage ELF program table")
        _require((shnum == 0 and shoff == 0) or (0 < shnum <= 4096 and shsize == 64),
                 "Unsupported AppImage ELF section table")
        end = 64

        def include(offset, size):
            nonlocal end
            _require(0 <= offset <= MAX_RUNTIME_BYTES and 0 <= size <= MAX_RUNTIME_BYTES - offset
                     and offset + size <= file_size, "AppImage runtime ELF exceeds its inspection bound")
            end = max(end, offset + size)

        include(phoff, phsize * phnum)
        for index in range(phnum):
            segment = struct.unpack("<IIQQQQQQ", _read_at(stream, phoff + index * phsize, phsize, file_size))
            include(segment[2], segment[5])
        if shnum:
            include(shoff, shsize * shnum)
            for index in range(shnum):
                section = struct.unpack("<IIQQQQIIQQ", _read_at(stream, shoff + index * shsize, shsize, file_size))
                if section[1] != 8:  # SHT_NOBITS has no bytes in the ELF file.
                    include(section[4], section[5])
        # Type-2 tools append SquashFS after the runtime ELF, sometimes with
        # page alignment. Never search arbitrary payload bytes for a magic string.
        candidates = sorted({end, (end + 4095) // 4096 * 4096})
        matches = []
        for offset in candidates:
            if offset + 96 > file_size:
                continue
            block = _read_at(stream, offset, 96, file_size)
            if block[:4] != b"hsqs":
                continue
            inodes, block_size = struct.unpack_from("<I", block, 4)[0], struct.unpack_from("<I", block, 12)[0]
            major, minor = struct.unpack_from("<HH", block, 28)
            used = struct.unpack_from("<Q", block, 40)[0]
            if (major == 4 and minor == 0 and 0 < inodes <= MAX_ENTRIES
                    and 4096 <= block_size <= 1024 * 1024 and not block_size & (block_size - 1)
                    and 96 <= used <= file_size - offset):
                matches.append(offset)
        _require(len(matches) == 1, "No unique SquashFS filesystem follows the AppImage runtime ELF")
        return matches[0]


def _bounded_run(command, maximum, deadline):
    """Bound decompressor time and both pipe sizes, including unsuccessful runs."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    output = {"out": bytearray(), "err": bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "out")
            selector.register(process.stderr, selectors.EVENT_READ, "err")
            while selector.get_map():
                remaining = min(10.0, deadline - time.monotonic())
                _require(remaining > 0, "AppImage inspection timed out")
                events = selector.select(remaining)
                _require(bool(events), "AppImage decompression timed out")
                for key, _ in events:
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    limit = maximum if key.data == "out" else 65536
                    _require(len(output[key.data]) + len(block) <= limit,
                             "AppImage decompressor output exceeds its bound")
                    output[key.data].extend(block)
        code = process.wait(timeout=max(0.01, min(10, deadline - time.monotonic())))
        _require(code == 0, "Could not read a required AppImage entry")
        return bytes(output["out"])
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def _unsquashfs(path, offset, options, maximum, deadline):
    tool = shutil.which("unsquashfs")
    _require(tool is not None, "unsquashfs is required for static AppImage inspection")
    return _bounded_run([tool, "-processors", "1", "-offset", str(offset),
                         *options, str(path)], maximum, deadline)


def _inventory(listing):
    entries = {}
    for line in listing.decode("utf-8", errors="strict").splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or len(fields[0]) != 10 or fields[0][0] not in "-dlbcps":
            continue  # unsquashfs's fixed explanatory header, if present.
        name = fields[5].split(" -> ", 1)[0] if fields[0][0] == "l" else fields[5]
        if name == "squashfs-root":
            continue
        _require(name.startswith("squashfs-root/"), "Unexpected AppImage inventory root")
        relative = name.removeprefix("squashfs-root/")
        value = PurePosixPath(relative)
        _require(relative and len(relative) <= 4096 and not value.is_absolute() and ".." not in value.parts
                 and "\\" not in relative and "\x00" not in relative and relative not in entries,
                 "Unsafe or duplicate AppImage entry")
        _require(fields[0][0] in "-dl", "Special files must not enter an AppImage")
        if fields[0][0] == "l":
            parts = fields[5].split(" -> ", 1)
            _require(len(parts) == 2 and parts[1] and not PurePosixPath(parts[1]).is_absolute(),
                     "An AppImage link must have a relative target")
            target = posixpath.normpath(posixpath.join(str(value.parent), parts[1]))
            _require(target != ".." and not target.startswith("../") and "\\" not in parts[1],
                     "An AppImage link escapes its runtime")
        forbidden = {".git", "CMakeFiles", "ApplicationSnapshot", "accounts", "private"}
        _require(not any(part in forbidden for part in value.parts)
                 and value.parts[0] not in {"source", "src", "qt-native", "build", "build-ci"},
                 "Source, build or account data entered the AppImage")
        _require(value.suffix.lower() not in {".a", ".o", ".obj", ".pdb", ".log", ".pem", ".key"},
                 "A build, credential or diagnostic file entered the AppImage")
        _require("licenses" in value.parts or value.suffix.lower() not in
                 {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp"},
                 "Application development sources entered the AppImage")
        entries[relative] = fields[0]
        _require(len(entries) <= MAX_ENTRIES, "AppImage contains too many entries")
    _require(bool(entries), "AppImage inventory is empty")
    return entries


def inspect_appimage(path, tag):
    """Return the verified manifest; invoke only the system SquashFS reader."""
    # Local candidates may use a suffix that is deliberately not publishable.
    # Publication policy belongs to the publisher; inspection only compares an
    # unambiguous, bounded label against the embedded runtime identity.
    _require(isinstance(tag, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", tag) is not None,
             "An AppImage inspection tag must be a safe local label")
    path = Path(path).absolute()
    _require(not path.is_symlink() and path.is_file(), "AppImage must be an ordinary file")
    offset = _filesystem_offset(path)
    deadline = time.monotonic() + 60
    entries = _inventory(_unsquashfs(path, offset, ["-lln"], MAX_LIST_BYTES, deadline))
    required = {
        "runtime-manifest.json": 65536,
        "AppRun": 65536,
        "SUPPORTING_MATERIALS.zip": 32 * 1024 * 1024,
        "usr/bin/mclauncher-qt": 128 * 1024 * 1024,
        "usr/plugins/platforms/libqxcb.so": 16 * 1024 * 1024,
        "usr/plugins/platforms/libqwayland-generic.so": 16 * 1024 * 1024,
        "usr/plugins/platforms/libqwayland-egl.so": 16 * 1024 * 1024,
    }
    contents = {}
    for name, maximum in required.items():
        _require(name in entries and entries[name].startswith("-"), "A required AppImage entry is missing or linked")
        if name in {"AppRun", "usr/bin/mclauncher-qt"}:
            _require("x" in entries[name], "An AppImage entry is not executable")
        # -cat takes its file selectors after the image pathname.
        tool = shutil.which("unsquashfs")
        _require(tool is not None, "unsquashfs is required for static AppImage inspection")
        data = _bounded_run([tool, "-processors", "1", "-offset", str(offset),
                             "-cat", str(path), name], maximum, deadline)
        _require(bool(data), "A required AppImage entry is empty")
        if name.endswith(".so"):
            _require(data[:7] == b"\x7fELF\x02\x01\x01" and len(data) >= 20
                     and struct.unpack_from("<H", data, 18)[0] == 62, "A platform plugin is not x86_64 ELF")
        elif name == "usr/bin/mclauncher-qt":
            _elf_header(data)
        elif name == "AppRun":
            _require(data.startswith((b"#!", b"\x7fELF")), "AppRun is not a script or executable")
        elif name == "SUPPORTING_MATERIALS.zip":
            _require(data.startswith(b"PK\x03\x04"), "AppImage supporting materials are not a ZIP")
        else:
            contents[name] = data
    manifest = json.loads(contents["runtime-manifest.json"].decode("utf-8"))
    expected = {"schema": 2, "version": tag, "platform": "linux-x86_64", "format": "appimage",
                "qt": "6.8.3", "linkage": "dynamic", "displayBackends": ["xcb", "wayland"],
                "application": "usr/bin/mclauncher-qt"}
    _require(isinstance(manifest, dict) and all(manifest.get(key) == value for key, value in expected.items()),
             "AppImage runtime identity or display backends differ from this release")
    return manifest
