"""Review round 9. R9-1: the first-baseline flash seeds slot state from
its proof. R9-2: a path that never boots needs no armed identity. R9-3:
calibration and the first baseline need a stamped running build. R9-4: a
dev flash reads the slot before it clears. R9-5: a dev flash reads nothing
after the settle, and a failed second clear halts cleanly. R9-7: settle's
freshness check uses the host's boot clock. R9-8: dataclasses.replace
can't carry the seal. R9-9: arming with an unreadable slot. R9-11: the
rollback halts name their own command.

As in test_round8, obligations are written straight to the journal, and
the fakes stay compatible with the round-8 code (100df60), so these
regressions also run there. No adb or fastboot."""
import dataclasses
import io
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from riverqa import fbmode, flashing, journal as jmod, reboots
from test_round8 import DEV_BID, R8Phone, arm_entry
from test_slot_recovery import BID, SHA, batch_inputs, order, patches, run_main, state


def legacy_entry():
    """An obligation armed before round 8: no slot, no build IDs."""
    fbmode.journal().record(jmod.HOST, "fb_mode", "fb_mode", "clear",
                            extra={"serial": "ZY225DPT7F", "armed_at": "t0"})


def boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


class R9Phone(R8Phone):
    """R8Phone whose uptime runs on the host's boot clock, so a step of the
    wall clock (time.time) moves the host's reading but not the phone's
    uptime, as on a real phone. ``clear_fails_after`` lets the first N
    fb_mode_clear calls succeed and fails the rest."""

    def __init__(self, *a, clear_fails_after=None, unstamped=False, **kw):
        self.clear_fails_after = clear_fails_after
        self.unstamped = unstamped
        self.clears = 0
        super().__init__(*a, **kw)

    def _new_boot(self):
        super()._new_boot()
        self._bt0 = boottime()

    def boot_status(self):
        from riverqa.device import BootStatus
        if self.state() != "device":
            return None
        t = time.monotonic()
        bt = boottime()
        st = BootStatus(self.boot_id(), True, bt - self._bt0, t, t, time.time())
        st.host_boottime = bt       # a plain attribute at 100df60, a field now
        return st

    def fb_mode_clear(self):
        self.clears += 1
        if self.clear_fails_after is not None and self.clears > self.clear_fails_after:
            self.clear_fails = True
        return super().fb_mode_clear()

    def sh(self, cmd, timeout=30):
        if self.unstamped and "build_id" in cmd:
            self._note(cmd)
            return ""
        return super().sh(cmd, timeout)


def dev_flash(p, *argv, reboot=None):
    from riverqa import stage
    ps = [mock.patch.object(flashing, "check_images", return_value={}),
          mock.patch.object(stage, "read_prop", side_effect=lambda path, key: DEV_BID if "build_id" in key else None)]
    if reboot is not None:
        ps.append(mock.patch.object(reboots, "planned_reboot", reboot))
    with patches(ps):
        return run_main(p, "/nonexistent-product", *argv)


def booting_reboot(dev, path, *, boot_timeout, in_fastboot=None, **kw):
    if dev.state() == "device":
        dev.reboot_adb("bootloader")
    in_fastboot()
    dev.reboot_fastboot()
    return mock.Mock(host_boot_seconds=42.0)


def flashes(calls):
    return [c for c in calls if c[:2] == ("fastboot", "flash")]


