"""The device target and the reboot guards (C3). No adb: device.run is
replaced by a fake that records every command."""
import os
import subprocess
import unittest
from unittest import mock

import _path  # noqa: F401
from riverqa import device
from riverqa.core import SERIAL
from riverqa.device import Device, GuardError


class FakeRun:
    def __init__(self, props=None, state="device", fb_product="river"):
        self.props = {"ro.product.device": "river", "ro.serialno": SERIAL, **(props or {})}
        self.state, self.fb_product = state, fb_product
        self.cmds = []

    def __call__(self, cmd, timeout, input=None, text=True, env=None):
        self.cmds.append(cmd)
        out = ""
        if cmd[0] == "adb" and cmd[3:4] == ["get-state"]:
            out = self.state + "\n"
        elif cmd[0] == "adb" and cmd[3:4] == ["shell"] and cmd[4].startswith("getprop "):
            out = self.props.get(cmd[4].split()[1].strip("'"), "") + "\n"
        elif cmd[0] == "fastboot" and cmd[3:4] == ["getvar"]:
            var = cmd[4]
            val = {"product": self.fb_product, "unlocked": "yes"}.get(var, "")
            return subprocess.CompletedProcess(cmd, 0, "", f"{var}: {val}\n")
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def reboots(self):
        return [c for c in self.cmds if "reboot" in c]


class Target(unittest.TestCase):
    def test_only_the_test_phone(self):
        self.assertEqual(Device().serial, "ZY225DPT7F")
        with self.assertRaises(GuardError):
            Device("ABC123")

    def test_environment_cannot_change_the_target(self):
        with mock.patch.dict(os.environ, {"BENCH_DEVICE": "ABC123"}):
            with self.assertRaises(GuardError):
                Device()
            with self.assertRaises(GuardError):
                Device("ABC123")
        with mock.patch.dict(os.environ, {"BENCH_DEVICE": SERIAL}):
            self.assertEqual(Device().serial, SERIAL)

    def test_test_seam(self):
        device.TEST_SERIALS.add("FAKE1")
        try:
            self.assertEqual(Device("FAKE1").serial, "FAKE1")
        finally:
            device.TEST_SERIALS.discard("FAKE1")
        with self.assertRaises(GuardError):
            Device("FAKE1")


