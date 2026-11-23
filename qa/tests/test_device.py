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


if __name__ == "__main__":
    unittest.main()
