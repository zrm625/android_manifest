"""Review round 5, R5-1/R5-16: slot recovery with an fb_mode obligation
pending. It must clear fb_mode in fastboot, set the fallback active and
reboot; it must never boot the active slot first (it may be the failed
candidate), and it resolves the obligation only once the phone runs the
fallback. No adb or fastboot."""
import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from riverqa import fbmode, flashing, slots

BID = "river-good-1"
SHA = "f" * 64


class SlotPhone:
    """A phone with two slots. ``bootable`` says which slots reach Android;
    a boot of an unbootable slot hangs (neither adb nor fastboot). Every
    action is logged in ``calls``; ``boots`` lists each slot booted."""
    serial = "ZY225DPT7F"

    def __init__(self, where="fastboot", active="b", fb=True, bootable=("a",), clear_fails=False):
        self.where, self.active, self.fb = where, active, fb
        self.bootable, self.clear_fails = set(bootable), clear_fails
        self.running = active if where == "android" else None
        self.calls, self.boots = [], []

    # adb
    def state(self):
        return "device" if self.where == "android" else ""

    def guard(self):
        if self.where != "android":
            raise AssertionError("adb guard with the phone not in Android")

    def boot_status(self):
        return None

    def reboot_adb(self, target=None):
        self.calls.append(("reboot_adb", target))
        if target == "bootloader":
            self.where, self.running = "fastboot", None
        else:
            self._boot()

    def root(self, seconds=60):
        pass

    def getprop(self, name):
        return f"_{self.running}" if name == "ro.boot.slot_suffix" else ""

    def sh(self, cmd, timeout=30):
        if "build_id" in cmd:
            key = "ro.vendor.river.build_id" if "vendor" in cmd else "ro.river.build_id"
            return f"{key}={BID if self.running == 'a' else 'river-cand'}\n"
        if "sha256sum" in cmd:
            return f"{SHA}  x\n"
        raise AssertionError(cmd)

    def wait_for_device(self, seconds):
        if self.where != "android":
            raise TimeoutError("no adb")

    def wait_boot_completed(self, seconds):
        if self.where != "android":
            raise TimeoutError("no boot completed")

    # fastboot
    def in_fastboot(self):
        return self.where == "fastboot"

    def wait_fastboot(self, seconds):
        if self.where != "fastboot":
            raise TimeoutError("no fastboot")

    def fastboot_guard(self):
        if self.where != "fastboot":
            raise AssertionError("fastboot guard with the phone not in fastboot")

    def getvar(self, name):
        assert self.where == "fastboot"
        return f"_{self.active}" if name == "current-slot" else ""

    def fb_mode_set(self):
        self.fb = True

    def fb_mode_clear(self):
        assert self.where == "fastboot"
        self.calls.append(("fb_mode_clear",))
        if self.clear_fails:
            raise RuntimeError("fastboot oem fb_mode_clear failed")
        self.fb = False

    def fastboot_ok(self, *args, timeout=300):
        assert self.where == "fastboot"
        self.calls.append(("fastboot",) + args)
        if args[0].startswith("--set-active="):
            self.active = args[0].split("=", 1)[1]

    def fastboot(self, *args, timeout=120):
        self.fastboot_ok(*args)
        return mock.Mock(returncode=0, stdout="", stderr="")

    def reboot_fastboot(self, target=None):
        assert self.where == "fastboot"
        self.calls.append(("reboot_fastboot", target))
        if target != "bootloader":
            self._boot()

    def _boot(self):
        if self.fb:
            self.where, self.running = "fastboot", None
            return
        self.boots.append(self.active)
        if self.active in self.bootable:
            self.where, self.running = "android", self.active
        else:
            self.where, self.running = "hung", None


def state(pair_kind="calibration"):
    st = slots.first_run("a", SHA, "t0")
    st["known_good"]["build_id"] = BID
    if pair_kind:
        slots.reserve(st, "cand", pair_kind, SHA, "t1")
        slots.mark_written(st, "flash-cand", "t2")
    flashing.save_state(st)
    return st


