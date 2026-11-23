"""The shared device lock (E): exclusive, token reuse only when the token
matches and someone holds the lock."""
import os
import unittest

import _path  # noqa: F401
from benchhome import bench_home
from riverqa.lock import DeviceLock, LockError


class Lock(unittest.TestCase):
    def test_exclusive(self):
        with bench_home():
            a = DeviceLock().acquire()
            token = os.environ.pop("BENCH_LOCK_TOKEN")     # a second, unrelated process
            try:
                with self.assertRaises(LockError):
                    DeviceLock().acquire()
            finally:
                os.environ["BENCH_LOCK_TOKEN"] = token
                a.release()
            self.assertNotIn("BENCH_LOCK_TOKEN", os.environ)
            DeviceLock().acquire().release()             # free again

    def test_token_mode_reuses_a_held_lock(self):
        with bench_home():
            owner = DeviceLock().acquire()
            try:
                child = DeviceLock().acquire()            # BENCH_LOCK_TOKEN is exported
                self.assertFalse(child.owned)
                self.assertEqual(child.token, owner.token)
                child.release()                           # a child never releases the owner's lock
                with self.assertRaises(LockError):
                    os.environ.pop("BENCH_LOCK_TOKEN")
                    try:
                        DeviceLock().acquire()
                    finally:
                        os.environ["BENCH_LOCK_TOKEN"] = owner.token
            finally:
                owner.release()

    def test_wrong_token_is_refused(self):
        with bench_home():
            owner = DeviceLock().acquire()
            try:
                os.environ["BENCH_LOCK_TOKEN"] = "not-the-token"
                with self.assertRaises(LockError):
                    DeviceLock().acquire()
            finally:
                os.environ["BENCH_LOCK_TOKEN"] = owner.token
                owner.release()

    def test_token_without_a_holder_is_refused(self):
        with bench_home() as h:
            (h / ".device-lock").write_text("stale\n")
            os.environ["BENCH_LOCK_TOKEN"] = "stale"
            with self.assertRaises(LockError):
                DeviceLock().acquire()
            os.environ.pop("BENCH_LOCK_TOKEN")


class DurableWrite(unittest.TestCase):
    def test_parent_directory_is_fsynced_after_the_rename(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        from riverqa import core
        calls = []
        real_replace, real_fsync = os.replace, os.fsync

        def replace(a, b):
            calls.append(("replace", b))
            return real_replace(a, b)

        def fsync(fd):
            calls.append(("fsync-dir" if os.path.isdir(f"/proc/self/fd/{fd}") else "fsync-file", fd))
            return real_fsync(fd)

        with tempfile.TemporaryDirectory() as t, mock.patch.object(core.os, "replace", replace), \
                mock.patch.object(core.os, "fsync", fsync):
            core.write_json(Path(t) / "x.json", {"a": 1})
            self.assertEqual(core.read_json(Path(t) / "x.json"), {"a": 1})
        kinds = [c[0] for c in calls]
        self.assertEqual(kinds, ["fsync-file", "replace", "fsync-dir"])


class BuildLock(unittest.TestCase):
    def test_no_device_job_while_a_build_runs(self):
        """R5-13: a build holding $BENCH_HOME/.lock stops a device job from
        starting, in owned and token mode."""
        import subprocess
        import sys
        from riverqa import lock
        with bench_home() as h:
            owner = DeviceLock().acquire()                       # the runner, before the build
            child = subprocess.Popen([sys.executable, "-I", "-c",
                                      "import fcntl, sys, time; f = open(sys.argv[1], 'w'); "
                                      "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(30)",
                                      str(h / ".lock")], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "held")
                self.assertTrue(lock.build_running())
                with self.assertRaises(LockError) as cm:
                    DeviceLock().acquire()                       # token mode: a child of the runner
                self.assertIn("a build holds", str(cm.exception))
                os.environ.pop("BENCH_LOCK_TOKEN")
                owner.release()
                with self.assertRaises(LockError):
                    DeviceLock().acquire()                       # owned mode
            finally:
                child.kill()
                child.wait()
                child.stdout.close()                            # R6-7
                os.environ.pop("BENCH_LOCK_TOKEN", None)
            self.assertFalse(lock.build_running())
            DeviceLock().acquire().release()                    # the build finished

    def test_concurrent_probes_are_not_a_build(self):
        """R6-5: another job probing the build lock (a shared hold) is not a
        build; an exclusive hold is."""
        import fcntl
        from riverqa import lock
        with bench_home() as h:
            fd = os.open(h / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH)
                self.assertFalse(lock.build_running())
                DeviceLock().acquire().release()
            finally:
                os.close(fd)

    def test_merge_started_and_promote_run_during_a_build(self):
        """R6-5: host-only flash steps skip the build-lock probe; a dev
        flash still refuses."""
        import fcntl
        import io
        from contextlib import redirect_stderr
        from riverqa import flashing
        with bench_home() as h:
            fd = os.open(h / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                for argv in (["--merge-started", "nope"], ["--promote", "nope"], ["stripped"]):
                    with self.subTest(argv=argv), redirect_stderr(io.StringIO()) as err:
                        flashing.main(argv)
                    if argv[0] == "stripped":
                        self.assertIn("a build holds", err.getvalue())
                    else:
                        self.assertNotIn("a build holds", err.getvalue())
                        self.assertIn("nope", err.getvalue())
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
