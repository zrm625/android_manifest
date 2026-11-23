"""Review round 4, item 3: fastboot oem fb_mode as a durable obligation."""
import unittest
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from fakes import Boots
from riverqa import fbmode, flashing, journal as jmod
from riverqa.results import FAIL


class Phone(Boots):
    """Where the phone is ("android", "fastboot" or "gone") and whether
    fb_mode is set. Every call is logged."""
    serial = "ZY225DPT7F"

    def __init__(self, where="android", fb=False, clear_fails=False, active="a", falls_back_to=None,
                 build_id="B1"):
        self.where, self.fb, self.clear_fails, self.calls = where, fb, clear_fails, []
        self.journal_at_set = None
        # The slot fastboot reports active; the slot Android runs (R7-1: a
        # bootloader may fall back to the other one); the build on it.
        self.active, self.running, self.falls_back_to, self.build_id = active, active, falls_back_to, build_id
        if where == "android":
            self._new_boot()

    def getvar(self, name):
        assert name == "current-slot" and self.where == "fastboot", (name, self.where)
        return f"_{self.active}" if self.active else ""

    def guard(self):
        self.calls.append(("guard",))
        if self.where != "android":
            raise RuntimeError(f"guard: phone is in {self.where}")

    def getprop(self, name):
        assert name == "ro.boot.slot_suffix" and self.where == "android", (name, self.where)
        return f"_{self.running}"

    def root(self, seconds=60):
        pass

    def sh(self, cmd, timeout=30):
        assert "build_id" in cmd and self.where == "android", (cmd, self.where)
        key = "ro.vendor.river.build_id" if "vendor" in cmd else "ro.river.build_id"
        return f"{key}={self.build_id}\n"

    def in_fastboot(self):
        return self.where == "fastboot"

    def state(self):
        return "device" if self.where == "android" else ""

    def reboot_adb(self, target=None):
        self.calls.append(("reboot_adb", target))
        self.where = "fastboot" if target == "bootloader" or self.fb else "android"
        if self.where == "android":
            self._new_boot()

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
        if self.where == "android":
            self.running = self.falls_back_to or self.active
            self._new_boot()

    def wait_for_device(self, seconds):
        if self.where != "android":
            raise TimeoutError("no adb")

    def wait_boot_completed(self, seconds):
        self.calls.append(("boot_completed",))


class Obligation(unittest.TestCase):
    def test_arm_journals_before_the_set(self):
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p, "a", ("B1", "B1"))
            self.assertIsNotNone(p.journal_at_set, "fb_mode_set ran before the obligation was durable")
            self.assertEqual(p.journal_at_set["where"], jmod.HOST)
            self.assertIsNotNone(fbmode.pending())
            x = fbmode.pending()["extra"]
            self.assertEqual((x["slot"], x["build_ids"], x["cleared_at"]), ("a", ["B1", "B1"], None))
            with self.assertRaisesRegex(RuntimeError, "already pending"):
                fbmode.arm(p, "a", ("B1", "B1"))
            self.assertEqual(p.calls, [("fb_mode_set",)])

    def test_killed_while_armed_then_recovered_from_android(self):
        """Armed, booted Android with fastboot continue, then the host died:
        recovery takes the phone to fastboot, clears fb_mode and boots."""
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p, "a", ("B1", "B1"))
            p.where = "android"          # fastboot continue, then the harness was killed
            what = fbmode.recover(p, 10).what
            self.assertIn("booted Android", what)
            self.assertEqual(p.calls[1:], [("reboot_adb", "bootloader"), ("fb_mode_clear",),
                                           ("reboot_fastboot", None), ("boot_completed",), ("guard",)])
            self.assertEqual((p.where, p.fb), ("android", False))
            self.assertIsNone(fbmode.pending())

    def test_killed_after_the_stop_recovers_from_fastboot(self):
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p, "a", ("B1", "B1"))                # then sysrq, and the bootloader stopped: the phone sits in fastboot
            fbmode.recover(p, 10)
            self.assertEqual(p.calls[1:], [("fb_mode_clear",), ("reboot_fastboot", None), ("boot_completed",),
                                           ("guard",)])
            self.assertEqual((p.where, p.fb), ("android", False))
            self.assertIsNone(fbmode.pending())

    def test_phone_in_fastboot_with_nothing_pending(self):
        with bench_home():
            p = Phone("fastboot")
            self.assertIn("cleared fb_mode", fbmode.recover(p, 10).what)
            self.assertEqual(p.calls, [("fb_mode_clear",)])            # cleared, left in fastboot
            p = Phone("fastboot")
            fbmode.recover(p, 10, boot=True)                            # calibration boots it
            self.assertEqual(p.where, "android")

    def test_nothing_to_do(self):
        with bench_home():
            p = Phone("android")
            self.assertEqual(fbmode.recover(p, 10), fbmode.Recovery(None, None))
            self.assertEqual(p.calls, [])

    def test_failed_clear_keeps_the_obligation(self):
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p, "a", ("B1", "B1"))
            p.clear_fails = True
            with self.assertRaises(RuntimeError):
                fbmode.recover(p, 10)
            self.assertIsNotNone(fbmode.pending())

    def test_unreachable_phone_keeps_the_obligation(self):
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p, "a", ("B1", "B1"))
            p.where = "gone"
            with self.assertRaises(RuntimeError):
                fbmode.recover(p, 10)
            self.assertIsNotNone(fbmode.pending())