class RebootGuard(unittest.TestCase):
    def test_raw_reboot_is_refused(self):
        fake = FakeRun()
        with mock.patch.object(device, "run", fake):
            dev = Device()
            for args in (("reboot",), ("reboot", "bootloader"), ("reboot-bootloader",)):
                with self.assertRaises(GuardError):
                    dev.adb(*args)
                with self.assertRaises(GuardError):
                    dev.fastboot(*args)
        self.assertEqual(fake.reboots(), [])

    def test_raw_sysrq_is_refused(self):
        fake = FakeRun()
        with mock.patch.object(device, "run", fake):
            for cmd in ("echo b > /proc/sysrq-trigger", device.SYSRQ_REBOOT):
                with self.assertRaises(GuardError):
                    Device().adb("shell", cmd)
                with self.assertRaises(GuardError):
                    Device().sh(cmd)
        self.assertFalse(any("sysrq" in " ".join(c) for c in fake.cmds))

    def test_only_a_trigger_write_is_refused(self):
        """Review round 4 (Opus 6): reading the sysrq setting is fine."""
        fake = FakeRun()
        with mock.patch.object(device, "run", fake):
            Device().sh("cat /proc/sys/kernel/sysrq")
            Device().adb("shell", "getprop | grep -i sysrq")
            for cmd in ("echo c >/proc/sysrq-trigger", "printf b | tee /proc/sysrq-trigger"):
                with self.assertRaises(GuardError):
                    Device().sh(cmd)
        self.assertEqual(sum("sysrq" in " ".join(c) for c in fake.cmds), 2)

    def test_read_only_mentions_of_the_trigger_are_allowed(self):
        """R5-9: only writes to sysrq-trigger are refused."""
        fake = FakeRun()
        reads = ("ls -l /proc/sysrq-trigger", "test -e /proc/sysrq-trigger && echo yes",
                 "stat -c %a /proc/sysrq-trigger", "grep -c sysrq-trigger /init.rc")
        writes = ("echo b>/proc/sysrq-trigger", "echo b >> /proc/sysrq-trigger", "echo b 1>'/proc/sysrq-trigger'",
                  "dd if=/dev/zero of=/proc/sysrq-trigger bs=1 count=1", "printf b | tee -a /proc/sysrq-trigger",
                  "su -c 'echo b > /proc/sysrq-trigger'", "cp /data/local/tmp/b /proc/sysrq-trigger")
        with mock.patch.object(device, "run", fake):
            for cmd in reads:
                Device().sh(cmd)
            for cmd in writes:
                with self.assertRaises(GuardError, msg=cmd):
                    Device().sh(cmd)
        self.assertEqual(len(fake.cmds), len(reads))

    def test_copy_judged_on_its_destination(self):
        """R6-9: a cp/mv/ln/install with the trigger only as its source is
        allowed; one onto the trigger, or into /proc from a file named
        sysrq-trigger, is refused."""
        fake = FakeRun()
        allowed = ("cp /proc/sysrq-trigger /data/local/tmp/x", "cp -p /proc/sysrq-trigger /sdcard/",
                   "ln -s /proc/sysrq-trigger /data/local/tmp/t", "su -c 'cp /proc/sysrq-trigger /tmp/x'",
                   "cp -t /tmp /proc/sysrq-trigger")
        refused = ("cp /data/local/tmp/b /proc/sysrq-trigger", "install -m 200 b /proc/sysrq-trigger",
                   "su -c 'cp /tmp/b /proc/sysrq-trigger'", "cp /tmp/sysrq-trigger /proc",
                   "cp /tmp/sysrq-trigger /proc/", "cp -t /proc /tmp/sysrq-trigger",
                   "cd /proc && cp /tmp/sysrq-trigger .", "mv b /proc/sysrq-trigger",
                   "cp --target-directory=/proc sysrq-trigger", "cp 'unbalanced /proc/sysrq-trigger")
        with mock.patch.object(device, "run", fake):
            for cmd in allowed:
                Device().sh(cmd)
            for cmd in refused:
                with self.assertRaises(GuardError, msg=cmd):
                    Device().sh(cmd)
        self.assertEqual(len(fake.cmds), len(allowed), fake.cmds)

    def test_sysrq_reboot_after_the_guard_and_as_root(self):
        class Root(FakeRun):
            def __call__(self, cmd, timeout, input=None, text=True, env=None):
                if cmd[3:5] == ["shell", "id -u"]:
                    self.cmds.append(cmd)
                    return subprocess.CompletedProcess(cmd, 0, self.uid, "")
                return super().__call__(cmd, timeout, input, text, env)
        fake = Root()
        fake.uid = "0\n"
        with mock.patch.object(device, "run", fake):
            Device().reboot_sysrq()
        sysrq = [c for c in fake.cmds if "sysrq" in " ".join(c)]
        self.assertEqual(sysrq, [["adb", "-s", SERIAL, "shell", "sync; echo b > /proc/sysrq-trigger"]])
        idx = fake.cmds.index(sysrq[0])
        self.assertTrue(any("ro.product.device" in " ".join(c) for c in fake.cmds[:idx]))
        self.assertTrue(any("ro.serialno" in " ".join(c) for c in fake.cmds[:idx]))
        for bad in (Root(props={"ro.product.device": "lynx"}), Root()):
            bad.uid = "0\n" if bad.props["ro.product.device"] != "river" else "2000\n"
            with mock.patch.object(device, "run", bad):
                with self.assertRaises(GuardError):
                    Device().reboot_sysrq()
            self.assertFalse(any("sysrq" in " ".join(c) for c in bad.cmds))

    def test_adb_reboot_checks_the_product_first(self):
        fake = FakeRun(props={"ro.product.device": "lynx"})
        with mock.patch.object(device, "run", fake):
            with self.assertRaises(GuardError):
                Device().reboot_adb("bootloader")
        self.assertEqual(fake.reboots(), [])

    def test_adb_reboot_checks_the_serial(self):
        fake = FakeRun(props={"ro.serialno": "OTHER"})
        with mock.patch.object(device, "run", fake):
            with self.assertRaises(GuardError):
                Device().reboot_adb()
        self.assertEqual(fake.reboots(), [])

    def test_empty_serialno_is_refused(self):
        fake = FakeRun(props={"ro.serialno": ""})
        with mock.patch.object(device, "run", fake):
            with self.assertRaises(GuardError):
                Device().reboot_adb()
            with self.assertRaises(GuardError):
                Device().guard()
        self.assertEqual(fake.reboots(), [])

    def test_kmsg_checks_every_copy(self):
        seen = []

        def run(cmd, timeout, input=None, text=True, env=None):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 1 if len(seen) > 1 else 0, "", "")
        with mock.patch.object(device, "run", run):
            self.assertTrue(Device().kmsg("RQM:1:2"))
            self.assertFalse(Device().kmsg("RQM:1:2"))
        self.assertIn(" && ", seen[0][-1])

    def test_adb_reboot_after_the_guard(self):
        fake = FakeRun()
        with mock.patch.object(device, "run", fake):
            Device().reboot_adb("bootloader")
        self.assertEqual(fake.reboots(), [["adb", "-s", SERIAL, "reboot", "bootloader"]])
        idx = fake.cmds.index(fake.reboots()[0])
        self.assertTrue(any("ro.product.device" in " ".join(c) for c in fake.cmds[:idx]))

    def test_fastboot_reboot_checks_the_product_first(self):
        fake = FakeRun(fb_product="lynx")
        with mock.patch.object(device, "run", fake):
            with self.assertRaises(GuardError):
                Device().reboot_fastboot()
        self.assertEqual(fake.reboots(), [])
        fake = FakeRun()
        with mock.patch.object(device, "run", fake):
            Device().reboot_fastboot("bootloader")
        self.assertEqual(fake.reboots(), [["fastboot", "-s", SERIAL, "reboot", "bootloader"]])

    def test_read_carries_the_return_code(self):
        def failing(cmd, timeout, input=None, text=True, env=None):
            return subprocess.CompletedProcess(cmd, 1, "", "error: closed\n")
        with mock.patch.object(device, "run", failing):
            r = Device().read("dumpsys dropbox")
        self.assertFalse(r.ok)
        self.assertEqual(r.err.strip(), "error: closed")