class Round9Majors(unittest.TestCase):
    # R9-1 (Sol 1): the first-baseline flash
    def test_first_baseline_reads_nothing_after_settling(self):
        """No slot-state.json, an obligation armed on slot a: the provisional
        known-good slot and its boot hash come from the proof. At 100df60
        the flash settled, then read the guard, the slot and the hash."""
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a",))
            arm_entry("a")
            with patches(batch_inputs(kind="baseline")):
                rc, err = run_main(p, "--batch", "x", "--wipe")
            self.assertEqual(rc, "raised", err)              # StopHere at slots.reserve
            self.assertIn("stopped after fb_mode recovery", err)
            self.assertIsNone(fbmode.pending())
            self.assertEqual(p.after_settle, [])
            st = flashing.load_state()
            self.assertEqual((st["known_good_slot"], st["known_good"]["boot_sha256"]), ("a", SHA))

    def test_first_baseline_hash_timeout_writes_nothing(self):
        """A timeout on the boot hash: HALT 3, the obligation pending, no
        slot-state.json. At 100df60 the hash was read after the settle and
        the TimeoutError ended in exit 1 with the obligation resolved."""
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a",), timeout_on=("sha256sum", 1))
            arm_entry("a")
            with patches(batch_inputs(kind="baseline")):
                rc, err = run_main(p, "--batch", "x", "--wipe")
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertIsNotNone(fbmode.pending(), "the fb_mode obligation was resolved")
            self.assertIsNone(flashing.load_state())

    def test_first_baseline_without_an_obligation_uses_a_proof(self):
        """Nothing pending, the phone in Android: verify_boot of the running
        slot gives the slot and the hash; an unstamped build is refused
        (R9-3) with nothing written."""
        for unstamped, want_rc in ((False, "raised"), (True, flashing.EXIT_ERROR)):
            with self.subTest(unstamped=unstamped), bench_home():
                p = R9Phone(where="android", active="b", bootable=("b",), fb=False, unstamped=unstamped,
                            builds={"a": BID, "b": BID})
                with patches(batch_inputs(kind="baseline")):
                    rc, err = run_main(p, "--batch", "x", "--wipe")
                self.assertEqual(rc, want_rc, err)
                if unstamped:
                    self.assertIn("running build is unstamped; dev-flash a build from this tree first", err)
                    self.assertIsNone(flashing.load_state())
                else:
                    self.assertIn("stopped after fb_mode recovery", err)
                    st = flashing.load_state()
                    self.assertEqual((st["known_good_slot"], st["known_good"]["boot_sha256"]), ("b", SHA))
                self.assertEqual(p.boots, [])

    # R9-2 (Sol 2): no identity needed on a path that never boots
    def test_dev_flash_settles_a_legacy_obligation(self):
        """Sol's case: no slot-state.json and an entry armed before round 8.
        The dev flash clears, writes, boots, verifies and settles: exit 0.
        At 100df60 recover(boot=False) asked for the armed identity first
        and halted (3)."""
        with bench_home():
            # R11-1: with no slot recorded, only a phone found in Android
            # shows which slot boots, so the case starts there.
            p = R9Phone(where="android", active="a", bootable=("a", "b"), builds={"a": BID, "b": DEV_BID})
            legacy_entry()
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertIsNone(fbmode.pending())
            self.assertEqual([c[2] for c in flashes(p.calls)][:1], ["boot_b"])
            self.assertEqual([o for o in order(p.calls) if o != "reboot_adb bootloader"][:1], ["clear"])

    def test_legacy_obligation_in_fastboot_writes_nothing(self):
        """Sol round 11: no slot-state.json, an entry from before round 8,
        slot b active in fastboot after an interrupted flash, and only slot a
        boots. At 0fb68fa the dev flash wrote all four a partitions. Now it
        halts before any clear or write, and its advice names no flash until
        a slot is shown to boot."""
        with bench_home():
            p = R9Phone(where="fastboot", active="b", bootable=("a",), builds={"a": BID})
            legacy_entry()
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertEqual(flashes(p.calls), [])
            self.assertNotIn("clear", order(p.calls))
            self.assertIsNotNone(fbmode.pending())
            self.assertIn("check that X reaches Android", err)
            self.assertIn("If no slot boots, stop and tell Zak", err)
            for argv in (("--no-reboot",),):
                rc, err = dev_flash(p, *argv)
                self.assertEqual(rc, flashing.EXIT_ERROR, err)
                self.assertIn("advises no flash until you have found one", err)
            self.assertEqual(flashes(p.calls), [])

    def test_recover_reads_the_armed_identity_only_to_boot(self):
        with bench_home():
            p = R9Phone(where="fastboot", active="a", bootable=("a",))
            legacy_entry()
            fbmode.recover(p, 10, boot=False)              # clears and stamps
            self.assertEqual(order(p.calls), ["clear"])
            self.assertIsNotNone(fbmode.pending()["extra"]["cleared_at"])
            p.calls.clear()
            with self.assertRaisesRegex(RuntimeError, "no recorded slot and build IDs"):
                fbmode.recover(p, 10, boot=True)
            self.assertEqual((p.calls, p.boots), ([], []))  # nothing changed before the refusal

    # R9-3 (Sol 3): calibration refuses an unstamped build up front
    def test_boot_timing_refuses_an_unstamped_build_before_any_reboot(self):
        from riverqa import calibrate
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a",), fb=False, unstamped=True)
            one = mock.Mock(side_effect=AssertionError("a calibration reboot"))
            with mock.patch.object(calibrate, "Device", lambda *a: p), mock.patch.object(calibrate, "one", one), \
                    mock.patch.object(calibrate, "UsbWatcher"), mock.patch.object(calibrate, "prove", one), \
                    mock.patch.object(calibrate, "boot_count_now", one), \
                    mock.patch.object(reboots, "load_timing", return_value={"paths": {"sysrq": {}}}), \
                    mock.patch.object(calibrate, "record_problems", return_value=[]), \
                    redirect_stderr(io.StringIO()) as err:
                rc1 = calibrate.boot_timing_main([])
                rc2 = calibrate.prove_main()
            self.assertEqual((rc1, rc2), (1, 1), err.getvalue())
            self.assertEqual(err.getvalue().count(
                "running build is unstamped; dev-flash a build from this tree first (`flash stripped --wipe`"), 2)
            self.assertEqual((one.call_count, p.calls, p.boots), (0, [], []))

    # R9-4 (Gemini 3): the slot is read before the clear
    def test_dev_flash_unreadable_slot_halts_before_the_clear(self):
        """No slot-state.json, so the active slot picks the target; it can't
        be read: HALT 3 with fb_mode still set. At 100df60 the flash cleared
        fb_mode first."""
        with bench_home():
            p = R9Phone(where="fastboot", active="a", bootable=("a", "b"), slot_unreadable=True)
            arm_entry("a")
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertIn("cannot read the active slot", err)
            self.assertEqual(order(p.calls), [], "fb_mode was cleared or a slot touched")
            self.assertTrue(p.fb)
            self.assertIsNotNone(fbmode.pending())

    # R9-5 (Gemini 4, Opus m3)
    def test_dev_flash_reads_nothing_after_settling(self):
        """At 100df60 the flash read ro.lineage.display.version after the
        settle."""
        with bench_home():
            state(pair_kind=None)
            p = R9Phone(where="android", active="a", bootable=("a", "b"), builds={"a": BID, "b": DEV_BID})
            arm_entry("a")
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertIsNone(fbmode.pending())
            self.assertEqual(p.after_settle, [])

    def test_dev_flash_second_clear_failure_halts(self):
        """The first clear (recover(boot=False)) succeeds, write_images'
        clear fails with the obligation pending: HALT 3 before any image is
        written, the obligation pending. At 100df60 ClearFailed escaped
        main as a traceback."""
        for argv, reboot in ((("--no-reboot",), None), ((), booting_reboot)):
            with self.subTest(argv=argv), bench_home():
                state(pair_kind=None)
                p = R9Phone(where="fastboot", active="a", bootable=("a", "b"), clear_fails_after=1)
                arm_entry("a")
                rc, err = dev_flash(p, *argv, reboot=reboot)
                self.assertEqual(rc, flashing.EXIT_HALT, err)
                self.assertIn("fb_mode_clear failed", err)
                self.assertEqual(flashes(p.calls), [])
                self.assertIsNotNone(fbmode.pending())


    def test_dev_flash_write_failure_halts(self):
        """Found in the self-review (the R9-5 class): a failed `fastboot
        flash` with the obligation pending halts (3) with it pending. At
        100df60 the RuntimeError escaped main as a traceback."""
        for argv, reboot in ((("--no-reboot",), None), ((), booting_reboot)):
            with self.subTest(argv=argv), bench_home():
                state(pair_kind=None)
                p = R9Phone(where="fastboot", active="a", bootable=("a", "b"))
                real = p.fastboot_ok

                def fastboot_ok(*args, timeout=300, real=real):
                    if args[0] == "flash":
                        raise RuntimeError("fastboot flash boot_b failed: FAILED (remote: 'size too large')")
                    return real(*args, timeout=timeout)
                p.fastboot_ok = fastboot_ok
                arm_entry("a")
                rc, err = dev_flash(p, *argv, reboot=reboot)
                self.assertEqual(rc, flashing.EXIT_HALT, err)
                self.assertIn("size too large", err)
                self.assertIsNotNone(fbmode.pending())


