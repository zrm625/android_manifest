import json
import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from fakes import DROPBOX_EMPTY, FakeShell
from riverqa import evidence as ev
from riverqa import features
from riverqa.results import FAIL, PASS, UNVERIFIED

DMESG = """[ 10.0] init: Service 'vendor.qti.hardware.foo' (pid 100) received signal 9
[ 11.0] init: Processed ctl.stop for 'idmap2d' from pid: 1
[ 11.1] init: Service 'idmap2d' (pid 200) exited with status 0
[ 12.0] init: Service 'idmap2d' (pid 201) exited with status 0
"""


class Parse(unittest.TestCase):
    def test_sigkill_and_ctl_stop_are_not_problems(self):
        self.assertEqual(ev.service_problems(DMESG), ([], []))

    def test_lazy_service_clean_exits_are_not_a_loop(self):
        d = "\n".join(f"[ {i}.0] init: Service 'artd' (pid {i}) exited with status 0 oneshot service took 1.0 seconds"
                      for i in range(5))
        self.assertEqual(ev.service_problems(d), ([], []))

    def test_abnormal_exits_loop(self):
        d = "\n".join(f"[ {i}.0] init: Service 'foo' (pid {i}) exited with status 1" for i in range(3))
        self.assertEqual(ev.service_problems(d), ([], ["foo x3"]))

    def test_crash_signal_and_loop(self):
        d = DMESG + "\n".join(f"[ 2{i}.0] init: Service 'cameraserver' (pid 3{i}) received signal 6"
                              for i in range(3))
        crashed, loops = ev.service_problems(d)
        self.assertEqual(crashed, ["cameraserver (signal 6)"])
        self.assertEqual(loops, ["cameraserver x3"])

    def test_crash_processes(self):
        log = "--------- beginning of crash\nE AndroidRuntime: FATAL EXCEPTION: main\nE AndroidRuntime: Process: com.x.y, PID: 1\n"
        self.assertEqual(ev.crash_processes(log), ["com.x.y"])
        self.assertEqual(ev.crash_processes(""), [])


class Judge(unittest.TestCase):
    def make(self, **meta):
        d = Path(tempfile.mkdtemp())
        m = {"boot_id_at_collection": "B", "boot_id_at_completion": "B", "boot_id_after_collection": "B",
             "boot_completed": "1", "selinux": "Enforcing",
             "captured": {f: {"ok": True, "why": ""} for f in ev.CAPTURED_FILES}}
        m.update(meta)
        (d / "meta.json").write_text(json.dumps(m))
        (d / "dmesg.txt").write_text(DMESG)
        (d / "crash.txt").write_text("")
        (d / "dropbox.txt").write_text(DROPBOX_EMPTY)
        (d / "pstore.bin").write_bytes(b"")
        return d

    def test_clean(self):
        self.assertEqual(ev.judge(self.make())[0], PASS)

    def test_boot_id_mismatch_is_unverified(self):
        self.assertEqual(ev.judge(self.make(boot_id_at_collection="C"))[0], UNVERIFIED)
        self.assertEqual(ev.judge(self.make(boot_id_after_collection="C"))[0], UNVERIFIED)

    def test_missing_is_unverified(self):
        self.assertEqual(ev.judge(Path(tempfile.mkdtemp()))[0], UNVERIFIED)

    def test_planted_crash_fails(self):
        d = self.make()
        (d / "crash.txt").write_text("E AndroidRuntime: Process: com.android.systemui, PID: 9\n")
        self.assertEqual(ev.judge(d)[0], FAIL)

    def test_permissive_fails(self):
        self.assertEqual(ev.judge(self.make(selinux="Permissive"))[0], FAIL)
        self.assertEqual(ev.judge(self.make(boot_completed=""))[0], FAIL)