class VerifyBeforeSettle(unittest.TestCase):
    """R7-1, R8-1: fbmode.recover resolves the obligation only after the
    booted phone passed verify_boot: the guard, the slot and both build
    IDs, against ``want`` or the identity the obligation was armed with."""

    def test_bootloader_fallback_keeps_the_obligation(self):
        for want in (None, fbmode.Want("b", ("B1", "B1"))):
            with self.subTest(want=want), bench_home():
                p = Phone("fastboot", active="b", falls_back_to="a")
                fbmode.arm(p, "b", ("B1", "B1"))
                with self.assertRaisesRegex(RuntimeError, "runs slot a"):
                    fbmode.recover(p, 10, boot=True, want=want)
                self.assertEqual((p.where, p.running), ("android", "a"))
                self.assertIsNotNone(fbmode.pending())

    def test_failed_build_check_keeps_the_obligation(self):
        with bench_home():
            p = Phone("fastboot", active="b")
            fbmode.arm(p, "b", ("B1", "B1"))
            p.build_id = "B2"
            with self.assertRaisesRegex(RuntimeError, "build IDs system=B2"):
                fbmode.recover(p, 10, boot=True)
            self.assertIsNotNone(fbmode.pending())
            p.build_id = "B1"
            r = fbmode.recover(p, 10, boot=True)
            self.assertIn("verified: slot b, build IDs B1/B1", r.what)
            self.assertEqual(r.proof.slot, "b")
            self.assertIsNone(fbmode.pending())

    def test_wrong_active_slot_raises_before_anything(self):
        for want in (None, fbmode.Want("b", ("B1", "B1"))):
            with self.subTest(want=want), bench_home():
                p = Phone("fastboot", active="a")
                fbmode.arm(p, "b", ("B1", "B1"))
                with self.assertRaises(fbmode.WrongSlot):
                    fbmode.recover(p, 10, boot=True, want=want)
                self.assertEqual(p.calls[1:], [])
                self.assertIsNotNone(fbmode.pending())


