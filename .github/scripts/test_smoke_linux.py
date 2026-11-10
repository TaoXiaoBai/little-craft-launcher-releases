#!/usr/bin/env python3
"""Offline environment, runtime-map, and owned-process tests; no GUI is started."""
import importlib.util
from contextlib import ExitStack
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("smoke_linux_under_test", SCRIPT_ROOT / "smoke-linux.py")
SMOKE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SMOKE
SPEC.loader.exec_module(SMOKE)


def maps_for(paths):
    return "\n".join(f"7f000000-7f010000 r-xp 00000000 08:01 42 {path}" for path in paths)


def runtime_paths(bundle, backend="xcb"):
    return ([bundle / "usr/lib" / (name + ".so.6.8.3") for name in SMOKE.REQUIRED_QT]
            + [bundle / "usr/plugins/platforms" / ("libqxcb.so" if backend == "xcb" else "libqwayland-generic.so")]
            + [bundle / "usr/qml/QtQuick/libqtquick2plugin.so"])


class EnvironmentTests(unittest.TestCase):
    def test_pollution_is_removed_without_changing_the_parent(self):
        pollution = {
            "QT_PLUGIN_PATH": "/sdk/plugins", "QT_QPA_PLATFORM_PLUGIN_PATH": "/foreign/platforms",
            "QML_IMPORT_PATH": "/sdk/qml", "QML2_IMPORT_PATH": "/sdk/qml2",
            "QT_QUICK_BACKEND": "foreign", "QML_DISABLE_DISK_CACHE": "0",
            "LD_LIBRARY_PATH": "/sdk/lib", "LD_PRELOAD": "/foreign.so", "LD_AUDIT": "/audit.so",
            "LD_DEBUG": "all", "QTDIR": "/sdk", "Qt6_DIR": "/sdk/cmake",
            "QT_HOST_PATH": "/sdk/host", "CMAKE_PREFIX_PATH": "/sdk", "CMAKE_MODULE_PATH": "/sdk",
            "SDKROOT": "/sdk", "QMAKEPATH": "/sdk", "QMAKEFEATURES": "/sdk",
            "PYTHONPATH": "/foreign/python", "PYTHONHOME": "/foreign/python",
            "VIRTUAL_ENV": "/venv", "CONDA_PREFIX": "/conda", "CONDA_DEFAULT_ENV": "qt",
            "APPDIR": "/old/image", "APPIMAGE": "/old.AppImage", "APPIMAGE_EXTRACT_AND_RUN": "1",
            "OWD": "/old", "PATH": "/sdk/bin:/conda/bin:/usr/bin",
        }
        preserved = {"DISPLAY": ":91", "WAYLAND_DISPLAY": "wayland-7", "XDG_RUNTIME_DIR": "/run/private",
                     "XAUTHORITY": "/private/Xauthority", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/bus",
                     "XDG_SESSION_TYPE": "wayland", "LANG": "zh_CN.UTF-8", "HOME": "/home/test",
                     "QT_QPA_PLATFORM": "wayland;xcb"}
        source = dict(pollution, **preserved)
        before = dict(source)
        result = SMOKE.clean_environment(source)
        self.assertEqual(source, before)
        self.assertEqual(result, dict(preserved, PATH=SMOKE.SYSTEM_PATH))

    def test_no_backend_is_invented_and_no_bundle_library_path_is_injected(self):
        result = SMOKE.clean_environment({"DISPLAY": ":9"})
        self.assertEqual(result, {"DISPLAY": ":9", "PATH": SMOKE.SYSTEM_PATH})
        self.assertNotIn("QT_QPA_PLATFORM", result)
        self.assertNotIn("LD_LIBRARY_PATH", result)

    def test_case_roots_are_private_and_wayland_keeps_the_compositor_socket(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            base = {"PATH": SMOKE.SYSTEM_PATH, "DISPLAY": ":9", "WAYLAND_DISPLAY": "owned-wayland",
                    "XDG_RUNTIME_DIR": "/owned/compositor", "QT_QPA_PLATFORM": "offscreen"}
            before = dict(base)
            result = SMOKE.case_environment(base, directory, "wayland")
            self.assertEqual(base, before)
            self.assertNotIn("DISPLAY", result)
            self.assertEqual(result["WAYLAND_DISPLAY"], "owned-wayland")
            self.assertEqual(result["XDG_RUNTIME_DIR"], "/owned/compositor")
            self.assertEqual(result["QT_QPA_PLATFORM"], "wayland")
            self.assertEqual(result["QT_QUICK_BACKEND"], "software")
            for key in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "TMPDIR"):
                path = Path(result[key])
                self.assertTrue(path.is_relative_to(directory))
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("LD_LIBRARY_PATH", result)

    def test_xcb_case_has_its_own_runtime_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = SMOKE.case_environment({"WAYLAND_DISPLAY": "foreign", "XDG_RUNTIME_DIR": "/foreign"},
                                            Path(temporary), "xcb")
            self.assertNotIn("WAYLAND_DISPLAY", result)
            self.assertEqual(result["XDG_SESSION_TYPE"], "x11")
            self.assertEqual(Path(result["XDG_RUNTIME_DIR"]).stat().st_mode & 0o777, 0o700)
            self.assertEqual(result["QT_QPA_PLATFORM"], "xcb")


class RuntimeMapsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.bundle = Path(self.temporary.name) / "bundle with spaces"

    def test_parse_maps_keeps_spaces_and_decodes_kernel_escapes(self):
        source = ("100-200 r--p 0 00:00 0 [heap]\n"
                  "200-300 r-xp 0 00:00 1 /bundle with spaces/usr/lib/libQt6Core.so.6 (deleted)\n"
                  r"300-400 r--p 0 00:00 2 /bundle/a\011b\134c.so" + "\nmalformed")
        self.assertEqual(SMOKE.parse_maps(source),
                         [Path("/bundle with spaces/usr/lib/libQt6Core.so.6"), Path("/bundle/a\tb\\c.so")])

    def test_complete_xcb_and_wayland_runtime_maps_pass(self):
        for backend in ("xcb", "wayland"):
            with self.subTest(backend=backend):
                observed = SMOKE.validate_runtime_maps(maps_for(runtime_paths(self.bundle, backend)), self.bundle, backend)
                self.assertEqual(observed, {name + ".so.6.8.3" for name in SMOKE.REQUIRED_QT})

    def test_each_required_qt_library_must_be_observed(self):
        paths = runtime_paths(self.bundle)
        for index in range(4):
            with self.subTest(index=index), self.assertRaises(SMOKE.RuntimeNotReady):
                SMOKE.validate_runtime_maps(maps_for(paths[:index] + paths[index + 1:]), self.bundle, "xcb")

    def test_quick_controls_cannot_substitute_for_quick(self):
        paths = runtime_paths(self.bundle)
        paths[3] = self.bundle / "usr/lib/libQt6QuickControls2.so.6"
        with self.assertRaises(SMOKE.RuntimeNotReady):
            SMOKE.validate_runtime_maps(maps_for(paths), self.bundle, "xcb")

    def test_platform_and_qml_import_are_both_required(self):
        paths = runtime_paths(self.bundle)
        for index in (4, 5):
            with self.subTest(index=index), self.assertRaises(SMOKE.RuntimeNotReady):
                SMOKE.validate_runtime_maps(maps_for(paths[:index] + paths[index + 1:]), self.bundle, "xcb")

    def test_wrong_backend_and_offscreen_cannot_pass(self):
        with self.assertRaisesRegex(RuntimeError, "Unexpected QPA"):
            SMOKE.validate_runtime_maps(maps_for(runtime_paths(self.bundle, "wayland")), self.bundle, "xcb")
        for backend in ("offscreen", "minimal", "both"):
            with self.subTest(backend=backend), self.assertRaises(RuntimeError):
                SMOKE.validate_runtime_maps(maps_for(runtime_paths(self.bundle)), self.bundle, backend)
        paths = runtime_paths(self.bundle)
        paths[4] = self.bundle / "usr/plugins/platforms/libqoffscreen.so"
        with self.assertRaisesRegex(RuntimeError, "Unexpected QPA"):
            SMOKE.validate_runtime_maps(maps_for(paths), self.bundle, "xcb")

    def test_host_or_sdk_qt_is_fatal_even_with_all_packaged_libraries_present(self):
        for name in ("libQt6Core.so.6", "libQt5Core.so.5", "libQt6Svg.so.6"):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "outside"):
                SMOKE.validate_runtime_maps(maps_for(runtime_paths(self.bundle) + [Path("/sdk/lib") / name]), self.bundle, "xcb")

    def test_external_platform_plugin_or_qml_module_is_fatal(self):
        for path in ("/sdk/plugins/platforms/libqxcb.so", "/foreign/libqwayland-generic.so",
                     "/sdk/qml/QtQuick/libqtquick2plugin.so", "/foreign/QtQuick/libfoo.so"):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, "outside"):
                SMOKE.validate_runtime_maps(maps_for(runtime_paths(self.bundle) + [Path(path)]), self.bundle, "xcb")

    def test_bundle_prefix_is_not_a_containment_check(self):
        prefix_impostor = Path(str(self.bundle) + "-foreign") / "usr/lib/libQt6Core.so.6"
        with self.assertRaisesRegex(RuntimeError, "outside"):
            SMOKE.validate_runtime_maps(maps_for(runtime_paths(self.bundle) + [prefix_impostor]), self.bundle, "xcb")

    def test_a_linked_runtime_directory_cannot_escape(self):
        (self.bundle / "usr").mkdir(parents=True)
        outside = Path(self.temporary.name) / "external-libraries"
        outside.mkdir()
        (self.bundle / "usr/lib").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "escapes"):
            SMOKE.validate_runtime_maps(maps_for(runtime_paths(self.bundle)), self.bundle, "xcb")

    def test_an_individual_library_link_cannot_escape(self):
        directory = self.bundle / "usr/lib"
        directory.mkdir(parents=True)
        outside = Path(self.temporary.name) / "libQt6Core.so.6.8.3"
        outside.touch()
        (directory / outside.name).symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "outside"):
            SMOKE.validate_runtime_maps(maps_for(runtime_paths(self.bundle)), self.bundle, "xcb")


