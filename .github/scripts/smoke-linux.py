#!/usr/bin/env python3
"""Check an extracted Linux runtime without falling back to the build SDK."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="launcher-runtime-check-") as temporary:
        root = Path(temporary)
        with tarfile.open(args.archive, "r:gz") as archive:
            archive.extractall(root / "extracted", filter="data")
        launchers = list((root / "extracted").glob("*/LittleCraftLauncher"))
        if len(launchers) != 1:
            raise RuntimeError("The runtime package must contain exactly one launcher")
        launcher = launchers[0]
        app = launcher.parent
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("QT_", "QML_", "QML2_"))
            and key not in ("LD_LIBRARY_PATH", "LD_PRELOAD", "CMAKE_PREFIX_PATH", "QTDIR", "QT6_DIR")
        }
        environment["LD_LIBRARY_PATH"] = str(app / "lib")
        environment["QT_QUICK_BACKEND"] = "software"
        environment["QML_DISABLE_DISK_CACHE"] = "1"
        # A fresh state root is outside the runtime tree and never rearchived.
        state = root / "user-state"
        cli = subprocess.run(
            [str(launcher), "--cli", "--data-root", str(state)],
            env=environment, capture_output=True, text=True, timeout=30,
        )
        if cli.returncode != 0 or "NATIVE_STATE_READ" not in cli.stdout:
            raise RuntimeError("Extracted runtime CLI check failed: " + cli.stderr)
        if not shutil.which("xvfb-run"):
            raise RuntimeError("xvfb-run is required for the package GUI check")
        with (root / "gui.log").open("wb") as log:
            process = subprocess.Popen(
                ["xvfb-run", "-a", str(launcher), "--data-root", str(state)],
                env=environment, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Extracted GUI exited before the startup check completed")
                    time.sleep(0.2)
                # Inspect the application process, not the xvfb wrapper. Qt must
                # come from this package; an absolute build RPATH is insufficient.
                observed = set()
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        executable = (entry / "exe").resolve(strict=True)
                        if executable != (app / "mclauncher-qt").resolve():
                            continue
                        for line in (entry / "maps").read_text().splitlines():
                            if "/libQt6" in line:
                                path = line.split()[-1]
                                if not Path(path).is_relative_to(app):
                                    raise RuntimeError("GUI loaded Qt outside the runtime package")
                                observed.add(Path(path).name)
                    except (PermissionError, FileNotFoundError, ProcessLookupError):
                        continue
                for name in ("libQt6Core", "libQt6Gui", "libQt6Qml", "libQt6Quick"):
                    if not any(value.startswith(name + ".") for value in observed):
                        raise RuntimeError("A required packaged Qt GUI library was not observed: " + name)
                print("Extracted Linux CLI and GUI startup checks passed; gameplay was not tested.")
            finally:
                # This check measures startup only, not normal GUI shutdown.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)


if __name__ == "__main__":
    main()
