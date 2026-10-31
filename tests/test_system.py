"""rawi_core.system: the adapter for each OS, tested on any OS.

The point of these tests is the bug that started the adapter: Windows-only process flags
reaching subprocess on macOS. Popen is mocked, so nothing is actually started.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _helpers  # noqa: F401
from rawi_core import system


class TestSelection(unittest.TestCase):
    def test_each_platform_gets_its_adapter(self):
        self.assertIsInstance(system.for_platform("win32"), system.WindowsPlatform)
        self.assertIsInstance(system.for_platform("darwin"), system.MacPlatform)
        self.assertIsInstance(system.for_platform("linux"), system.Platform)

    def test_current_is_cached(self):
        self.assertIs(system.current(), system.current())

    def test_paste_and_undo_chords(self):
        self.assertEqual(system.for_platform("win32").paste_chord(), ("ctrl", "v"))
        self.assertEqual(system.for_platform("darwin").paste_chord(), ("command", "v"))
        self.assertEqual(system.for_platform("darwin").undo_chord(), ("command", "z"))
        self.assertEqual(system.for_platform("linux").undo_chord(), ("ctrl", "z"))

    def test_only_macos_needs_the_main_thread_for_tk(self):
        self.assertTrue(system.for_platform("darwin").tk_needs_main_thread)
        self.assertFalse(system.for_platform("win32").tk_needs_main_thread)
        self.assertFalse(system.for_platform("linux").tk_needs_main_thread)

    def test_fonts_scale_on_macos(self):
        self.assertEqual(system.for_platform("win32").font(9), ("Segoe UI", 9))
        self.assertEqual(system.for_platform("win32").font(9, bold=True), ("Segoe UI Semibold", 9))
        self.assertEqual(system.for_platform("darwin").font(9), ("Helvetica Neue", 12))


class TestSpawn(unittest.TestCase):
    """What each adapter hands to subprocess.Popen."""

    def spawn_kwargs(self, plat_name, **kw):
        with mock.patch.object(system.subprocess, "Popen") as popen:
            system.for_platform(plat_name).spawn(["python", "x.py"], **kw)
        return popen.call_args.kwargs

    def test_posix_never_passes_windows_flags(self):
        for name in ("darwin", "linux"):
            for kw in ({}, {"detached": True}, {"new_group": True}):
                self.assertNotIn("creationflags", self.spawn_kwargs(name, **kw), (name, kw))

    def test_posix_detached_starts_a_new_session(self):
        self.assertTrue(self.spawn_kwargs("darwin", detached=True)["start_new_session"])
        self.assertNotIn("start_new_session", self.spawn_kwargs("darwin"))

    def test_windows_flags_match_the_original_calls(self):
        self.assertEqual(self.spawn_kwargs("win32")["creationflags"], system.CREATE_NO_WINDOW)
        self.assertEqual(self.spawn_kwargs("win32", detached=True, new_group=True)["creationflags"],
                         system.DETACHED_PROCESS | system.CREATE_NEW_PROCESS_GROUP)
        self.assertEqual(self.spawn_kwargs("win32", new_group=True)["creationflags"],
                         system.CREATE_NO_WINDOW | system.CREATE_NEW_PROCESS_GROUP)

    def test_log_path_receives_output_and_is_closed_in_the_parent(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "child.log")
            p = system.current().spawn([sys.executable, "-c", "print('hello from child')"],
                                       log_path=log)
            p.wait(timeout=30)
            with open(log, encoding="utf-8") as f:
                self.assertIn("hello from child", f.read())

    def test_a_real_spawn_works_on_this_machine(self):
        p = system.current().spawn([sys.executable, "-c", "pass"], detached=True, new_group=True)
        self.assertEqual(p.wait(timeout=30), 0)


class TestProcesses(unittest.TestCase):
    def test_this_process_is_alive(self):
        self.assertTrue(system.current().pid_alive(os.getpid()))

    def test_nonsense_pids_are_not_alive(self):
        plat = system.current()
        for pid in (0, -1, "not a pid", None):
            self.assertFalse(plat.pid_alive(pid), pid)

    def test_a_finished_process_is_not_alive(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait(timeout=30)
        self.assertFalse(system.current().pid_alive(p.pid))

    @unittest.skipIf(sys.platform.startswith("win"), "zombies are a POSIX thing")
    def test_an_exited_child_nobody_waited_for_is_not_alive(self):
        # the hub's situation: it starts the daemon, never waits, then stops it
        import signal
        import time
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        os.kill(p.pid, signal.SIGTERM)
        deadline = time.time() + 10
        while system.current().pid_alive(p.pid) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(system.current().pid_alive(p.pid))

    def test_kill_stops_a_process(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(system.current().kill(p.pid))
            p.wait(timeout=10)
        finally:
            if p.poll() is None:
                p.kill()

    def test_windows_background_python_prefers_pythonw(self):
        with mock.patch.object(system.os.path, "exists", return_value=True):
            self.assertTrue(system.for_platform("win32").background_python().endswith("pythonw.exe"))
        with mock.patch.object(system.os.path, "exists", return_value=False):
            self.assertEqual(system.for_platform("win32").background_python(), sys.executable)

    def test_posix_background_python_is_this_interpreter(self):
        self.assertEqual(system.for_platform("darwin").background_python(), sys.executable)


class TestQueries(unittest.TestCase):
    def test_mac_active_app_reads_osascript(self):
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout="Slack\n")
        with mock.patch.object(system.subprocess, "run", return_value=done) as run:
            self.assertEqual(system.for_platform("darwin").active_app(), "Slack")
        self.assertEqual(run.call_args.args[0][0], "osascript")

    def test_active_app_failure_is_empty_not_an_error(self):
        with mock.patch.object(system.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(system.for_platform("linux").active_app(), "")
            self.assertEqual(system.for_platform("darwin").active_app(), "")

    def test_cleaner_command_needs_a_launcher_next_to_rawi(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(system.for_platform("darwin").cleaner_command(d))
            open(os.path.join(d, "cleaner-server.sh"), "w").close()
            open(os.path.join(d, "cleaner-server.bat"), "w").close()
            self.assertEqual(system.for_platform("darwin").cleaner_command(d)[0], "/bin/sh")
            self.assertEqual(system.for_platform("win32").cleaner_command(d)[:2], ["cmd.exe", "/c"])

    def test_input_permission_is_a_tri_state(self):
        self.assertIsNone(system.for_platform("linux").input_permission())
        self.assertIn(system.current().input_permission(), (True, False, None))

    def test_windows_only_calls_fail_open_elsewhere(self):
        win = system.for_platform("win32")
        if sys.platform.startswith("win"):
            self.skipTest("these are the real calls on Windows")
        self.assertFalse(win.focus_window("Rawi"))
        self.assertIsInstance(win.active_app(), str)
        win.beep(440, 10)                       # winsound is missing: must not raise
        win.make_passive(object(), click_through=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
