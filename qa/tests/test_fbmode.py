"""Review round 4, item 3: fastboot oem fb_mode as a durable obligation."""
import unittest
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from riverqa import fbmode, flashing, journal as jmod
from riverqa.results import FAIL


class Phone:
    """Where the phone is ("android", "fastboot" or "gone") and whether
    fb_mode is set. Every call is logged."""
    serial = "ZY225DPT7F"

    def __init__(self, where="android", fb=False, clear_fails=False):
        self.where, self.fb, self.clear_fails, self.calls = where, fb, clear_fails, []
        self.journal_at_set = None

    def in_fastboot(self):
        return self.where == "fastboot"

    def state(self):
        return "device" if self.where == "android" else ""

    def reboot_adb(self, target=None):
        self.calls.append(("reboot_adb", target))
        self.where = "fastboot" if target == "bootloader" or self.fb else "android"

    def wait_fastboot(self, seconds):
        if self.where != "fastboot":
            raise TimeoutError("no fastboot")

    def fb_mode_set(self):
        self.calls.append(("fb_mode_set",))
        self.journal_at_set = fbmode.pending()
        self.fb = True

    def fb_mode_clear(self):
        self.calls.append(("fb_mode_clear",))
        if self.clear_fails:
            raise RuntimeError("fastboot oem fb_mode_clear failed")
        self.fb = False

    def reboot_fastboot(self, target=None):
        self.calls.append(("reboot_fastboot", target))
        self.where = "fastboot" if target == "bootloader" or self.fb else "android"

    def wait_for_device(self, seconds):
        if self.where != "android":
            raise TimeoutError("no adb")

    def wait_boot_completed(self, seconds):
        self.calls.append(("boot_completed",))


class Obligation(unittest.TestCase):
    def test_arm_journals_before_the_set(self):
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p)
            self.assertIsNotNone(p.journal_at_set, "fb_mode_set ran before the obligation was durable")
            self.assertEqual(p.journal_at_set["where"], jmod.HOST)
            self.assertIsNotNone(fbmode.pending())
            fbmode.disarm(p)
            self.assertIsNone(fbmode.pending())
            self.assertFalse(p.fb)

    def test_killed_while_armed_then_recovered_from_android(self):
        """Armed, booted Android with fastboot continue, then the host died:
        recovery takes the phone to fastboot, clears fb_mode and boots."""
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p)
            p.where = "android"          # fastboot continue, then the harness was killed
            what = fbmode.recover(p, 10)
            self.assertIn("booted Android", what)
            self.assertEqual(p.calls[1:], [("reboot_adb", "bootloader"), ("fb_mode_clear",),
                                           ("reboot_fastboot", None), ("boot_completed",)])
            self.assertEqual((p.where, p.fb), ("android", False))
            self.assertIsNone(fbmode.pending())

    def test_killed_after_the_stop_recovers_from_fastboot(self):
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p)                # then sysrq, and the bootloader stopped: the phone sits in fastboot
            fbmode.recover(p, 10)
            self.assertEqual(p.calls[1:], [("fb_mode_clear",), ("reboot_fastboot", None), ("boot_completed",)])
            self.assertEqual((p.where, p.fb), ("android", False))
            self.assertIsNone(fbmode.pending())

    def test_phone_in_fastboot_with_nothing_pending(self):
        with bench_home():
            p = Phone("fastboot")
            self.assertIn("cleared fb_mode", fbmode.recover(p, 10))
            self.assertEqual(p.calls, [("fb_mode_clear",)])            # cleared, left in fastboot
            p = Phone("fastboot")
            fbmode.recover(p, 10, boot=True)                            # calibration boots it
            self.assertEqual(p.where, "android")

    def test_nothing_to_do(self):
        with bench_home():
            p = Phone("android")
            self.assertIsNone(fbmode.recover(p, 10))
            self.assertEqual(p.calls, [])

    def test_failed_clear_keeps_the_obligation(self):
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p)
            p.clear_fails = True
            with self.assertRaises(RuntimeError):
                fbmode.recover(p, 10)
            self.assertIsNotNone(fbmode.pending())

    def test_unreachable_phone_keeps_the_obligation(self):
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p)
            p.where = "gone"
            with self.assertRaises(RuntimeError):
                fbmode.recover(p, 10)
            self.assertIsNotNone(fbmode.pending())


class Callers(unittest.TestCase):
    def test_to_fastboot_clears_fb_mode(self):
        """Rescue and rollback reach fastboot through to_fastboot."""
        with bench_home():
            p = Phone("fastboot", fb=True)
            fbmode.arm(p)
            flashing.to_fastboot(p, 5)
            self.assertFalse(p.fb)
            self.assertIsNone(fbmode.pending())

    def test_flash_preflight_recovers(self):
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p)
            with mock.patch.object(flashing, "Device", lambda *a: p), \
                    mock.patch.object(flashing, "show_status", return_value=0), \
                    mock.patch.object(flashing, "load_state", return_value=None):
                flashing.main(["--rollback"])
            self.assertIsNone(fbmode.pending())
            self.assertEqual(p.where, "android")

    def test_calibration_starts_from_fastboot(self):
        from riverqa import calibrate
        with bench_home():
            p = Phone("fastboot")
            with mock.patch.object(calibrate, "Device", lambda *a: p), \
                    mock.patch.object(p, "state", create=True, side_effect=lambda: "device" if p.where == "android"
                                      else ""):
                p.guard = mock.Mock(side_effect=RuntimeError("stop here"))
                with self.assertRaises(RuntimeError):
                    calibrate.run(1)
            self.assertEqual(p.calls[:3], [("fb_mode_clear",), ("reboot_fastboot", None), ("boot_completed",)])
            p.guard.assert_called_once()

    def test_phone_qa_preflight_recovers_then_stops(self):
        from riverqa import qa
        from riverqa.results import PASS, Result
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p)

            class R:
                dev, results = p, {}
                journal = fbmode.journal()

                def add(self, r):
                    self.results[r.id] = r

                def guard(self):
                    raise AssertionError("the guard must not run before fb_mode recovery")
            run = R()
            ok = qa.preflight(run, Result("preflight.lock", PASS, "held"))
            self.assertFalse(ok)
            self.assertEqual(run.results["preflight.journal"].state, FAIL)
            self.assertIn("fb_mode recovery", run.results["preflight.journal"].summary)
            self.assertIsNone(fbmode.pending())
            self.assertEqual(p.where, "android")

    def test_device_bench_recovers_and_stops_a_batch(self):
        from riverqa import bench
        for batch, want in (("b1", 1), (None, "guarded")):
            with bench_home():
                p = Phone("android")
                fbmode.arm(p)
                p.guard = mock.Mock(side_effect=RuntimeError("guarded"))
                a = mock.Mock(batch=batch, flash=False)
                with mock.patch.object(bench, "Device", lambda *x: p):
                    try:
                        got = bench.run(a)
                    except RuntimeError as e:
                        got = str(e)
                self.assertEqual(got, want)
                self.assertIsNone(fbmode.pending())
                self.assertFalse(p.fb)

    def test_restore_handler(self):
        from riverqa import restore
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p)
            p.where = "android"

            class R:
                dev = p
                journal = fbmode.journal()
            run = R()
            ok, msg = restore.restore_entry(run, fbmode.pending(run.journal))
            self.assertTrue(ok, msg)
            self.assertIsNone(fbmode.pending())


if __name__ == "__main__":
    unittest.main()
