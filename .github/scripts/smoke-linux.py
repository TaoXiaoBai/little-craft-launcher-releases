#!/usr/bin/env python3
"""Exercise real X11/Wayland windows from the distributed Linux runtime.

This is an isolated startup gate, not gameplay, GPU, or graceful-shutdown proof.
No build SDK paths are supplied, including when testing the raw packaged ELF.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
REQUIRED_QT = ("libQt6Core", "libQt6Gui", "libQt6Qml", "libQt6Quick")


class RuntimeNotReady(RuntimeError):
    """Required imports may still arrive; integrity failures must not be retried."""


def clean_environment(source: dict[str, str]) -> dict[str, str]:
    """Change only child environments; keep display/session and explicit QPA choice."""
    removed = {"QTDIR", "QT6_DIR", "CMAKE_PREFIX_PATH", "CMAKE_MODULE_PATH",
               "APPDIR", "OWD", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
               "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "SDKROOT", "QMAKEPATH", "QMAKEFEATURES"}
    result = {key: value for key, value in source.items()
              if not key.upper().startswith(("QT_", "QML_", "QML2_", "LD_", "APPIMAGE"))
              and key.upper() not in removed}
    if "QT_QPA_PLATFORM" in source:
        result["QT_QPA_PLATFORM"] = source["QT_QPA_PLATFORM"]
    result["PATH"] = SYSTEM_PATH
    return result


def parse_maps(text: str) -> list[Path]:
    result = []
    for line in text.splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6 or not fields[5].startswith("/"):
            continue
        name = fields[5]
        if name.endswith(" (deleted)"):
            name = name[:-10]
        name = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), name)
        result.append(Path(name))
    return result


def validate_runtime_maps(text: str, bundle: Path, backend: str) -> set[str]:
    """Reject mixed Qt runtimes, then require QPA and dynamic QML imports too."""
    if backend not in ("xcb", "wayland"):
        raise RuntimeError("Only real xcb/wayland desktop backends are accepted")
    bundle = bundle.resolve()
    libraries = (bundle / "usr/lib").resolve()
    plugins = (bundle / "usr/plugins").resolve()
    qml = (bundle / "usr/qml").resolve()
    for directory in (libraries, plugins, qml):
        if not directory.is_relative_to(bundle):
            raise RuntimeError("A packaged runtime directory escapes the bundle")
    observed: set[str] = set()
    qpa = False
    qml_plugin = False
    for original in parse_maps(text):
        path = original.resolve()
        name = path.name
        qt_library = re.match(r"libQt[56][^/]*\.so(?:\.|$)", name) is not None
        platform_plugin = name == "libqxcb.so" or name.startswith("libqwayland")
        runtime_plugin = ("plugins" in original.parts or "qml" in original.parts
                          or any(part.startswith("QtQuick") for part in original.parts)
                          or ("plugin" in name and ("quick" in name or "qml" in name)))
        if qt_library and not path.is_relative_to(libraries):
            raise RuntimeError(f"GUI loaded Qt outside packaged usr/lib: {original}")
        if (platform_plugin or runtime_plugin) and not path.is_relative_to(bundle):
            raise RuntimeError(f"GUI loaded a plugin/QML object outside the package: {original}")
        if qt_library:
            observed.add(name)
        if path.parent == plugins / "platforms" and ".so" in name:
            matches = name == "libqxcb.so" if backend == "xcb" else name.startswith("libqwayland")
            if not matches:
                raise RuntimeError(f"Unexpected QPA plugin for forced {backend}: {name}")
            qpa = True
        if path.is_relative_to(qml) and ".so" in name:
            qml_plugin = True
    missing = [name for name in REQUIRED_QT
               if not any(value.startswith(name + ".so") for value in observed)]
    if missing or not qpa or not qml_plugin:
        raise RuntimeNotReady("Runtime maps are incomplete: " + ", ".join(
            missing + ([] if qpa else [backend + " platform plugin"])
            + ([] if qml_plugin else ["packaged QML plugin"])))
    return observed


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    pgrp: int
    start_time: int


def read_process_identity(pid: int, proc_root: Path = Path("/proc")) -> ProcessIdentity | None:
    try:
        text = (proc_root / str(pid) / "stat").read_text()
        fields = text[text.rfind(")") + 2:].split()
        return ProcessIdentity(pid, int(fields[1]), int(fields[2]), int(fields[19]))
    except (OSError, ValueError, IndexError):
        return None


def read_process_children(pid: int, proc_root: Path = Path("/proc"),
                          parent_identity: ProcessIdentity | None = None) -> tuple[int, ...]:
    children: set[int] = set()
    interface_available = False
    try:
        for thread in (proc_root / str(pid) / "task").iterdir():
            try:
                children.update(int(value) for value in (thread / "children").read_text().split())
                interface_available = True
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    if interface_available:
        return tuple(sorted(children))
    # Some cloud kernels omit CONFIG_CHECKPOINT_RESTORE's children interface.
    # Enumerate only stat records, never unrelated exe/environment/maps. Adopt
    # only PPID matches while the already-owned parent's start time is stable.
    before = read_process_identity(pid, proc_root)
    if before is None or (parent_identity is not None
                          and before.start_time != parent_identity.start_time):
        return ()
    try:
        for entry in proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            child = read_process_identity(int(entry.name), proc_root)
            if child and child.ppid == pid and child.start_time >= before.start_time:
                children.add(child.pid)
    except OSError:
        return ()
    after = read_process_identity(pid, proc_root)
    if after is None or after.start_time != before.start_time:
        return ()
    return tuple(sorted(children))


class OwnedProcess:
    """Track only this session's PID and descendants, including PID reuse guards."""
    def __init__(self, argv: list[str], env: dict[str, str], log: Path, cwd: Path,
                 deadline: float, proc_root: Path = Path("/proc")):
        self.argv, self.env, self.log, self.cwd = argv, env, log, cwd
        self.deadline, self.proc_root = deadline, proc_root
        self.process = None
        self.handle = None
        self.identities: dict[int, ProcessIdentity] = {}
        self.closed = False

    def start(self):
        if self.process is not None:
            raise RuntimeError("An owned process may only start once")
        remaining(self.deadline)
        self.handle = self.log.open("wb")
        try:
            self.process = subprocess.Popen(
                self.argv, env=self.env, cwd=self.cwd, stdout=self.handle,
                stderr=subprocess.STDOUT, start_new_session=True)
            identity = read_process_identity(self.process.pid, self.proc_root)
            if identity is None and self.process.poll() is not None:
                return self  # A short extraction command can finish before /proc is read.
            if identity is None or identity.pgrp != self.process.pid:
                # Popen's unreaped direct child remains ours if /proc admission fails.
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
                raise RuntimeError("Could not establish the new child process session")
            self.identities[identity.pid] = identity
            self.tracked_pids()
        except BaseException:
            # __exit__ is not called when __enter__/start itself raises.
            # Once the session identity is recorded, use the same ownership
            # guards as ordinary cleanup, including already observed children.
            try:
                if self.identities:
                    self.close()
            finally:
                self.handle.close()
            raise
        return self

    def tracked_pids(self) -> tuple[int, ...]:
        pending = list(self.identities)
        live: set[int] = set()
        while pending:
            pid = pending.pop()
            if pid in live:
                continue
            identity = read_process_identity(pid, self.proc_root)
            saved = self.identities.get(pid)
            if identity is None or (saved and identity.start_time != saved.start_time):
                continue
            live.add(pid)
            for child in read_process_children(pid, self.proc_root, identity):
                child_identity = read_process_identity(child, self.proc_root)
                if child_identity is None or child_identity.ppid != pid:
                    continue
                saved_child = self.identities.get(child)
                if saved_child and saved_child.start_time != child_identity.start_time:
                    continue
                self.identities[child] = child_identity
                pending.append(child)
        return tuple(sorted(live))

    def _signal_owned(self, signum: int):
        live = self.tracked_pids()
        current_live = [current for pid in live
                        if (current := read_process_identity(pid, self.proc_root)) is not None
                        and current.start_time == self.identities[pid].start_time]
        if any(current.pgrp == self.process.pid for current in current_live):
            try:
                os.killpg(self.process.pid, signum)
            except ProcessLookupError:
                pass
        for current in current_live:
            if current.pgrp != self.process.pid:
                try:
                    os.kill(current.pid, signum)
                except ProcessLookupError:
                    pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.process is not None:
                self._signal_owned(signal.SIGTERM)
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._signal_owned(signal.SIGKILL)
                    self.process.wait(timeout=3)
                if self.tracked_pids():
                    self._signal_owned(signal.SIGKILL)
        finally:
            if self.handle is not None:
                self.handle.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()


