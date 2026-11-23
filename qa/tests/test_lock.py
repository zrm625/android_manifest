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


if __name__ == "__main__":
    unittest.main()
