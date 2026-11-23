import json
import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from riverqa import evidence as ev
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
             "boot_completed": "1", "selinux": "Enforcing"}
        m.update(meta)
        (d / "meta.json").write_text(json.dumps(m))
        (d / "dmesg.txt").write_text(DMESG)
        (d / "crash.txt").write_text("")
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


if __name__ == "__main__":
    unittest.main()