def run_flash(phone, *argv):
    with mock.patch.object(flashing, "Device", lambda *a: phone), redirect_stderr(io.StringIO()) as err:
        rc = flashing.main(list(argv))
    return rc, err.getvalue()


def order(calls):
    """The fastboot sequence as short names."""
    out = []
    for c in calls:
        if c[0] == "fb_mode_clear":
            out.append("clear")
        elif c[0] == "fastboot" and c[1].startswith("--set-active="):
            out.append("set-active " + c[1].split("=")[1])
        elif c[0] == "fastboot" and c[1:3] == ("erase", "userdata"):
            out.append("erase")
        elif c[0] in ("reboot_fastboot", "reboot_adb"):
            out.append(f"{c[0]} {c[1] or ''}".strip())
    return out


class Rollback(unittest.TestCase):
    """Sol's counterexample: obligation pending, the candidate b active in
    fastboot and unbootable."""

    def check_rolled_back(self, p):
        self.assertEqual(p.boots, ["a"], "booted another slot before the fallback")
        self.assertEqual(order(p.calls), ["clear", "set-active a", "erase", "reboot_fastboot"])
        self.assertEqual((p.where, p.running, p.fb), ("android", "a", False))
        self.assertIsNone(fbmode.pending())
        self.assertIsNone(flashing.load_state()["pair"])

    def test_flash_rollback(self):
        with bench_home():
            state()
            p = SlotPhone()
            fbmode.arm(p)
            rc, err = run_flash(p, "--rollback")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.check_rolled_back(p)

    def test_flash_recover_rolls_back(self):
        with bench_home():
            state()
            p = SlotPhone()
            fbmode.arm(p)
            rc, err = run_flash(p, "--recover")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.check_rolled_back(p)

    def test_candidate_in_android_goes_through_fastboot(self):
        with bench_home():
            state()
            p = SlotPhone(where="android", active="b", fb=True, bootable=("a", "b"))
            fbmode.arm(p)
            rc, err = run_flash(p, "--rollback")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertEqual(order(p.calls)[0], "reboot_adb bootloader")
            self.assertEqual(p.boots, ["a"])
            self.assertIsNone(fbmode.pending())

    def test_killed_between_clear_and_reboot_resumes(self):
        with bench_home():
            state()
            p = SlotPhone()
            fbmode.arm(p)
            real = p.reboot_fastboot
            p.reboot_fastboot = mock.Mock(side_effect=KeyboardInterrupt)
            with self.assertRaises(KeyboardInterrupt):
                run_flash(p, "--rollback")
            self.assertIsNotNone(fbmode.pending(), "resolved before the phone ran the fallback")
            self.assertEqual(p.boots, [])
            p.reboot_fastboot = real
            rc, err = run_flash(p, "--rollback")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertEqual(p.boots, ["a"])
            self.assertIsNone(fbmode.pending())

    def test_failed_clear_without_obligation_goes_on(self):
        """Opus minor 5: a failed clear with nothing pending never aborts a
        rollback."""
        with bench_home():
            state()
            p = SlotPhone(fb=False, clear_fails=True)
            rc, err = run_flash(p, "--rollback")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertIn("going on", err)
            self.assertEqual(p.boots, ["a"])

    def test_failed_clear_with_obligation_sets_fallback_then_halts(self):
        with bench_home():
            state()
            p = SlotPhone(clear_fails=True)
            fbmode.arm(p)
            rc, err = run_flash(p, "--rollback")
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertEqual(order(p.calls), ["clear", "set-active a"])
            self.assertEqual(p.boots, [])
            self.assertIsNotNone(fbmode.pending())
            self.assertIsNotNone(flashing.load_state()["pair"])

    def test_rescue_after_a_failed_first_boot(self):
        """A rescue comes through the same procedure."""
        with bench_home():
            st = state(pair_kind="calibration")
            p = SlotPhone()
            fbmode.arm(p)
            with redirect_stderr(io.StringIO()):
                flashing.rescue(p, st, "first boot timed out")
            self.assertEqual(p.boots, ["a"])
            self.assertIsNone(fbmode.pending())