def remaining(deadline: float, maximum: float | None = None) -> float:
    result = deadline - time.monotonic()
    if result <= 0:
        raise RuntimeError("Linux runtime smoke exceeded its total time budget")
    return min(result, maximum) if maximum is not None else result


def log_tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 16000))
            return handle.read().decode(errors="replace")
    except OSError:
        return "(no process log)"


def run_command(argv: list[str], env: dict[str, str], cwd: Path,
                log: Path, deadline: float, timeout: float = 30) -> str:
    with OwnedProcess(argv, env, log, cwd, deadline) as owned:
        try:
            code = owned.process.wait(timeout=remaining(deadline, timeout))
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Runtime command timed out:\n" + log_tail(log)) from error
        if code != 0:
            raise RuntimeError(f"Runtime command exited {code}:\n" + log_tail(log))
    return log.read_text(errors="replace")


def validate_layout(bundle: Path, tag: str) -> None:
    bundle = bundle.resolve(strict=True)
    for relative in ("AppRun", "runtime-manifest.json", "SUPPORTING_MATERIALS.zip",
                     "usr/bin/mclauncher-qt", "usr/bin/qt.conf"):
        path = bundle / relative
        if not path.is_file() or not path.resolve().is_relative_to(bundle):
            raise RuntimeError("Missing or escaping AppDir file: " + relative)
    for relative in ("usr/lib", "usr/plugins", "usr/qml"):
        path = bundle / relative
        if not path.is_dir() or not path.resolve().is_relative_to(bundle):
            raise RuntimeError("Missing or escaping AppDir directory: " + relative)
    manifest = json.loads((bundle / "runtime-manifest.json").read_text())
    if manifest.get("version") != tag:
        raise RuntimeError("Extracted runtime manifest does not match the requested tag")


