"""Review round 12, the dev flash before the first baseline (no
slot-state.json). R12-1: what may be overwritten is decided by the active
slot alone, so that slot must be shown to boot (the phone in Android with
sys.boot_completed=1) before the other slot is written; a phone in fastboot,
or one that answers adb without completing boot, is refused before anything
is cleared or written. Halts in this state with an obligation pending give
the R10-3/R11-1 way back, never flash --recover. R12-2: the advice after a
--set-active or wipe timeout keeps the caller's --wipe choice and starts by
confirming the active slot.

The fakes are those of rounds 9 and 10, so these regressions also run at
the round-11 code (c4064da). No adb or fastboot."""
import unittest

import _path  # noqa: F401
from benchhome import bench_home
from riverqa import fbmode, flashing
from test_round8 import DEV_BID
from test_round9 import R9Phone, booting_reboot, dev_flash, flashes, legacy_entry
from test_slot_recovery import BID


def not_booted(p):
    """adb answers in Android, but the boot never completes."""
    real = p.getprop

    def getprop(name, real=real):
        if name == "sys.boot_completed":
            return ""
        return real(name)
    p.getprop = getprop


class R12_1PreBaselineBootedSlot(unittest.TestCase):
    def test_adb_without_boot_completed_writes_nothing(self):
        """Sol round 12: slot b answers adb but never completes boot; slot a
        is the only slot that boots. At c4064da the flash wrote all four a
        partitions."""
        for owed in (False, True):
            with self.subTest(owed=owed), bench_home():
                p = R9Phone(where="android", active="b", bootable=("a",), fb=False, builds={"a": BID})
                not_booted(p)
                if owed:
                    legacy_entry()
                rc, err = dev_flash(p, reboot=booting_reboot)
                self.assertEqual(rc, flashing.EXIT_HALT if owed else flashing.EXIT_ERROR, err)
                self.assertIn("has not completed boot", err)
                self.assertEqual(flashes(p.calls), [])
                self.assertNotIn(("fb_mode_clear",), p.calls)
                if owed:
                    self.assertIsNotNone(fbmode.pending())
                    self.assertNotIn("flash --recover", err)

    def test_second_no_reboot_flash_keeps_the_original_slot(self):
        """Sol round 12: two pre-baseline --no-reboot flashes. The first
        writes b from a booted a and leaves the phone in fastboot on b; at
        c4064da the second wrote a before b had ever booted. Now it is
        refused (exit 1) with nothing written."""
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a", "b"), fb=False,
                        builds={"a": BID, "b": DEV_BID})
            rc, err = dev_flash(p, "--no-reboot")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertEqual({c[2][-1] for c in flashes(p.calls)}, {"b"})
            p.calls.clear()
            rc, err = dev_flash(p, "--no-reboot")
            self.assertEqual(rc, flashing.EXIT_ERROR, err)
            self.assertIn("nothing shows that slot b boots", err)
            self.assertEqual(flashes(p.calls), [])

    def test_booted_phone_still_flashes(self):
        """The bootstrap's step 0 is unchanged: from a booted phone the dev
        flash writes the other slot and verifies its boot."""
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a", "b"), fb=False,
                        builds={"a": BID, "b": DEV_BID})
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertEqual({c[2][-1] for c in flashes(p.calls)}, {"b"})

    def test_pre_write_halts_give_the_slotless_way_back(self):
        """Sol round 12 (minor): before the first baseline a halt before any
        write sent a slotless entry to flash --recover, which can't verify it."""
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a", "b"), fb=False)
            p.getprop = lambda name: "1" if name == "sys.boot_completed" else ""
            legacy_entry()
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertIn("records no slot", err)
            self.assertNotIn("flash --recover finishes it", err)
            self.assertEqual(flashes(p.calls), [])