class RecoverNoPair(unittest.TestCase):
    def test_known_good_active_clears_then_boots_it(self):
        with bench_home():
            state(pair_kind=None)
            p = SlotPhone(active="a", bootable=("a",))
            fbmode.arm(p)
            rc, err = run_flash(p, "--recover")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertEqual(order(p.calls), ["clear", "set-active a", "reboot_fastboot"])
            self.assertEqual((p.boots, p.running), (["a"], "a"))
            self.assertIsNone(fbmode.pending())
            self.assertEqual(flashing.load_state()["history"][-1]["event"], "fb-mode-recovered")

    def test_other_slot_active_stays_in_fastboot(self):
        """No pair, but a dev flash left slot b active: clear fb_mode, never
        boot b, never switch slots without a wipe."""
        with bench_home():
            state(pair_kind=None)
            p = SlotPhone(active="b", bootable=())
            fbmode.arm(p)
            rc, err = run_flash(p, "--recover")
            self.assertEqual(rc, flashing.EXIT_ERROR, err)
            self.assertEqual(order(p.calls), ["clear"])
            self.assertEqual((p.where, p.boots, p.fb), ("fastboot", [], False))
            self.assertIn("flash --rollback", err)

    def test_nothing_to_do(self):
        with bench_home():
            state(pair_kind=None)
            p = SlotPhone(where="android", active="a", fb=False)
            rc, err = run_flash(p, "--recover")
            self.assertEqual((rc, p.calls), (flashing.EXIT_OK, []))
            self.assertIn("No unresolved pair", err)


def run_main(phone, *argv):
    """run_flash, but an exception escaping main is an outcome too (a
    traceback, not exit 3): it comes back as ("raised", repr)."""
    try:
        return run_flash(phone, *argv)
    except Exception as e:     # noqa: BLE001
        return "raised", repr(e)


class FailingPhone(SlotPhone):
    """SlotPhone with recovery failures: ``set_active_fails`` (fastboot
    --set-active returns an error) and ``no_bootloader`` (adb reboot
    bootloader never reaches fastboot)."""

    def __init__(self, *a, set_active_fails=False, no_bootloader=False, **kw):
        super().__init__(*a, **kw)
        self.set_active_fails, self.no_bootloader = set_active_fails, no_bootloader

    def fastboot_ok(self, *args, timeout=300):
        if self.set_active_fails and args[0].startswith("--set-active="):
            self.calls.append(("fastboot",) + args)
            raise RuntimeError("fastboot --set-active failed: FAILED (remote: 'unknown command')")
        return super().fastboot_ok(*args, timeout=timeout)

    def reboot_adb(self, target=None):
        if self.no_bootloader and target == "bootloader":
            self.calls.append(("reboot_adb", target))
            self.where, self.running = "hung", None
            return
        return super().reboot_adb(target)

    def guard(self):
        if self.where != "android":
            from riverqa.device import GuardError
            raise GuardError("adb guard: the phone is not in Android")


PROMOTE = (slots.PROMOTE, "proved (test)")


def promote_mocks():
    """The host side of a PROMOTE decision, proved by fiat."""
    from riverqa import recovery, stage
    return (mock.patch.object(recovery, "lookup", return_value={"prs": [], "tags": []}),
            mock.patch.object(slots, "decide_recovery", return_value=PROMOTE),
            mock.patch.object(stage, "repo_slugs", return_value={}),
            mock.patch.object(stage, "load", return_value={"batch": "cand", "build_id": "river-cand"}),
            mock.patch.object(stage, "batch_identity_errors", return_value=[]))