def case_environment(base: dict[str, str], directory: Path, backend: str) -> dict[str, str]:
    env = dict(base)
    for relative, key in (("home", "HOME"), ("cache", "XDG_CACHE_HOME"),
                          ("config", "XDG_CONFIG_HOME"), ("data", "XDG_DATA_HOME"), ("tmp", "TMPDIR")):
        path = directory / relative
        path.mkdir(parents=True, mode=0o700)
        env[key] = str(path)
    env["QT_QPA_PLATFORM"] = backend
    env["QT_QUICK_BACKEND"] = "software"
    env["QML_DISABLE_DISK_CACHE"] = "1"
    if backend == "wayland":
        env.pop("DISPLAY", None)
        env["WAYLAND_DEBUG"] = "client"
    else:
        runtime = directory / "runtime"
        runtime.mkdir(mode=0o700)
        env["XDG_RUNTIME_DIR"] = str(runtime)
        env["XDG_SESSION_TYPE"] = "x11"
        env.pop("WAYLAND_DISPLAY", None)
    return env


def process_environment(pid: int) -> dict[str, str]:
    return dict(item.decode(errors="replace").split("=", 1)
                for item in (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0") if b"=" in item)


def x11_window_visible(environment: dict[str, str], pid: int, deadline: float) -> bool:
    environment = dict(environment, LC_ALL="C")
    tree = subprocess.run(["xwininfo", "-root", "-tree"], env=environment,
                          capture_output=True, text=True, timeout=remaining(deadline, 3))
    if tree.returncode:
        return False
    for line in tree.stdout.splitlines():
        match = re.match(r'^\s*(0x[0-9a-fA-F]+)\s+.*?\b(\d+)x(\d+)[+-]', line)
        if not match or int(match[2]) < 300 or int(match[3]) < 200:
            continue
        window = match[1]
        owner = subprocess.run(["xprop", "-id", window, "_NET_WM_PID"], env=environment,
                               capture_output=True, text=True, timeout=remaining(deadline, 3))
        if not re.search(r"=\s*" + str(pid) + r"\s*$", owner.stdout):
            continue
        info = subprocess.run(["xwininfo", "-id", window], env=environment,
                              capture_output=True, text=True, timeout=remaining(deadline, 3))
        if info.returncode == 0 and "Map State: IsViewable" in info.stdout:
            return True
    return False


def wayland_window_visible(log: Path) -> bool:
    # Initial xdg-shell commits carry no buffer. Require an acknowledged
    # toplevel and a later nonempty buffer commit on that very wl_surface.
    surfaces: dict[str, str] = {}
    toplevels: set[str] = set()
    acknowledged: set[str] = set()
    attached: set[str] = set()
    for line in log.read_text(errors="replace").splitlines():
        match = re.search(r"xdg_wm_base(?:@|#)\d+\.get_xdg_surface\(new id xdg_surface(?:@|#)(\d+),\s*wl_surface(?:@|#)(\d+)", line)
        if match:
            surfaces[match[2]] = match[1]
        match = re.search(r"xdg_surface(?:@|#)(\d+)\.get_toplevel\(", line)
        if match:
            toplevels.add(match[1])
        match = re.search(r"xdg_surface(?:@|#)(\d+)\.ack_configure\(", line)
        if match:
            acknowledged.add(match[1])
        match = re.search(r"wl_surface(?:@|#)(\d+)\.attach\(([^,]+),", line)
        if match:
            if re.fullmatch(r"wl_buffer(?:@|#)\d+", match[2].strip()):
                attached.add(match[1])
            else:
                attached.discard(match[1])
        match = re.search(r"wl_surface(?:@|#)(\d+)\.commit\(", line)
        if match and match[1] in attached:
            xdg = surfaces.get(match[1])
            if xdg in toplevels and xdg in acknowledged:
                return True
    return False


def smoke_case(command: list[str], base: dict[str, str], directory: Path, backend: str,
               extracted: Path | None, temporary: Path, tag: str, deadline: float,
               startup_timeout: float, observe_seconds: float, appimage: bool = False):
    directory.mkdir()
    env = case_environment(base, directory, backend)
    if appimage:
        env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
    state = directory / "state"
    argv = command + ["--data-root", str(state)]
    if backend == "xcb":
        argv = ["xvfb-run", "-a", "-s", "-screen 0 1280x840x24 -nolisten tcp"] + argv
    log = directory / "gui.log"
    ready_since = None
    last_error = "No owned launcher process was observed"
    startup_deadline = min(deadline, time.monotonic() + startup_timeout)
    with OwnedProcess(argv, env, log, directory, deadline) as owned:
        try:
            while True:
                remaining(startup_deadline)
                if owned.process.poll() is not None:
                    raise RuntimeError(f"GUI exited before verification ({owned.process.returncode})")
                valid = False
                for pid in owned.tracked_pids():
                    try:
                        executable = (Path("/proc") / str(pid) / "exe").resolve(strict=True)
                        if executable.name != "mclauncher-qt":
                            continue
                        bundle = executable.parent.parent.parent
                        if executable != bundle / "usr/bin/mclauncher-qt":
                            continue
                        if not bundle.is_relative_to(temporary):
                            raise RuntimeError("Owned launcher executable escaped the controlled temporary directory")
                        if extracted is not None and bundle != extracted.resolve():
                            raise RuntimeError("Extracted entry ran a different launcher executable")
                        validate_layout(bundle, tag)
                        maps = (Path("/proc") / str(pid) / "maps").read_text()
                        observed = validate_runtime_maps(maps, bundle, backend)
                        child_env = clean_environment(process_environment(pid))
                        visible = (x11_window_visible(child_env, pid, startup_deadline)
                                   if backend == "xcb" else wayland_window_visible(log))
                        if not visible:
                            last_error = "Packaged Qt loaded, but no owned visible native window was observed"
                            continue
                        valid = True
                    except (FileNotFoundError, ProcessLookupError, PermissionError):
                        continue
                    except RuntimeNotReady as error:
                        last_error = str(error)
                if valid:
                    if ready_since is None:
                        ready_since = time.monotonic()
                    if time.monotonic() - ready_since >= observe_seconds:
                        print(f"PASS {directory.name}: real {backend} window; packaged Qt/QPA/QML observed ({len(observed)} Qt objects)", flush=True)
                        return
                else:
                    ready_since = None
                time.sleep(min(0.2, remaining(startup_deadline)))
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"{directory.name} failed: {error}\nLast observation: {last_error}\n{log_tail(log)}") from error


def start_weston(base: dict[str, str], directory: Path, deadline: float) -> tuple[OwnedProcess, dict[str, str]]:
    directory.mkdir()
    runtime = directory / "runtime"
    runtime.mkdir(mode=0o700)
    env = dict(base, XDG_RUNTIME_DIR=str(runtime), WAYLAND_DISPLAY="wayland-smoke", XDG_SESSION_TYPE="wayland")
    env.pop("DISPLAY", None)
    log = directory / "weston.log"
    server = OwnedProcess([
        "weston", "--backend=headless-backend.so", "--use-pixman", "--no-config",
        "--socket=wayland-smoke", "--idle-time=0", "--width=1280", "--height=840",
    ], env, log, directory, deadline).start()
    try:
        ready_deadline = min(deadline, time.monotonic() + 15)
        while not (runtime / "wayland-smoke").is_socket():
            if server.process.poll() is not None:
                raise RuntimeError("Weston exited:\n" + log_tail(log))
            time.sleep(min(0.1, remaining(ready_deadline)))
        return server, env
    except BaseException:
        server.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path, help="The built x86_64 AppImage")
    parser.add_argument("--tag", help="Expected manifest version; otherwise inferred from the asset filename")
    parser.add_argument("--backend", choices=("xcb", "wayland", "both"), default="both")
    parser.add_argument("--timeout", type=float, default=180, help="Total gate budget in seconds")
    parser.add_argument("--startup-timeout", type=float, default=30)
    parser.add_argument("--observe-seconds", type=float, default=2)
    args = parser.parse_args()
    if args.timeout <= 0 or args.startup_timeout <= 0 or args.observe_seconds <= 0:
        parser.error("All timeout/observation values must be positive")
    archive = args.archive.absolute()
    if archive.is_symlink() or not archive.is_file():
        parser.error("--archive must be an ordinary AppImage file")
    match = re.fullmatch(r"LittleCraftLauncher-(.+)-linux-x86_64\.AppImage", archive.name)
    tag = args.tag or (match[1] if match else None)
    if not tag:
        parser.error("Pass --tag when the archive does not use the standard AppImage asset name")
    deadline = time.monotonic() + args.timeout
    base = clean_environment(dict(os.environ))
    backends = ("xcb", "wayland") if args.backend == "both" else (args.backend,)
    tools = {"xvfb-run", "xauth", "xwininfo", "xprop"} if "xcb" in backends else set()
    if "wayland" in backends:
        tools.add("weston")
    for tool in sorted(tools):
        if not shutil.which(tool, path=base["PATH"]):
            raise RuntimeError(f"Required desktop smoke tool is unavailable: {tool}")
    inspector = importlib.import_module("appimage_format")
    inspector.inspect_appimage(archive, tag)
    with tempfile.TemporaryDirectory(prefix="launcher-appimage-smoke-") as temporary_name:
        temporary = Path(temporary_name).resolve()
        image = temporary / "runtime.AppImage"
        shutil.copyfile(archive, image)
        image.chmod(0o700)
        extraction = temporary / "extract"
        extraction.mkdir()
        extraction_env = dict(base, TMPDIR=str(temporary))
        run_command([str(image), "--appimage-extract"], extraction_env, extraction,
                    temporary / "extract.log", deadline, timeout=45)
        extracted = extraction / "squashfs-root"
        if extracted.is_symlink() or not extracted.is_dir():
            raise RuntimeError("AppImage did not produce an ordinary squashfs-root directory")
        validate_layout(extracted, tag)
        entries = (("appimage", [str(image), "--appimage-extract-and-run"], None, True),
                   ("apprun", [str(extracted / "AppRun")], extracted, False),
                   ("raw-elf", [str(extracted / "usr/bin/mclauncher-qt")], extracted, False))
        for backend in backends:
            server = None
            backend_env = base
            try:
                if backend == "wayland":
                    server, backend_env = start_weston(base, temporary / "weston", deadline)
                for name, command, bundle, is_image in entries:
                    if server is not None and server.process.poll() is not None:
                        raise RuntimeError("Weston exited during the desktop smoke gate")
                    smoke_case(command, backend_env, temporary / f"{backend}-{name}", backend,
                               bundle, temporary, tag, deadline, args.startup_timeout,
                               args.observe_seconds, appimage=is_image)
                    if server is not None and server.process.poll() is not None:
                        raise RuntimeError("Weston exited while the Wayland application was observed")
            finally:
                if server is not None:
                    server.close()
        print("Linux AppImage/AppRun/raw ELF desktop startup gate passed. Software-rendered UI preview only; gameplay and GPU acceleration were not tested.")


if __name__ == "__main__":
    main()