class R12_2TimeoutAdvice(unittest.TestCase):
    def test_set_active_timeout_without_wipe_advises_no_erase(self):
        """Sol and Opus round 12: with no --wipe, nothing pending, a
        --set-active timeout advised erasing userdata. Now the advice checks
        the active slot first and never names an erase."""
        for argv in (("--no-reboot",), ()):
            for st in (True, False):
                with self.subTest(argv=argv, state=st), bench_home():
                    if st:
                        from test_slot_recovery import state
                        state(pair_kind=None)
                    p = R9Phone(where="fastboot" if st else "android", active="a", bootable=("a", "b"),
                                fb=bool(st))
                    real_ok = p.fastboot_ok

                    def fastboot_ok(*args, timeout=300, real_ok=real_ok):
                        if args[:1] == ("--set-active=b",):
                            raise TimeoutError("fastboot --set-active=b: timed out after 60 s")
                        return real_ok(*args, timeout=timeout)
                    p.fastboot_ok = fastboot_ok
                    rc, err = dev_flash(p, *argv, reboot=None if argv else booting_reboot)
                    self.assertEqual(rc, flashing.EXIT_HALT, err)
                    self.assertIn("getvar current-slot", err)
                    self.assertIn("run without --wipe", err)
                    self.assertNotIn("erase userdata", err)


if __name__ == "__main__":
    unittest.main()


class R13FollowUps(unittest.TestCase):
    def test_slot_and_boot_state_come_from_one_boot(self):
        """Sol round 13 (major 1): slot b answers adb before completing boot
        and the phone falls back to a between two separate getprop calls.
        At a1abb2a the flash paired b's suffix with a's completed boot,
        targeted a and wrote it. With one shell call both values are from
        the same boot (here: a, completed), so it writes b."""
        with bench_home():
            p = R9Phone(where="android", active="b", bootable=("a", "b"), fb=False,
                        builds={"a": BID, "b": DEV_BID})
            real = p.getprop

            def getprop(name, real=real, p=p):
                if name == "ro.boot.slot_suffix" and p.running == "b":
                    v = real(name)
                    p.running = p.active = "a"      # fell back to a before the next read
                    return v
                return real(name)
            p.getprop = getprop
            # One shell call sees one boot: the one the phone fell back to.
            p.slot_boot_state = lambda: ("_a", True)
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertNotIn("a", {c[2][-1] for c in flashes(p.calls)}, err)

    def test_sentinel_entry_in_fastboot_advice_leads_out(self):
        """Opus round 13 (m1): no slot-state.json, an obligation armed on a
        with the unstamped sentinel, the phone in fastboot on a. R12-1
        refuses; at a1abb2a the advice said "run flash stripped with slot a
        active", the command just refused. Now it says to clear fb_mode and
        boot slot a first, and following it lets the next flash settle."""
        from test_round8 import arm_entry
        with bench_home():
            p = R9Phone(where="fastboot", active="a", bootable=("a", "b"), builds={"a": BID, "b": DEV_BID})
            arm_entry("a", ids=("unstamped", "unstamped"))
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertIn("phone-qa recover: it clears fb_mode, boots slot a to completion", err)
            self.assertEqual(flashes(p.calls), [])
            # Follow the advice (Sol round 13): phone-qa recover boots slot a
            # and leaves the obligation pending; then the flash settles it.
            from test_round10 import recover_cmd
            rc, out = recover_cmd(p)
            self.assertEqual(rc, 0, out)
            self.assertIsNotNone(fbmode.pending())
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertIsNone(fbmode.pending())
            self.assertEqual({c[2][-1] for c in flashes(p.calls)}, {"b"})

    def test_not_booted_with_nothing_pending_says_wait(self):
        """Opus round 13 (m2): in Android before boot completes, the advice
        first says to wait for the boot to finish."""
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a", "b"), fb=False)
            not_booted(p)
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_ERROR, err)
            self.assertIn("If the phone is still booting, wait until it finishes", err)


class R13GeminiMinors(unittest.TestCase):
    def test_pre_baseline_clear_failure_gives_the_way_back(self):
        """Gemini round 13 (m2): the second clear fails before the first
        baseline. The advice said "run the flash again", which R12-1 refuses
        from fastboot; now it gives the R10-3 way back."""
        from test_round8 import arm_entry
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a", "b"), fb=False, clear_fails_after=1,
                        builds={"a": BID, "b": DEV_BID})
            arm_entry("a")
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertIn("phone-qa recover", err)
            self.assertNotIn("then run the flash again", err)
            self.assertEqual(flashes(p.calls), [])
            self.assertIsNotNone(fbmode.pending())

    def test_not_booted_advice_reboots_to_the_bootloader_first(self):
        """Gemini round 13 (m3)."""
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a", "b"), fb=False)
            not_booted(p)
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_ERROR, err)
            self.assertIn("adb -s ZY225DPT7F reboot bootloader, then fastboot", err)