class RecoverHaltContract(unittest.TestCase):
    """R6-1: every failure of flash --recover (and --rollback, and rescue)
    on the phone exits HALT 3 with a clean error line; the fb_mode
    obligation, and any pair, stay as they were. Driven through main."""

    def assertHalted(self, rc, err, *, pair=None):
        self.assertEqual(rc, flashing.EXIT_HALT, err)
        self.assertIn("error: ", err)
        self.assertIn("HALT", err)
        self.assertNotIn("Traceback", err)
        self.assertIsNotNone(fbmode.pending(), "the fb_mode obligation was resolved on a failure")
        if pair is not None:
            self.assertEqual(bool(flashing.load_state()["pair"]), pair)

    def test_no_pair_dev_slot_failed_clear_stays_in_fastboot(self):
        """Sol: no pair, a dev slot active, the clear fails: stay in
        fastboot, switch nothing, wipe nothing, HALT."""
        with bench_home():
            state(pair_kind=None)
            p = FailingPhone(active="b", bootable=(), clear_fails=True)
            fbmode.arm(p)
            rc, err = run_main(p, "--recover")
            self.assertHalted(rc, err, pair=False)
            self.assertEqual(order(p.calls), ["clear"])
            self.assertEqual((p.where, p.active, p.boots), ("fastboot", "b", []))
            self.assertIn("stays in fastboot", err)

    def test_no_pair_set_active_fails(self):
        with bench_home():
            state(pair_kind=None)
            p = FailingPhone(active="a", set_active_fails=True)
            fbmode.arm(p)
            rc, err = run_main(p, "--recover")
            self.assertHalted(rc, err, pair=False)
            self.assertEqual(p.boots, [])

    def test_no_pair_bootloader_timeout(self):
        """A TimeoutError on the way to fastboot (main's catch-all used to
        make it exit 1)."""
        with bench_home():
            state(pair_kind=None)
            p = FailingPhone(where="android", active="a", no_bootloader=True)
            fbmode.arm(p)
            p.where, p.running = "android", "a"
            rc, err = run_main(p, "--recover")
            self.assertHalted(rc, err, pair=False)

    def test_before_the_first_baseline(self):
        """No slot-state.json yet: the generic recovery's failures halt too,
        both a failed clear and a boot that never completes."""
        for kw in ({"clear_fails": True}, {"bootable": ()}):
            with self.subTest(**kw), bench_home():
                p = FailingPhone(active="a", **kw)
                fbmode.arm(p)
                rc, err = run_main(p, "--recover")
                self.assertHalted(rc, err)
                self.assertIsNone(flashing.load_state())

    def test_promote_path_failures(self):
        """A PROMOTE decision whose fb_mode recovery fails (clear) or times
        out (the candidate never boots): HALT, pair and obligation kept."""
        for kw in ({"clear_fails": True}, {"bootable": ()}):
            with self.subTest(**kw), bench_home():
                state(pair_kind="batch")
                p = FailingPhone(active="b", **kw)
                fbmode.arm(p)
                mocks = promote_mocks()
                with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
                    rc, err = run_main(p, "--recover")
                self.assertHalted(rc, err, pair=True)
                self.assertEqual(flashing.load_state()["known_good_slot"], "a")

    def test_rollback_path_failures(self):
        """A ROLLBACK decision: --set-active fails, or adb reboot bootloader
        never reaches fastboot. Nothing is wiped."""
        for kw in ({"set_active_fails": True}, {"no_bootloader": True, "where": "android",
                                                 "bootable": ("a", "b")}):
            with self.subTest(**kw), bench_home():
                state(pair_kind="calibration")
                p = FailingPhone(**kw)
                fbmode.arm(p)
                if p.where == "android":
                    p.running = "b"
                rc, err = run_main(p, "--recover")
                self.assertHalted(rc, err, pair=True)
                self.assertNotIn("erase", order(p.calls))
                self.assertEqual(p.boots, [])

    def test_flash_rollback_set_active_fails(self):
        with bench_home():
            state(pair_kind="calibration")
            p = FailingPhone(set_active_fails=True)
            fbmode.arm(p)
            rc, err = run_main(p, "--rollback")
            self.assertHalted(rc, err, pair=True)
            self.assertNotIn("erase", order(p.calls))

    def test_rescue_raises_halt(self):
        """flash_batch's rescue halts only on Halt, Unreachable and
        GuardError; a fastboot error must arrive as Halt."""
        with bench_home():
            st = state(pair_kind="calibration")
            p = FailingPhone(set_active_fails=True)
            fbmode.arm(p)
            with redirect_stderr(io.StringIO()), self.assertRaises(flashing.Halt):
                flashing.rescue(p, st, "first boot timed out")
            self.assertIsNotNone(fbmode.pending())


