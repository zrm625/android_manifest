"""Round-2 council findings on the batch runner (Sol SB*, SM*; Gemini GM2-*;
Opus minors), each through its real caller."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from riverbatch import hold, paths, ports
from riverqa import children


def _alive(pid: int) -> bool:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return text[text.rfind(")") + 2:].split()[0] != "Z"


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class SessionEscape(unittest.TestCase):
    """Sol SB2: a descendant in a process group of its own (GNU timeout)
    is still the child's; the holder ends it and the interlock holds until
    it is gone."""

    ESCAPE = ("import os, sys, time\n"
              "os.setpgid(0, 0)\n"
              "open(sys.argv[1], 'w').write(f'{os.getpid()} {os.getpgid(0)} {os.getsid(0)}')\n"
              "time.sleep(600)\n")

    def test_holder_deadline_ends_a_descendant_in_its_own_group(self):
        with bench_home() as h, tempfile.TemporaryDirectory() as t:
            pidf = Path(t) / "pid"
            script = Path(t) / "escape.py"
            script.write_text(self.ESCAPE)
            tools = ports.Tools()
            pid = None
            try:
                r = tools.run(["sh", "-c", f'"{sys.executable}" -I "{script}" "{pidf}" </dev/null >/dev/null 2>&1 & wait'], None,
                              timeout=3)
                self.assertTrue(r.timed_out)
                pid, pgid, sid = map(int, pidf.read_text().split())
                self.assertNotEqual(pgid, sid)                       # it did leave the tool's group
                for _ in range(50):
                    if not _alive(pid):
                        break
                    time.sleep(0.1)
                self.assertFalse(_alive(pid), "a descendant in its own group outlived the holder")
                self.assertEqual(children.live_children(h), [])
            finally:
                if pid:
                    _kill(pid)

    def test_record_stays_live_while_the_session_lives(self):
        """The leader and its group are gone; a process of its session
        is not: the record is live and blocks every other session."""
        with tempfile.TemporaryDirectory() as t:
            proc = Path(t) / "proc"
            (proc / "sys/kernel/random").mkdir(parents=True)
            (proc / "sys/kernel/random/boot_id").write_text("boot\n")
            (proc / "200").mkdir()
            # pid 200: state S, ppid 1, pgrp 200, session 100
            (proc / "200/stat").write_text("200 (timeout) S 1 200 100 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 77 0\n")
            home = Path(t) / "home"
            d = home / "runner" / "children"
            d.mkdir(parents=True)
            (d / "100.json").write_text(json.dumps({"cmd": ["phone-setup"], "pid": 100, "pgid": 100, "sid": 100,
                                                    "boot_id": "boot", "start_time": "5"}))
            self.assertEqual(len(children.blocking(caller_sid=999, home=home, proc=proc)), 1)
            self.assertTrue((d / "100.json").exists())
            (proc / "200/stat").write_text("200 (timeout) Z 1 200 100 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 77 0\n")
            self.assertEqual(children.blocking(caller_sid=999, home=home, proc=proc), [])

    def test_holder_keeps_the_record_while_the_session_survives(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "c"
            saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGALRM)}
            try:
                with mock.patch.object(hold, "GRACE", 0.2), \
                        mock.patch.object(hold, "group_alive", return_value=True), \
                        mock.patch.object(hold, "signal_session"):
                    rc = hold.main([str(d), "--", "true"])
            finally:
                for s, f in saved.items():
                    signal.signal(s, f)
            self.assertEqual(rc, 0)
            self.assertEqual(len(list(d.glob("*.json"))), 1, "the record went while the session was alive")

    def test_phone_setup_adb_stays_in_the_tools_group(self):
        """Through the real phone-setup, run by the real holder: a hung adb
        (a stub on PATH) runs in phone-setup's own group and session, and
        the holder's deadline ends it."""
        with bench_home() as h, tempfile.TemporaryDirectory() as t:
            m = Path(t) / "top/.repo/manifests"
            shutil.copytree(paths.SCRIPTS, m / "scripts", symlinks=True)
            shutil.copytree(paths.SCRIPTS.parent / "qa/lib", m / "qa/lib",
                            ignore=shutil.ignore_patterns("__pycache__"))
            bindir = Path(t) / "bin"
            bindir.mkdir()
            pidf = Path(t) / "adb.pid"
            (bindir / "adb").write_text(
                f"#!{sys.executable} -I\n"
                "import os, time\n"
                f"open({str(pidf)!r}, 'w').write(f'{{os.getpid()}} {{os.getpgid(0)}} {{os.getsid(0)}}')\n"
                "time.sleep(600)\n")
            (bindir / "adb").chmod(0o755)
            pid = None
            try:
                with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ.get('PATH', '')}"}):
                    r = ports.Tools().run([str(m / "scripts/phone-setup")], None, timeout=4)
                self.assertTrue(r.timed_out)
                pid, pgid, sid = map(int, pidf.read_text().split())
                self.assertEqual(pgid, sid, "adb runs in a process group of its own (timeout without --foreground)")
                for _ in range(50):
                    if not _alive(pid):
                        break
                    time.sleep(0.1)
                self.assertFalse(_alive(pid))
                self.assertEqual(children.live_children(h), [])
            finally:
                if pid:
                    _kill(pid)


if __name__ == "__main__":
    unittest.main()