class ProcessFileTests(unittest.TestCase):
    @staticmethod
    def write_identity(root, pid, ppid, start, pgrp=100):
        directory = root / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        fields = ["S"] + ["0"] * 19
        fields[1], fields[2], fields[19] = str(ppid), str(pgrp), str(start)
        (directory / "stat").write_text(f"{pid} (synthetic worker) " + " ".join(fields))

    def test_stat_parser_handles_spaces_and_parentheses_in_the_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "101").mkdir()
            fields = ["S"] + ["0"] * 19
            fields[1], fields[2], fields[19] = "100", "99", "123456"
            (root / "101/stat").write_text("101 (odd ) process) " + " ".join(fields))
            self.assertEqual(SMOKE.read_process_identity(101, root), SMOKE.ProcessIdentity(101, 100, 99, 123456))
            self.assertIsNone(SMOKE.read_process_identity(999, root))

    def test_children_are_read_only_from_the_owned_process_threads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for thread, values in (("100", "101 102"), ("110", "102 103")):
                directory = root / "100/task" / thread
                directory.mkdir(parents=True)
                (directory / "children").write_text(values)
            (root / "999/task/999").mkdir(parents=True)
            (root / "999/task/999/children").write_text("666")
            self.assertEqual(SMOKE.read_process_children(100, root), (101, 102, 103))
            self.assertEqual(SMOKE.read_process_children(55, root), ())

    def test_missing_children_interface_uses_only_stat_to_find_owned_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_identity(root, 100, 9, 10)
            self.write_identity(root, 101, 100, 11)
            self.write_identity(root, 102, 100, 9)  # predates the owned parent
            self.write_identity(root, 103, 101, 12)  # not its direct child yet
            self.write_identity(root, 999, 77, 20)  # an unrelated process
            (root / "100/task/100").mkdir(parents=True)
            (root / "not-a-pid").mkdir()
            (root / "104").mkdir()
            (root / "104/stat").write_text("malformed stat entry")
            for pid in (101, 102, 103, 999):
                (root / str(pid) / "maps").write_text("must not be read")
                (root / str(pid) / "environ").write_bytes(b"must not be read")
                (root / str(pid) / "exe").symlink_to("/unrelated/executable")
            read_paths = []
            original_read_text = Path.read_text
            def guarded_read(path, *args, **kwargs):
                read_paths.append(path)
                self.assertNotIn(path.name, ("exe", "environ", "maps"))
                return original_read_text(path, *args, **kwargs)
            with mock.patch.object(Path, "read_text", guarded_read), \
                    mock.patch.object(Path, "read_bytes", side_effect=AssertionError("No environment/maps may be read")), \
                    mock.patch.object(Path, "readlink", side_effect=AssertionError("No executable may be read")):
                children = SMOKE.read_process_children(100, root, SMOKE.ProcessIdentity(100, 9, 100, 10))
            self.assertEqual(children, (101,))
            self.assertIn(root / "999/stat", read_paths)
            self.assertNotIn(root / "not-a-pid/stat", read_paths)

    def test_readable_empty_children_interface_does_not_scan_other_pids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_identity(root, 100, 9, 10)
            self.write_identity(root, 101, 100, 11)
            directory = root / "100/task/100"
            directory.mkdir(parents=True)
            (directory / "children").write_text("")
            original_iterdir = Path.iterdir
            def guarded_iterdir(path):
                self.assertNotEqual(path, root, "A readable children interface must avoid a process-table scan")
                return original_iterdir(path)
            with mock.patch.object(Path, "iterdir", guarded_iterdir):
                self.assertEqual(SMOKE.read_process_children(100, root, SMOKE.ProcessIdentity(100, 9, 100, 10)), ())

    def test_fallback_rejects_a_parent_identity_that_was_already_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_identity(root, 100, 9, 30)
            self.write_identity(root, 101, 100, 31)
            self.assertEqual(SMOKE.read_process_children(100, root, SMOKE.ProcessIdentity(100, 9, 100, 10)), ())

    def test_fallback_rejects_parent_pid_reuse_during_the_stat_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_identity(root, 100, 9, 10)
            self.write_identity(root, 101, 100, 11)
            original_read_identity = SMOKE.read_process_identity
            parent_reads = 0
            def replace_parent_on_second_read(pid, proc_root):
                nonlocal parent_reads
                if pid == 100:
                    parent_reads += 1
                    if parent_reads > 1:
                        return SMOKE.ProcessIdentity(100, 9, 100, 99)
                return original_read_identity(pid, proc_root)
            with mock.patch.object(SMOKE, "read_process_identity", side_effect=replace_parent_on_second_read):
                self.assertEqual(SMOKE.read_process_children(100, root, SMOKE.ProcessIdentity(100, 9, 100, 10)), ())
            self.assertGreaterEqual(parent_reads, 2)


class OwnedProcessTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.identities = {100: SMOKE.ProcessIdentity(100, 9, 100, 10)}
        self.children = {}
        self.process = mock.Mock(pid=100)
        self.process.poll.return_value = None
        self.process.wait.side_effect = self.finish
        self.popen = self.patches.enter_context(mock.patch.object(SMOKE.subprocess, "Popen", return_value=self.process))
        self.identity = self.patches.enter_context(mock.patch.object(SMOKE, "read_process_identity", side_effect=lambda pid, *_: self.identities.get(pid)))
        self.patches.enter_context(mock.patch.object(SMOKE, "read_process_children", side_effect=lambda pid, *_: self.children.get(pid, ())))
        self.killpg = self.patches.enter_context(mock.patch.object(SMOKE.os, "killpg"))
        self.kill = self.patches.enter_context(mock.patch.object(SMOKE.os, "kill"))
        self.patches.enter_context(mock.patch.object(SMOKE.time, "monotonic", return_value=10))

    def finish(self, **_):
        self.identities.clear()
        return 0

    def owned(self, deadline=100):
        return SMOKE.OwnedProcess(["synthetic-launcher"], {"PATH": SMOKE.SYSTEM_PATH}, self.root / "process.log", self.root, deadline)

    def test_starts_a_new_session_and_reaps_only_its_group(self):
        owned = self.owned().start()
        self.assertTrue(self.popen.call_args.kwargs["start_new_session"])
        self.assertEqual(owned.tracked_pids(), (100,))
        owned.close()
        self.killpg.assert_called_once_with(100, signal.SIGTERM)
        self.kill.assert_not_called()
        self.assertTrue(owned.handle.closed)
        owned.close()
        self.killpg.assert_called_once()

    def test_discovers_descendants_and_does_not_adopt_unrelated_pids(self):
        self.identities.update({101: SMOKE.ProcessIdentity(101, 100, 100, 11),
                                102: SMOKE.ProcessIdentity(102, 101, 102, 12),
                                999: SMOKE.ProcessIdentity(999, 77, 77, 99),
                                888: SMOKE.ProcessIdentity(888, 77, 77, 88)})
        self.children = {100: (101, 999), 101: (102,)}
        owned = self.owned().start()
        self.assertEqual(owned.tracked_pids(), (100, 101, 102))
        self.assertNotIn(888, [call.args[0] for call in self.identity.call_args_list])
        owned.close()
        self.killpg.assert_called_once_with(100, signal.SIGTERM)
        self.kill.assert_called_once_with(102, signal.SIGTERM)

    def test_reused_pid_is_never_signalled(self):
        owned = self.owned().start()
        self.identities[100] = SMOKE.ProcessIdentity(100, 9, 100, 999)
        self.assertEqual(owned.tracked_pids(), ())
        owned.close()
        self.killpg.assert_not_called()
        self.kill.assert_not_called()

    def test_detached_known_child_does_not_authorize_killing_its_old_group(self):
        self.identities[101] = SMOKE.ProcessIdentity(101, 100, 100, 11)
        self.children[100] = (101,)
        owned = self.owned().start()
        del self.identities[100]
        self.identities[101] = SMOKE.ProcessIdentity(101, 1, 101, 11)
        owned.close()
        self.killpg.assert_not_called()
        self.kill.assert_called_once_with(101, signal.SIGTERM)

    def test_timeout_escalates_to_kill_and_closes_log(self):
        owned = self.owned().start()
        calls = 0
        def wait(**_):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise subprocess.TimeoutExpired("synthetic-launcher", 3)
            return self.finish()
        self.process.wait.side_effect = wait
        owned.close()
        self.assertEqual(self.killpg.call_args_list, [mock.call(100, signal.SIGTERM), mock.call(100, signal.SIGKILL)])
        self.assertEqual(self.process.wait.call_count, 2)
        self.assertTrue(owned.handle.closed)

    def test_context_exception_cleans_up_its_process(self):
        owned = self.owned()
        with self.assertRaisesRegex(ValueError, "synthetic failure"):
            with owned:
                raise ValueError("synthetic failure")
        self.killpg.assert_called_once_with(100, signal.SIGTERM)
        self.assertTrue(owned.handle.closed)

    def test_deadline_expires_before_any_child_or_log_is_created(self):
        owned = self.owned(deadline=9)
        with self.assertRaisesRegex(RuntimeError, "budget"):
            owned.start()
        self.popen.assert_not_called()
        self.assertFalse((self.root / "process.log").exists())

    def test_popen_failure_closes_the_log_without_signalling_other_processes(self):
        self.popen.side_effect = OSError("synthetic exec failure")
        owned = self.owned()
        with self.assertRaises(OSError):
            owned.start()
        self.assertTrue(owned.handle.closed)
        self.killpg.assert_not_called()
        self.kill.assert_not_called()

    def test_proc_walk_failure_during_enter_still_terminates_and_reaps_the_child(self):
        owned = self.owned()
        first = True
        def fail_once(*_):
            nonlocal first
            if first:
                first = False
                raise OSError("synthetic owned process traversal failure")
            return ()
        with mock.patch.object(SMOKE, "read_process_children", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "traversal failure"):
                owned.__enter__()
        self.killpg.assert_called_once_with(100, signal.SIGTERM)
        self.process.wait.assert_called_once()
        self.assertTrue(owned.handle.closed)
        self.kill.assert_not_called()

    def test_live_child_without_a_verifiable_session_is_reaped_and_rejected(self):
        self.identities.clear()
        owned = self.owned()
        with self.assertRaisesRegex(RuntimeError, "session"):
            owned.start()
        self.process.terminate.assert_called_once()
        self.process.wait.assert_called_once()
        self.assertTrue(owned.handle.closed)
        self.killpg.assert_not_called()

    def test_a_short_completed_command_does_not_require_a_still_live_proc_entry(self):
        self.identities.clear()
        self.process.poll.return_value = 0
        owned = self.owned().start()
        owned.close()
        self.process.terminate.assert_not_called()
        self.killpg.assert_not_called()