class Reads(unittest.TestCase):
    """A failed or incomplete diagnostic read is UNVERIFIED, never PASS (A2)."""

    def test_format_checks(self):
        self.assertTrue(ev.validate("dropbox", 0, DROPBOX_EMPTY)[0])
        self.assertFalse(ev.validate("dropbox", 0, "")[0])                 # stream exited early
        self.assertFalse(ev.validate("dropbox", 0, "2026-11-22 18:44:04 SYSTEM_BOOT")[0])  # header lost
        self.assertFalse(ev.validate("dropbox", 1, DROPBOX_EMPTY)[0])
        self.assertTrue(ev.validate("dmesg", 0, "[ 1.0] x")[0])
        self.assertFalse(ev.validate("dmesg", 0, "  \n")[0])
        self.assertTrue(ev.validate("features", 0, "feature:android.hardware.wifi\n")[0])
        self.assertFalse(ev.validate("features", 0, "")[0])
        self.assertTrue(ev.validate("crash", 0, "")[0])                    # empty crash buffer, read fine
        self.assertFalse(ev.validate("crash", 255, "")[0])
        self.assertFalse(ev.validate("tombstones", 1, "")[0])
        self.assertTrue(ev.validate("pstore", 0, b"x")[0])

    def fake(self, **over):
        replies = {r"^cat /proc/sys/kernel/random/boot_id": (0, "B\n1\nEnforcing\nreboot\nreboot\n12.0 3.0\n"),
                   r"^dmesg$": (0, "[ 1.0] init: ok\n"), r"^logcat -b crash -d$": (0, ""),
                   r"^dumpsys dropbox$": (0, DROPBOX_EMPTY), r"^ls /sys/fs/pstore": (0, "console-ramoops-0\n")}
        replies.update(over)
        return FakeShell(replies, {r"pstore": (0, b"[ 1.0] log\n")})

    def collect(self, dev):
        d = Path(tempfile.mkdtemp())
        ev.collect(dev, d, expected_boot_id="B", context={})
        return d

    def test_empty_crash_buffer_read_successfully_passes(self):
        st, msg, _ = ev.judge(self.collect(self.fake()))
        self.assertEqual(st, PASS, msg)

    def test_failed_read_is_unverified(self):
        for key in (r"^logcat -b crash -d$", r"^dmesg$", r"^dumpsys dropbox$"):
            st, msg, _ = ev.judge(self.collect(self.fake(**{key: (1, "")})))
            self.assertEqual(st, UNVERIFIED, key)
            self.assertIn("not captured cleanly", msg)

    def test_failed_or_partial_pstore_read_is_unverified(self):
        good = "__FILE__ 11 /sys/fs/pstore/console-ramoops-0\nWyAxLjBdIGxvZwo=\n__EOF__\n"
        for files in ((1, [("/sys/fs/pstore/console-ramoops-0", b"[ 1.0] log\n")]),   # remote failure
                      (0, good.replace("__FILE__ 11", "__FILE__ 4096")),             # partial read
                      (0, good),                                                     # never finished
                      (0, good + "__RC__ 2\n"),                                      # a file went missing
                      (0, good.replace("__EOF__\n", "")),                            # cut off mid-file
                      (0, b"")):                                                     # no console-ramoops
            dev = self.fake()
            dev.bytes_replies = [(r"pstore", files)]
            st, msg, _ = ev.judge(self.collect(dev))
            self.assertEqual(st, UNVERIFIED, files)
            self.assertIn("pstore.bin", msg)
        dev = self.fake()
        dev.bytes_replies = [(r"pstore", (0, good + "__RC__ 1\n"))]
        self.assertEqual(ev.judge(self.collect(dev))[0], PASS)

    def test_capture_record_needs_the_files(self):
        d = self.collect(self.fake())
        self.assertEqual(ev.judge(d)[0], PASS)
        for f in ("pstore.bin", "dropbox.txt"):
            d = self.collect(self.fake())
            (d / f).unlink()
            st, msg, _ = ev.judge(d)
            self.assertEqual(st, UNVERIFIED, f)
            self.assertIn(f, msg)

    def test_stream_that_exits_early_is_unverified(self):
        # dumpsys dropbox cut off before its header; dmesg empty.
        st, _, _ = ev.judge(self.collect(self.fake(**{r"^dumpsys dropbox$": (0, "")})))
        self.assertEqual(st, UNVERIFIED)
        st, _, _ = ev.judge(self.collect(self.fake(**{r"^dmesg$": (0, "")})))
        self.assertEqual(st, UNVERIFIED)

    def test_old_evidence_without_capture_record_is_unverified(self):
        d = Path(tempfile.mkdtemp())
        (d / "meta.json").write_text(json.dumps({"boot_id_at_collection": "B", "boot_id_at_completion": "B",
                                                 "boot_id_after_collection": "B", "boot_completed": "1",
                                                 "selinux": "Enforcing"}))
        (d / "dmesg.txt").write_text(DMESG)
        (d / "crash.txt").write_text("")
        self.assertEqual(ev.judge(d)[0], UNVERIFIED)

    def test_pstore_read(self):
        self.assertFalse(ev.read_pstore(FakeShell({}, {r"pstore": (1, [])}), "*")[0])
        self.assertEqual(ev.read_pstore(FakeShell({}, {r"pstore": (0, [])}), "*"), (True, b"", ""))
        two = [("/sys/fs/pstore/console-ramoops-0", b"\x00\xffab"), ("/sys/fs/pstore/pmsg-ramoops-0", b"cd")]
        self.assertEqual(ev.read_pstore(FakeShell({}, {r"pstore": (0, two)}), "*"), (True, b"\x00\xffabcd", ""))
        self.assertFalse(ev.read_pstore(FakeShell({}, {r"pstore": (0, [])}), "console-ramoops*")[0])

    def test_unsafe_glob_is_refused(self):
        from riverqa.device import b64_script
        with self.assertRaises(ValueError):
            b64_script("/sys/fs/pstore/*; reboot")


class FinalHealth(unittest.TestCase):
    SNAP = {"tombstones": [], "pstore": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "crash": "--------- beginning of crash\nE AndroidRuntime: Process: com.old, PID: 1\n"}

    def parts(self, reads, log="", dead=None):
        d = Path(tempfile.mkdtemp())
        (d / "logcat.txt").write_text(log)
        return features.final_health_parts(self.SNAP, reads, d / "logcat.txt", dead)

    def reads(self, **over):
        r = {"tombstones": (True, "", ""), "pstore": (True, b"", ""),
             "crash": (True, self.SNAP["crash"], "")}
        r.update(over)
        return r

    def test_clean(self):
        self.assertEqual({p[0] for p in self.parts(self.reads())}, {PASS})

    def test_dead_stream_is_unverified(self):
        states = [p[0] for p in self.parts(self.reads(), dead="streamed logcat died before system.video")]
        self.assertIn(UNVERIFIED, states)
        self.assertNotIn(FAIL, states)

    def test_failed_reads_are_unverified(self):
        for key in ("tombstones", "pstore", "crash"):
            states = [p[0] for p in self.parts(self.reads(**{key: (False, "", "exit 1")}))]
            self.assertIn(UNVERIFIED, states, key)

    def test_new_crash_in_buffer_fails(self):
        crash = self.SNAP["crash"] + "E AndroidRuntime: Process: com.new, PID: 2\n"
        parts = self.parts(self.reads(crash=(True, crash, "")))
        self.assertIn((FAIL, "crash buffer: new crashes com.new"), parts)


if __name__ == "__main__":
    unittest.main()