class Round9Minors(unittest.TestCase):
    def stamped_obligation(self):
        """A phone in Android on slot a with an obligation pending whose
        clear is stamped after this boot began."""
        p = R9Phone(where="android", active="a", bootable=("a",), fb=False)
        arm_entry("a")
        time.sleep(0.01)
        p.where = "fastboot"
        fbmode.clear(p)
        p.where = "android"                               # the same boot as before the clear
        return p

    # R9-7 (Opus m4, Sol)
    def test_a_wall_clock_step_cannot_date_an_old_boot_after_the_clear(self):
        """The wall clock steps an hour forward after the clear. A proof of
        the boot that began before the clear must not settle. At 100df60
        (wall time minus uptime) it did."""
        with bench_home():
            p = self.stamped_obligation()
            real = time.time
            with mock.patch("time.time", side_effect=lambda: real() + 3600):
                proof = fbmode.verify_boot(p, fbmode.Want("a", (BID, BID)))
                with self.assertRaises(fbmode.NotSettled):
                    fbmode.settle(proof)
            self.assertIsNotNone(fbmode.pending())

    def test_a_host_reboot_after_the_clear_refuses_then_recovery_restamps(self):
        with bench_home():
            p = self.stamped_obligation()
            p.reboot_adb()                                 # a boot after the clear
            with mock.patch.object(fbmode, "host_boot_id", return_value="another-host-boot", create=True):
                proof = fbmode.verify_boot(p, fbmode.Want("a", (BID, BID)))
                with self.assertRaisesRegex(fbmode.NotSettled, "host rebooted"):
                    fbmode.settle(proof)
            self.assertIsNotNone(fbmode.pending())
            # A rerun of the recovery clears and stamps again, then settles.
            self.assertIsNotNone(fbmode.recover(p, 10).proof)
            self.assertIsNone(fbmode.pending())

    def test_a_round8_stamp_is_not_trusted(self):
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a",), fb=False)
            arm_entry("a")
            j = fbmode.journal()
            j.annotate(fbmode.pending(j), cleared_at=time.time() - 3600)
            with self.assertRaisesRegex(fbmode.NotSettled, "no host boot clock"):
                fbmode.settle(fbmode.verify_boot(p, fbmode.Want("a", (BID, BID))))
            self.assertIsNotNone(fbmode.pending())

    # R9-8 (Opus m2)
    def test_replace_does_not_carry_the_seal(self):
        """dataclasses.replace of a sealed proof gives an unsealed one. At
        100df60 the seal was an __init__ argument, so replace copied it and
        the altered proof settled."""
        with bench_home():
            p = self.stamped_obligation()
            p.reboot_adb()
            proof = fbmode.verify_boot(p, fbmode.Want("a", (BID, BID)))
            forged = dataclasses.replace(proof, build_ids=("river-other", "river-other"))
            with self.assertRaises(TypeError):
                fbmode.settle(forged)
            self.assertIsNotNone(fbmode.pending())
            self.assertTrue(fbmode.settle(proof))

    # R9-9 (Opus m6)
    def test_arm_with_no_slot_property_is_a_clean_refusal(self):
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a",), fb=False)
            p.getprop = lambda name: None
            with self.assertRaisesRegex(RuntimeError, "cannot arm fb_mode: cannot read the running slot"):
                reboots.arm_bootloader_stop(p, 10)
            self.assertEqual((p.calls, fbmode.pending()), ([], None))

    # R9-11 (Gemini)
    def test_rollback_halts_name_flash_rollback(self):
        for fail in ("set-active", "erase"):
            with self.subTest(fail=fail), bench_home():
                state(pair_kind="calibration")
                p = R9Phone(active="b", bootable=("a",), set_active_fails=fail == "set-active")
                if fail == "erase":
                    real = p.fastboot

                    def fastboot(*args, timeout=120, real=real):
                        if args[:2] == ("erase", "userdata"):
                            return mock.Mock(returncode=1, stdout="", stderr="FAILED (remote: 'denied')")
                        return real(*args, timeout=timeout)
                    p.fastboot = fastboot
                arm_entry("a")
                rc, err = run_main(p, "--rollback")
                self.assertEqual(rc, flashing.EXIT_HALT, err)
                self.assertIn("flash --rollback finishes it", err)
                self.assertNotIn("flash --recover", err)
                self.assertIsNotNone(fbmode.pending())


if __name__ == "__main__":
    unittest.main()
