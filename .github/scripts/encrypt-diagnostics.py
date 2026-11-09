#!/usr/bin/env python3
"""Encrypt private CI diagnostics without writing them to public step output.

Only the selected log directory is archived. The certificate is public; its
matching private key must stay outside the public repository and runner.
"""

import argparse
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile


MAX_FILES = 1000
MAX_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 10000
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class DiagnosticLimitError(ValueError):
    pass


class OpenSSLUnavailableError(RuntimeError):
    pass


def ordinary(path, *, directory=False):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE_POINT:
        raise ValueError("Links are not diagnostic inputs")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise ValueError("Unexpected diagnostic input type")
    return info


def files_under(root, *, exclude_screenshots=False):
    ordinary(root, directory=True)
    result = []
    stack = [root]
    total = 0
    entries = 0
    while stack:
        parent = stack.pop()
        ordinary(parent, directory=True)
        for item in sorted(parent.iterdir(), key=lambda value: value.name):
            entries += 1
            if entries > MAX_ENTRIES:
                raise DiagnosticLimitError("Too many diagnostic directory entries")
            if "\\" in item.name or ":" in item.name or any(ord(char) < 32 for char in item.name):
                raise ValueError("Unsupported diagnostic file name")
            info = item.lstat()
            if stat.S_ISDIR(info.st_mode):
                ordinary(item, directory=True)
                stack.append(item)
                continue
            info = ordinary(item)
            # Validate even excluded entries first: screenshot filtering must
            # never make links, devices or malformed paths acceptable inputs.
            if exclude_screenshots and item.suffix.lower() == ".png":
                continue
            result.append((item, info))
            total += info.st_size
            if len(result) > MAX_FILES or total > MAX_BYTES:
                raise DiagnosticLimitError("Diagnostic archive limit exceeded")
    return sorted(result, key=lambda value: value[0].relative_to(root).as_posix())


def write_archive(destination, root, entries):
    total = 0
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, before in entries:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source:
                opened = os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("Diagnostic input changed")
                name = path.relative_to(root).as_posix()
                with archive.open(name, "w") as target:
                    while block := source.read(1024 * 1024):
                        total += len(block)
                        if total > MAX_BYTES:
                            raise DiagnosticLimitError("Diagnostic archive limit exceeded")
                        target.write(block)
                after = os.fstat(source.fileno())
                if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
                    raise ValueError("Diagnostic input changed")


def openssl_path():
    executable = shutil.which("openssl")
    if executable:
        return executable
    fallback = Path("C:/Program Files/Git/usr/bin/openssl.exe")
    if os.name == "nt" and fallback.is_file():
        return str(fallback)
    raise OpenSSLUnavailableError("OpenSSL is unavailable")


def encrypt(certificate, root, output, *, exclude_screenshots=False):
    ordinary(certificate)
    entries = files_under(root, exclude_screenshots=exclude_screenshots)
    output.parent.mkdir(parents=True, exist_ok=True)
    ordinary(output.parent, directory=True)
    if output.exists() or output.is_symlink():
        raise ValueError("Refusing an existing encrypted archive")
    # Plaintext is deliberately outside the artifact output directory. The
    # temporary tree is removed on every ordinary success/failure path.
    with tempfile.TemporaryDirectory(prefix="private-ci-diagnostics-") as temporary:
        temporary = Path(temporary)
        archive = temporary / "diagnostics.zip"
        encrypted = temporary / "diagnostics.p7m"
        write_archive(archive, root, entries)
        subprocess.run(
            [openssl_path(), "cms", "-encrypt", "-binary", "-aes-256-cbc", "-outform", "DER",
             "-in", str(archive), "-out", str(encrypted), str(certificate)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
        if not encrypted.is_file() or encrypted.stat().st_size <= 0:
            raise ValueError("Encrypted archive is empty")
        created = False
        try:
            with output.open("xb") as target:
                created = True
                with encrypted.open("rb") as source:
                    shutil.copyfileobj(source, target)
        except BaseException:
            if created:
                output.unlink(missing_ok=True)
            raise


def main():
    parser = argparse.ArgumentParser(description="Encrypt private CI diagnostics with a public certificate")
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--logs-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exclude-screenshots", action="store_true",
                        help="Omit ordinary PNG files while preserving input validation and archive limits")
    arguments = parser.parse_args()
    try:
        encrypt(arguments.certificate.absolute(), arguments.logs_dir.absolute(), arguments.output.absolute(),
                exclude_screenshots=arguments.exclude_screenshots)
    except Exception as failure:
        # Exceptions, subprocess errors and file paths can contain source or
        # credentials. Publish only a fixed category, never their bodies.
        if isinstance(failure, DiagnosticLimitError):
            category = "input_limit"
        elif isinstance(failure, (OpenSSLUnavailableError, subprocess.SubprocessError)):
            category = "openssl"
        elif isinstance(failure, OSError):
            category = "filesystem"
        else:
            category = "invalid_input"
        print(f"Private diagnostics encryption failed ({category}); no plaintext was published.", file=sys.stderr)
        return 1
    print("Encrypted diagnostics archive created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