class B64ScriptOnHost(unittest.TestCase):
    """b64_script run for real by the host's sh against a scratch directory
    (review round 3). No phone."""

    def read(self, glob, ls_lists=None, shell="sh"):
        """``ls_lists``: names a fake ``ls`` prints instead of the real
        listing, to stand for a directory that changes after it is listed."""
        import os
        import subprocess
        from riverqa.device import b64_script, parse_b64_dump
        env = dict(os.environ)
        if ls_lists is not None:
            bindir = f"{self.d}.bin"
            os.makedirs(bindir, exist_ok=True)
            with open(f"{bindir}/ls", "w") as f:
                f.write("#!/bin/sh\nprintf '%s\\n' " + " ".join(f"'{n}'" for n in ls_lists) + "\n")
            os.chmod(f"{bindir}/ls", 0o755)
            self.addCleanup(lambda: __import__("shutil").rmtree(bindir, ignore_errors=True))
            env["PATH"] = bindir + ":" + env["PATH"]
        r = subprocess.run([shell, "-c", b64_script(glob)], capture_output=True, text=True, timeout=30, env=env)
        return parse_b64_dump(r.returncode, r.stdout, r.stderr)

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, data):
        with open(f"{self.d}/{name}", "wb") as f:
            f.write(data)

    def test_reads_every_match(self):
        self.write("console-ramoops-0", b"\x00\xffabc\n" * 100)
        self.write("dmesg-ramoops-0", b"x")
        r = self.read(f"{self.d}/*")
        self.assertTrue(r.ok, r.why)
        self.assertEqual(sorted(p.rsplit("/", 1)[1] for p, _ in r.files), ["console-ramoops-0", "dmesg-ramoops-0"])
        self.assertEqual(self.read(f"{self.d}/console-ramoops*").data, b"\x00\xffabc\n" * 100)

    def test_missing_directory_fails(self):
        r = self.read(f"{self.d}/nope/*")
        self.assertFalse(r.ok)
        self.assertFalse(r.no_match)
        self.assertIn("missing or unreadable", r.why)

    def test_unreadable_directory_fails(self):
        import os
        if os.geteuid() == 0:
            self.skipTest("root reads any directory")
        os.mkdir(f"{self.d}/locked")
        os.chmod(f"{self.d}/locked", 0)
        self.addCleanup(os.chmod, f"{self.d}/locked", 0o700)
        r = self.read(f"{self.d}/locked/*")
        self.assertFalse(r.ok)
        self.assertFalse(r.no_match)

    def test_no_match_fails_but_is_marked(self):
        r = self.read(f"{self.d}/console-ramoops*")
        self.assertFalse(r.ok)
        self.assertTrue(r.no_match)
        r = self.read(f"{self.d}/*")
        self.assertFalse(r.ok)
        self.assertTrue(r.no_match)

    def test_file_that_vanishes_after_the_listing_fails(self):
        # A dangling link is listed by the glob but can't be read: the old
        # script's `[ -e ]` skipped it quietly and reported a clean read.
        import os
        self.write("console-ramoops-0", b"abc")
        os.symlink(f"{self.d}/gone", f"{self.d}/console-ramoops-1")
        r = self.read(f"{self.d}/console-ramoops*")
        self.assertFalse(r.ok)
        self.assertFalse(r.no_match)

    def test_one_enumeration_a_listed_file_that_vanished_fails(self):
        """Review round 4: the read set is exactly what ls listed. A name ls
        listed that is gone by the time it is read fails the whole read."""
        self.write("console-ramoops-0", b"abc")
        r = self.read(f"{self.d}/console-ramoops*", ls_lists=["console-ramoops-0", "console-ramoops-1"])
        self.assertFalse(r.ok)
        self.assertFalse(r.no_match)
        self.assertIn("vanished", r.why)

    def test_one_enumeration_reads_only_what_ls_listed(self):
        self.write("console-ramoops-0", b"abc")
        self.write("console-ramoops-1", b"late")          # appeared after the listing
        self.write("dmesg-ramoops-0", b"x")
        r = self.read(f"{self.d}/console-ramoops*", ls_lists=["console-ramoops-0", "dmesg-ramoops-0"])
        self.assertTrue(r.ok, r.why)
        self.assertEqual([p.rsplit("/", 1)[1] for p, _ in r.files], ["console-ramoops-0"])
        r = self.read(f"{self.d}/console-ramoops*", ls_lists=["dmesg-ramoops-0"])
        self.assertTrue(r.no_match)

    def test_other_shells(self):
        """Whichever of bash, dash and mksh the host has (the phone runs
        mksh; the phone check runs it there)."""
        import shutil
        self.write("console-ramoops-0", b"\x00abc")
        for sh in ("bash", "dash", "mksh"):
            if shutil.which(sh):
                with self.subTest(shell=sh):
                    r = self.read(f"{self.d}/console-ramoops*", shell=sh)
                    self.assertTrue(r.ok, r.why)
                    self.assertEqual(r.data, b"\x00abc")

    def test_wildcard_only_in_the_last_component(self):
        from riverqa.device import b64_script
        for bad in ("/sys/fs/*/x", "relative/*", "/sys/fs/pstore/$(id)"):
            with self.assertRaises(ValueError):
                b64_script(bad)


class ReadPstoreNoMatch(unittest.TestCase):
    def test_empty_pstore_is_clean_only_for_star(self):
        from fakes import FakeShell
        from riverqa import evidence
        dev = FakeShell({}, {})
        self.assertEqual(evidence.read_pstore(dev, "*"), (True, b"", ""))
        self.assertFalse(evidence.read_pstore(dev, "console-ramoops*")[0])
        # A missing directory (exit 5) is never an empty pstore.
        dev = FakeShell({}, {r"pstore": (5, [])})
        self.assertFalse(evidence.read_pstore(dev, "*")[0])


if __name__ == "__main__":
    unittest.main()