class WindowEvidenceTests(unittest.TestCase):
    @staticmethod
    def wayland_trace(marker="@"):
        return [f" -> xdg_wm_base{marker}1.get_xdg_surface(new id xdg_surface{marker}2, wl_surface{marker}3)",
                f" -> xdg_surface{marker}2.get_toplevel(new id xdg_toplevel{marker}4)",
                f" -> xdg_surface{marker}2.ack_configure(1)",
                f" -> wl_surface{marker}3.attach(wl_buffer{marker}5, 0, 0)",
                f" -> wl_surface{marker}3.commit()"]

    def test_wayland_requires_handshake_and_buffer_commit_on_the_same_surface(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "wayland.log"
            for marker in ("@", "#"):
                with self.subTest(marker=marker):
                    log.write_text("\n".join(self.wayland_trace(marker)))
                    self.assertTrue(SMOKE.wayland_window_visible(log))
            log.write_text("Qt loaded wayland and QML; no window was created\n")
            self.assertFalse(SMOKE.wayland_window_visible(log))

    def test_wayland_incomplete_or_unrelated_protocol_objects_cannot_pass(self):
        complete = self.wayland_trace()
        invalid = {
            "no association": complete[1:],
            "no toplevel": complete[:1] + complete[2:],
            "no acknowledgement": complete[:2] + complete[3:],
            "empty initial commit": complete[:3] + [complete[-1]],
            "buffer for another surface": complete[:3] + [line.replace("wl_surface@3", "wl_surface@99") for line in complete[3:]],
            "commit for another surface": complete[:-1] + [complete[-1].replace("wl_surface@3", "wl_surface@99")],
            "another xdg surface": complete[:1] + [complete[1].replace("xdg_surface@2", "xdg_surface@99")] + complete[2:],
            "null buffer": complete[:3] + [complete[3].replace("wl_buffer@5", "nil"), complete[-1]],
            "commit only before attachment": complete[:3] + [complete[-1], complete[3]],
        }
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "wayland.log"
            for name, lines in invalid.items():
                with self.subTest(name=name):
                    log.write_text("\n".join(lines))
                    self.assertFalse(SMOKE.wayland_window_visible(log))

    def test_x11_requires_a_large_viewable_window_owned_by_the_launcher_pid(self):
        outputs = [subprocess.CompletedProcess([], 0, '  0x20 "launcher": 1280x840+0+0\n', ''),
                   subprocess.CompletedProcess([], 0, '_NET_WM_PID(CARDINAL) = 123\n', ''),
                   subprocess.CompletedProcess([], 0, 'Map State: IsViewable\n', '')]
        source = {"DISPLAY": ":99", "XAUTHORITY": "/private/auth", "LC_ALL": "zh_CN.UTF-8"}
        with mock.patch.object(SMOKE.subprocess, "run", side_effect=outputs) as run:
            self.assertTrue(SMOKE.x11_window_visible(source, 123, SMOKE.time.monotonic() + 10))
            self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
        self.assertEqual(source["LC_ALL"], "zh_CN.UTF-8")
        outputs[1] = subprocess.CompletedProcess([], 0, '_NET_WM_PID(CARDINAL) = 456\n', '')
        with mock.patch.object(SMOKE.subprocess, "run", side_effect=outputs[:2]) as run:
            self.assertFalse(SMOKE.x11_window_visible(source, 123, SMOKE.time.monotonic() + 10))
            self.assertEqual(run.call_count, 2)
        outputs[1] = subprocess.CompletedProcess([], 0, '_NET_WM_PID(CARDINAL) = 123\n', '')
        outputs[2] = subprocess.CompletedProcess([], 0, 'Map State: IsUnMapped\n', '')
        with mock.patch.object(SMOKE.subprocess, "run", side_effect=outputs):
            self.assertFalse(SMOKE.x11_window_visible(source, 123, SMOKE.time.monotonic() + 10))


if __name__ == "__main__":
    unittest.main()