class PhoneInFastbootNothingPending(unittest.TestCase):
    """R6-8: a batch flash or --promote that finds the phone in fastboot
    with no fb_mode obligation boots the slot it needs, or halts; it never
    leaves the phone in fastboot for the next adb guard."""

    def test_batch_flash_boots_known_good(self):
        with bench_home():
            state(pair_kind=None)
            p = FailingPhone(active="a", fb=False)
            with mock.patch.object(flashing, "flash_batch", return_value=flashing.EXIT_OK):
                rc, err = run_main(p, "--batch", "x", "--wipe")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertEqual((p.where, p.running, p.boots), ("android", "a", ["a"]))

    def test_batch_flash_halts_on_another_active_slot(self):
        with bench_home():
            state(pair_kind=None)
            p = FailingPhone(active="b", fb=False, bootable=())
            with mock.patch.object(flashing, "flash_batch", side_effect=AssertionError("flashed")):
                rc, err = run_main(p, "--batch", "x", "--wipe")
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertEqual((p.where, p.boots), ("fastboot", []))
            self.assertIn("not slot a", err)

    def test_promote_boots_the_candidate(self):
        with bench_home():
            state(pair_kind="batch")
            p = FailingPhone(active="b", fb=False, bootable=("a", "b"))
            mocks = promote_mocks()
            with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
                rc, err = run_main(p, "--promote", "cand")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertEqual(p.boots, ["b"])
            self.assertEqual(flashing.load_state()["known_good_slot"], "b")

    def test_promote_never_boots_another_slot(self):
        with bench_home():
            state(pair_kind="batch")
            p = FailingPhone(active="a", fb=False)
            mocks = promote_mocks()
            with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
                rc, err = run_main(p, "--promote", "cand")
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertEqual(p.boots, [])
            self.assertIsNotNone(flashing.load_state()["pair"])


class DevFlash(unittest.TestCase):
    def test_dev_flash_never_boots_before_writing(self):
        """R5-16: flash --no-reboot (and every dev flash) recovers with
        boot=False."""
        with bench_home():
            state(pair_kind=None)
            p = SlotPhone(active="b", bootable=())
            fbmode.arm(p)
            with mock.patch.object(flashing, "flash_dev", return_value=0) as fd:
                rc, err = run_flash(p, "/nonexistent-product", "--no-reboot")
            self.assertEqual(rc, 0, err)
            fd.assert_called_once()
            self.assertEqual((p.where, p.boots), ("fastboot", []))
            self.assertIsNone(fbmode.pending())

    def test_dev_flash_with_a_pair_refuses_before_touching_the_phone(self):
        with bench_home():
            state()
            p = SlotPhone()
            fbmode.arm(p)
            rc, err = run_flash(p, "/nonexistent-product")
            self.assertEqual(rc, flashing.EXIT_ERROR)
            self.assertEqual(p.calls, [])
            self.assertIn("unresolved slot pair", err)

    def test_preflight_timeout_halts(self):
        """R5-11: a recovery TimeoutError is a HALT, not EXIT_ERROR. Here
        known_good_slot a is active but hangs on boot."""
        with bench_home():
            state(pair_kind=None)
            p = SlotPhone(active="a", bootable=())
            fbmode.arm(p)
            with mock.patch.object(flashing, "flash_batch", side_effect=AssertionError("flashed")):
                rc, err = run_flash(p, "--batch", "x", "--wipe")
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertIn("error: fb_mode recovery failed", err)
            self.assertIsNotNone(fbmode.pending())

    def test_promote_without_a_pair_never_touches_the_phone(self):
        """R6-8: --promote checks its pair and proof on the host first."""
        with bench_home():
            state(pair_kind=None)
            p = SlotPhone(active="b", bootable=())
            fbmode.arm(p)
            calls = list(p.calls)
            rc, err = run_flash(p, "--promote", "x")
            self.assertEqual(rc, flashing.EXIT_ERROR, err)
            self.assertEqual(p.calls, calls)
            self.assertIsNotNone(fbmode.pending())


if __name__ == "__main__":
    unittest.main()