class Callers(unittest.TestCase):
    def test_to_fastboot_never_clears_or_boots(self):
        """R5-1: to_fastboot only gets to fastboot; the caller clears fb_mode
        and sets the slot it wants first."""
        with bench_home():
            p = Phone("fastboot", fb=True)
            fbmode.arm(p, "a", ("B1", "B1"))
            flashing.to_fastboot(p, 5)
            self.assertEqual(p.calls, [("fb_mode_set",)])
            self.assertIsNotNone(fbmode.pending())

    def test_generic_recovery_keeps_the_obligation_until_android_booted(self):
        """R5-1: a host killed between the clear and the boot leaves the
        entry, so the next recovery boots the phone."""
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p, "a", ("B1", "B1"))
            p.reboot_fastboot = mock.Mock(side_effect=KeyboardInterrupt)    # killed here
            with self.assertRaises(KeyboardInterrupt):
                fbmode.recover(p, 10)
            self.assertFalse(p.fb)
            self.assertIsNotNone(fbmode.pending(), "resolved before the phone left fastboot")
            del p.reboot_fastboot
            self.assertIn("booted Android", fbmode.recover(p, 10).what)
            self.assertEqual(p.where, "android")
            self.assertIsNone(fbmode.pending())

    def test_boot_false_never_boots(self):
        """R5-16: a dev flash recovers with boot=False and stays in fastboot,
        even when it had to take the phone out of Android. R8-1: nothing was
        booted or verified, so the obligation stays pending (stamped
        cleared); the dev flash settles it after its own verified boot."""
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p, "a", ("B1", "B1"))
            p.where = "android"
            self.assertIn("stays in fastboot", fbmode.recover(p, 10, boot=False).what)
            self.assertEqual(p.calls[1:], [("reboot_adb", "bootloader"), ("fb_mode_clear",)])
            self.assertEqual(p.where, "fastboot")
            self.assertIsNotNone(fbmode.pending())
            self.assertIsNotNone(fbmode.pending()["extra"]["cleared_at"])

    def test_calibration_starts_from_fastboot(self):
        from riverqa import calibrate
        with bench_home():
            p = Phone("fastboot")
            with mock.patch.object(calibrate, "Device", lambda *a: p), \
                    mock.patch.object(p, "state", create=True, side_effect=lambda: "device" if p.where == "android"
                                      else ""):
                # The first guard is fbmode.recover's own check of the
                # booted phone (R7-1); the second is calibration's.
                p.guard = mock.Mock(side_effect=[None, RuntimeError("stop here")])
                with self.assertRaises(RuntimeError):
                    calibrate.run(1)
            self.assertEqual(p.calls[:3], [("fb_mode_clear",), ("reboot_fastboot", None), ("boot_completed",)])
            self.assertEqual(p.guard.call_count, 2)

    def test_phone_qa_preflight_recovers_then_stops(self):
        from riverqa import qa
        from riverqa.results import PASS, Result
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p, "a", ("B1", "B1"))

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
                fbmode.arm(p, "a", ("B1", "B1"))
                # fbmode.recover's own guard passes (R7-1); bench's raises.
                p.guard = mock.Mock(side_effect=[None, RuntimeError("guarded")])
                a = mock.Mock(batch=batch, flash=False)
                with mock.patch.object(bench, "Device", lambda *x: p):
                    try:
                        got = bench.run(a)
                    except RuntimeError as e:
                        got = str(e)
                self.assertEqual(got, want)
                self.assertIsNone(fbmode.pending())
                self.assertFalse(p.fb)

    def test_phone_qa_recover_reports_once(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from riverqa import qa
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p, "a", ("B1", "B1"))
            out = io.StringIO()
            with mock.patch.object(qa, "Device", lambda *a: p), redirect_stderr(out), redirect_stdout(out):
                self.assertEqual(qa.main(["recover"]), 0)
            self.assertEqual(out.getvalue().count("cleared a pending fb_mode obligation"), 1, out.getvalue())
            self.assertIsNone(fbmode.pending())
            self.assertEqual(p.where, "android")

    def test_restore_handler(self):
        from riverqa import restore
        with bench_home():
            p = Phone("fastboot")
            fbmode.arm(p, "a", ("B1", "B1"))
            p.where = "android"

            class R:
                dev = p
                journal = fbmode.journal()
            run = R()
            ok, msg = restore.restore_entry(run, fbmode.pending(run.journal))
            self.assertTrue(ok, msg)
            self.assertIsNone(fbmode.pending())



class FailedRecoveryExitsCleanly(unittest.TestCase):
    """R5-11, R5-15: a RuntimeError or TimeoutError from fbmode.recover ends
    the command with a clean exit code; the obligation stays pending."""

    def gone(self):
        p = Phone("fastboot")
        fbmode.arm(p, "a", ("B1", "B1"))
        p.where = "gone"
        return p

    def test_device_bench_halts(self):
        import io
        from contextlib import redirect_stderr
        from riverqa import bench
        with bench_home():
            p = self.gone()
            with mock.patch.object(bench, "Device", lambda *a: p), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(bench.main(["current"]), flashing.EXIT_HALT)
            self.assertIn("HALT: fb_mode recovery failed", err.getvalue())
            self.assertIsNotNone(fbmode.pending())

    def test_boot_timing_and_controls_report_error(self):
        import io
        from contextlib import redirect_stderr
        from riverqa import calibrate, reboots
        for p_err in (RuntimeError, TimeoutError):
            with bench_home():
                p = self.gone()
                if p_err is TimeoutError:
                    p.where = "fastboot"
                    p.wait_for_device = mock.Mock(side_effect=TimeoutError("no adb"))
                with mock.patch.object(calibrate, "Device", lambda *a: p), redirect_stderr(io.StringIO()) as err:
                    # R8-1: a failure with the obligation pending halts (3).
                    self.assertEqual(calibrate.boot_timing_main([]), 3)
                    with mock.patch.object(reboots, "load_timing", return_value={"paths": {"sysrq": {}}}), \
                            mock.patch.object(calibrate, "record_problems", return_value=[]):
                        self.assertEqual(calibrate.prove_main(), 3)
                self.assertEqual(err.getvalue().count("error: fb_mode recovery failed"), 2, err.getvalue())
                self.assertIsNotNone(fbmode.pending())


if __name__ == "__main__":
    unittest.main()
