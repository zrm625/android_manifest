"""Review round 5, R5-1/R5-16: slot recovery with an fb_mode obligation
pending. It must clear fb_mode in fastboot, set the fallback active and
reboot; it must never boot the active slot first (it may be the failed
candidate), and it resolves the obligation only once the phone runs the
fallback. No adb or fastboot."""
import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from fakes import Boots
from riverqa import fbmode, flashing, slots

BID = "river-good-1"
SHA = "f" * 64


class SlotPhone(Boots):
    """A phone with two slots. ``bootable`` says which slots reach Android;
    a boot of an unbootable slot hangs (neither adb nor fastboot). Every
    action is logged in ``calls``; ``boots`` lists each slot booted."""
    serial = "ZY225DPT7F"

    def __init__(self, where="fastboot", active="b", fb=True, bootable=("a",), clear_fails=False):
        self.where, self.active, self.fb = where, active, fb
        self.bootable, self.clear_fails = set(bootable), clear_fails
        self.running = active if where == "android" else None
        self.calls, self.boots = [], []
        if where == "android":
            self._new_boot()

    # adb
    def state(self):
        return "device" if self.where == "android" else ""

    def guard(self):
        if self.where != "android":
            raise AssertionError("adb guard with the phone not in Android")

    def read(self, cmd, timeout=30):
        from riverqa.device import Read
        return Read(1, "", "no marker in this fake")

    def reboot_adb(self, target=None):
        self.calls.append(("reboot_adb", target))
        if target == "bootloader":
            self.where, self.running = "fastboot", None
        else:
            self._boot()

    def root(self, seconds=60):
        pass

    def getprop(self, name):
        if name == "sys.boot_completed":       # R12-1: a phone in Android has booted
            return "1" if self.where == "android" else ""
        return f"_{self.running}" if name == "ro.boot.slot_suffix" else ""

    def slot_boot_state(self):
        """One shell call: both values from the same boot (R13-1)."""
        return (self.getprop("ro.boot.slot_suffix") or ""), \
            (self.getprop("sys.boot_completed") or "").strip() == "1"

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
        if name.startswith("slot-successful:"):
            return "No"
        if name.startswith("slot-retry-count:"):
            return "6"
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
            self._new_boot()
        else:
            self.where, self.running = "hung", None


def arm(p, slot=None):
    """fbmode.arm with the identity of the slot the fake has active (R8-1)."""
    slot = slot or p.active
    bid = BID if slot == "a" else "river-cand"
    fbmode.arm(p, slot, (bid, bid))


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
            arm(p)
            rc, err = run_flash(p, "--rollback")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.check_rolled_back(p)

    def test_flash_recover_rolls_back(self):
        with bench_home():
            state()
            p = SlotPhone()
            arm(p)
            rc, err = run_flash(p, "--recover")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.check_rolled_back(p)

    def test_candidate_in_android_goes_through_fastboot(self):
        with bench_home():
            state()
            p = SlotPhone(where="android", active="b", fb=True, bootable=("a", "b"))
            arm(p)
            rc, err = run_flash(p, "--rollback")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertEqual(order(p.calls)[0], "reboot_adb bootloader")
            self.assertEqual(p.boots, ["a"])
            self.assertIsNone(fbmode.pending())

    def test_killed_between_clear_and_reboot_resumes(self):
        with bench_home():
            state()
            p = SlotPhone()
            arm(p)
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
            arm(p)
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
            arm(p)
            with redirect_stderr(io.StringIO()):
                flashing.rescue(p, st, "first boot timed out")
            self.assertEqual(p.boots, ["a"])
            self.assertIsNone(fbmode.pending())


class RecoverNoPair(unittest.TestCase):
    def test_known_good_active_clears_then_boots_it(self):
        with bench_home():
            state(pair_kind=None)
            p = SlotPhone(active="a", bootable=("a",))
            arm(p)
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
            arm(p)
            rc, err = run_flash(p, "--recover")
            self.assertEqual(rc, flashing.EXIT_HALT, err)      # R8-2: a confirmed dev slot halts
            self.assertEqual(order(p.calls), ["clear"])
            self.assertEqual((p.where, p.boots, p.fb), ("fastboot", [], False))
            self.assertIn("flash --rollback", err)
            self.assertIsNotNone(fbmode.pending())

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

    def __init__(self, *a, set_active_fails=False, no_bootloader=False, falls_back=False, **kw):
        super().__init__(*a, **kw)
        self.set_active_fails, self.no_bootloader = set_active_fails, no_bootloader
        # R7-1: an unbootable active slot makes the bootloader boot the
        # other one, while fastboot still reported the first one active.
        self.falls_back = falls_back

    def _boot(self):
        if self.falls_back and not self.fb and self.active not in self.bootable:
            other = slots.other(self.active)
            self.boots.append(self.active)
            self.where, self.running = "android", other
            return
        return super()._boot()

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


class StopHere(Exception):
    """Raised by the test where the flash would reserve the pair."""


def batch_inputs(kind="batch", stop=True):
    """A staged batch whose host-side inputs pass, slots.preflight included
    (R8-3: it runs before the phone is touched). With ``stop``,
    slots.reserve (the first step after the verified boot of
    known_good_slot) stops the flash with StopHere, so a test sees what
    recovery did; run_main returns it as ("raised", ...)."""
    from riverqa import stage
    ps = [mock.patch.object(stage, "load", return_value={"batch": "x", "kind": kind, "images": {}}),
          mock.patch.object(flashing, "check_images", return_value={}),
          mock.patch.object(slots, "preflight", return_value=[])]
    if stop:
        ps.append(mock.patch.object(slots, "reserve", side_effect=StopHere("stopped after fb_mode recovery (test)")))
    return ps


DEV_BID = "river-dev"


def dev_inputs():
    """A dev flash's host-side inputs pass (no images on disk). The build
    is stamped DEV_BID (R8-1: the dev flash verifies the boot against it)."""
    from riverqa import stage
    return [mock.patch.object(flashing, "check_images", return_value={}),
            mock.patch.object(stage, "read_prop", side_effect=lambda path, key: DEV_BID if "build_id" in key
                              else None)]


class patches:
    """Enter a list of mock patches as one context."""

    def __init__(self, ps):
        self.ps = ps

    def __enter__(self):
        for p in self.ps:
            p.__enter__()

    def __exit__(self, *exc):
        for p in reversed(self.ps):
            p.__exit__(*exc)


PROMOTE = (slots.PROMOTE, "proved (test)")


def promote_mocks():
    """The host side of a PROMOTE decision, proved by fiat."""
    from riverqa import recovery, stage
    return (mock.patch.object(recovery, "lookup", return_value=recovery.Lookup(recovery.OK, {"prs": [], "tags": []}, None)),
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
            arm(p)
            rc, err = run_main(p, "--recover")
            self.assertHalted(rc, err, pair=False)
            self.assertEqual(order(p.calls), ["clear"])
            self.assertEqual((p.where, p.active, p.boots), ("fastboot", "b", []))
            self.assertIn("stays in fastboot", err)

    def test_no_pair_set_active_fails(self):
        with bench_home():
            state(pair_kind=None)
            p = FailingPhone(active="a", set_active_fails=True)
            arm(p)
            rc, err = run_main(p, "--recover")
            self.assertHalted(rc, err, pair=False)
            self.assertEqual(p.boots, [])

    def test_no_pair_bootloader_timeout(self):
        """A TimeoutError on the way to fastboot (main's catch-all used to
        make it exit 1)."""
        with bench_home():
            state(pair_kind=None)
            p = FailingPhone(where="android", active="a", no_bootloader=True)
            arm(p)
            p.where, p.running = "android", "a"
            rc, err = run_main(p, "--recover")
            self.assertHalted(rc, err, pair=False)

    def test_before_the_first_baseline(self):
        """No slot-state.json yet: the generic recovery's failures halt too,
        both a failed clear and a boot that never completes."""
        for kw in ({"clear_fails": True}, {"bootable": ()}):
            with self.subTest(**kw), bench_home():
                p = FailingPhone(active="a", **kw)
                arm(p)
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
                arm(p)
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
                arm(p)
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
            arm(p)
            rc, err = run_main(p, "--rollback")
            self.assertHalted(rc, err, pair=True)
            self.assertNotIn("erase", order(p.calls))

    def test_rescue_raises_halt(self):
        """flash_batch's rescue halts only on Halt, Unreachable and
        GuardError; a fastboot error must arrive as Halt."""
        with bench_home():
            st = state(pair_kind="calibration")
            p = FailingPhone(set_active_fails=True)
            arm(p)
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
            with patches(batch_inputs()):
                rc, err = run_main(p, "--batch", "x", "--wipe")
            self.assertEqual(rc, "raised", err)
            self.assertIn("stopped after fb_mode recovery", err)
            self.assertEqual((p.where, p.running, p.boots), ("android", "a", ["a"]))

    def test_batch_flash_halts_on_another_active_slot(self):
        with bench_home():
            state(pair_kind=None)
            p = FailingPhone(active="b", fb=False, bootable=())
            with patches(batch_inputs()):
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


class ExhaustedTrialSlot(unittest.TestCase):
    def test_exhausted_slot_is_rewritten_before_activation(self):
        with bench_home(), redirect_stderr(io.StringIO()):
            phone = SlotPhone(active="a")
            getvar = phone.getvar

            def slot_var(name):
                if name == "slot-retry-count:_b" and phone.active == "a":
                    return "0"
                return getvar(name)

            with mock.patch.object(phone, "getvar", side_effect=slot_var):
                flashing.write_images(phone, Path("/images"), "b", False, {}, None)

        writes = [call for call in phone.calls if call[:2] == ("fastboot", "flash")]
        self.assertEqual([call[2] for call in writes], ["boot_b", "dtbo_b", "system_b", "vendor_b"])
        self.assertLess(phone.calls.index(writes[-1]), phone.calls.index(("fastboot", "--set-active=b")))
        self.assertEqual(phone.active, "b")

    def test_still_exhausted_after_writing_reselects_fallback_without_wiping(self):
        with bench_home(), redirect_stderr(io.StringIO()):
            phone = SlotPhone(active="a")
            getvar = phone.getvar

            def slot_var(name):
                return "0" if name == "slot-retry-count:_b" else getvar(name)

            with mock.patch.object(phone, "getvar", side_effect=slot_var):
                with self.assertRaisesRegex(flashing.Halt, "remains unbootable"):
                    flashing.write_images(phone, Path("/images"), "b", True, {}, None)

        self.assertEqual(phone.active, "a")
        self.assertIn(("fastboot", "--set-active=b"), phone.calls)
        self.assertEqual(phone.calls[-1], ("fastboot", "--set-active=a"))
        self.assertNotIn(("fastboot", "erase", "userdata"), phone.calls)
        self.assertEqual(phone.boots, [])

    def test_failed_candidate_boot_with_wipe_returns_to_verified_fallback(self):
        from riverqa import reboots

        def written_then_timeout(dev, path, **kw):
            kw["in_fastboot"]()
            raise TimeoutError("candidate never reached sys.boot_completed")

        with bench_home():
            state(pair_kind=None)
            phone = SlotPhone(active="a")
            with patches(dev_inputs()), mock.patch.object(reboots, "planned_reboot", written_then_timeout):
                rc, err = run_flash(phone, "/nonexistent-product", "--wipe")

        self.assertEqual(rc, flashing.EXIT_RESCUED, err)
        self.assertIn("candidate never reached sys.boot_completed", err)
        self.assertEqual(phone.boots, ["a"])


class DevFlash(unittest.TestCase):
    def test_dev_flash_never_boots_before_writing(self):
        """R5-16: flash --no-reboot (and every dev flash) recovers with
        boot=False, then writes the images."""
        with bench_home():
            state(pair_kind=None)
            p = SlotPhone(active="b", bootable=())
            arm(p)
            with patches(dev_inputs()):
                rc, err = run_flash(p, "/nonexistent-product", "--no-reboot")
            self.assertEqual(rc, 0, err)
            self.assertIn(("fastboot", "flash", "boot_b", "/nonexistent-product/boot.img"), p.calls)
            self.assertEqual((p.where, p.boots), ("fastboot", []))
            # R8-1: nothing booted, so nothing settled.
            self.assertIsNotNone(fbmode.pending())
            self.assertIn("obligation stays pending", err)

    def test_device_bench_flash_never_boots_before_writing(self):
        """R6-4: device-bench --flash with an obligation pending and an
        unbootable dev slot active clears fb_mode without booting, then
        flashes."""
        from riverqa import bench
        with bench_home():
            state(pair_kind=None)
            p = SlotPhone(active="b", bootable=())
            arm(p)
            from riverqa import reboots

            def written_then_stop(dev, path, **kw):
                kw["in_fastboot"]()
                raise TimeoutError("stopped after the writes (test)")
            with mock.patch.object(bench, "Device", lambda *a: p), mock.patch.object(flashing, "Device", lambda *a: p), \
                    patches(dev_inputs()), mock.patch.object(reboots, "planned_reboot", written_then_stop), \
                    redirect_stderr(io.StringIO()) as err:
                rc = bench.main(["stripped", "--flash"])
            # R8-1: the boot never came, so the obligation stays pending: HALT.
            self.assertEqual(rc, flashing.EXIT_HALT, err.getvalue())
            self.assertIn("stopped after the writes", err.getvalue())
            self.assertIn(("fastboot", "--set-active=b"), p.calls)
            self.assertEqual((p.where, p.boots), ("fastboot", []))
            self.assertIsNotNone(fbmode.pending())

    def test_dev_flash_with_a_pair_refuses_before_touching_the_phone(self):
        with bench_home():
            state()
            p = SlotPhone()
            arm(p)
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
            arm(p)
            with patches(batch_inputs()):
                rc, err = run_flash(p, "--batch", "x", "--wipe")
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertIn("error: known_good_slot a could not be verified", err)
            self.assertIsNotNone(fbmode.pending())

    def test_promote_without_a_pair_never_touches_the_phone(self):
        """R6-8: --promote checks its pair and proof on the host first."""
        with bench_home():
            state(pair_kind=None)
            p = SlotPhone(active="b", bootable=())
            arm(p)
            calls = list(p.calls)
            rc, err = run_flash(p, "--promote", "x")
            self.assertEqual(rc, flashing.EXIT_ERROR, err)
            self.assertEqual(p.calls, calls)
            self.assertIsNotNone(fbmode.pending())


class Round7(unittest.TestCase):
    """Round 7: no obligation or pair is resolved until the booted phone
    passed the guarded slot check and the build-ID check (R7-1, R7-8);
    promotion advice keeps the pair (R7-2); a refused flash never touches
    the phone (R7-3); rescue failures print an error line (R7-5)."""

    def assertHaltedIntact(self, rc, err, *, pair):
        self.assertEqual(rc, flashing.EXIT_HALT, err)
        self.assertIn("error: ", err)
        self.assertIn("HALT", err)
        self.assertIsNotNone(fbmode.pending(), "the fb_mode obligation was resolved before verification")
        self.assertEqual(bool(flashing.load_state()["pair"]), pair)

    def promote(self, p, build_id="river-cand", cmd=("--promote", "cand")):
        mocks = promote_mocks()
        from riverqa import stage
        with mocks[0], mocks[1], mocks[2], mock.patch.object(stage, "load", return_value={
                "batch": "cand", "build_id": build_id}), mocks[4]:
            return run_main(p, *cmd)

    def test_promote_bootloader_falls_back(self):
        """R7-1: fastboot reports candidate b active, the bootloader falls
        back to a. At 62f73de the obligation was resolved, then exit 1."""
        for cmd in (("--promote", "cand"), ("--recover",)):
            with self.subTest(cmd=cmd), bench_home():
                state(pair_kind="batch")
                p = FailingPhone(active="b", bootable=("a",), falls_back=True)
                arm(p)
                rc, err = self.promote(p, cmd=cmd)
                self.assertHaltedIntact(rc, err, pair=True)
                self.assertEqual((p.running, p.boots), ("a", ["b"]))
                self.assertEqual(flashing.load_state()["known_good_slot"], "a")
                self.assertIn("do not run flash --rollback", err)

    def test_promote_candidate_build_mismatch(self):
        """R7-1: the candidate boots, but its build ID is not the batch's.
        At 62f73de the obligation was resolved, then exit 1."""
        with bench_home():
            state(pair_kind="batch")
            p = FailingPhone(active="b", bootable=("a", "b"))
            arm(p)
            rc, err = self.promote(p, build_id="river-other")
            self.assertHaltedIntact(rc, err, pair=True)
            self.assertEqual(p.running, "b")
            self.assertIn("river-other", err)

    def test_promote_in_android_on_the_wrong_slot_or_build(self):
        """R7-8 (Gemini): already in Android, nothing pending, on the
        fallback slot or with another build: HALT 3, pair kept (62f73de:
        exit 1)."""
        for running, bid in (("a", "river-cand"), ("b", "river-other")):
            with self.subTest(running=running), bench_home():
                state(pair_kind="batch")
                p = FailingPhone(where="android", active=running, fb=False, bootable=("a", "b"))
                rc, err = self.promote(p, build_id=bid)
                self.assertEqual(rc, flashing.EXIT_HALT, err)
                self.assertIn("error: ", err)
                self.assertEqual(flashing.load_state()["pair"]["batch"], "cand")
                self.assertEqual(flashing.load_state()["known_good_slot"], "a")
                self.assertIn("do not run flash --rollback", err)

    def test_promote_fallback_active_never_advises_rollback(self):
        """R7-2: the fallback is active in fastboot during a promotion. The
        advice must keep the pair: never 'Return ... with flash
        --rollback' (62f73de advised exactly that)."""
        for cmd in (("--promote", "cand"), ("--recover",)):
            with self.subTest(cmd=cmd), bench_home():
                state(pair_kind="batch")
                p = FailingPhone(active="a", bootable=("a", "b"))
                arm(p)
                rc, err = self.promote(p, cmd=cmd)
                self.assertHaltedIntact(rc, err, pair=True)
                self.assertEqual(p.boots, [])
                self.assertNotIn("with flash --rollback", err)
                self.assertNotIn("flash --recover finishes it", err)
                self.assertIn("do not run flash --rollback", err)
                self.assertIn("pair stays reserved", err)

    def test_batch_preflight_bootloader_falls_back(self):
        """R7-1: the batch preflight boots known_good_slot a; the bootloader
        falls back to b. At 62f73de the obligation was resolved and the
        flash went on (here: stopped at slots.preflight, exit 1)."""
        with bench_home():
            state(pair_kind=None)
            p = FailingPhone(active="a", bootable=("b",), falls_back=True)
            arm(p)
            with patches(batch_inputs()):
                rc, err = run_main(p, "--batch", "x", "--wipe")
            self.assertHaltedIntact(rc, err, pair=False)
            self.assertEqual(p.running, "b")
            self.assertNotIn("stopped after fb_mode recovery", err)

    def test_batch_preflight_known_good_build_mismatch(self):
        """R7-1: known_good_slot a boots with another build than the
        known-good record (62f73de: resolved, then exit 1)."""
        with bench_home():
            st = state(pair_kind=None)
            st["known_good"]["build_id"] = "river-good-2"
            flashing.save_state(st)
            p = FailingPhone(active="a", bootable=("a",))
            arm(p)
            with patches(batch_inputs()):
                rc, err = run_main(p, "--batch", "x", "--wipe")
            self.assertHaltedIntact(rc, err, pair=False)
            self.assertIn("river-good-2", err)

    def test_batch_preflight_wrong_slot_advice(self):
        """R7-2: with no pair, the batch preflight's wrong-slot halt points
        at flash --rollback, which is right there."""
        with bench_home():
            state(pair_kind=None)
            p = FailingPhone(active="b", bootable=())
            arm(p)
            with patches(batch_inputs()):
                rc, err = run_main(p, "--batch", "x", "--wipe")
            self.assertHaltedIntact(rc, err, pair=False)
            self.assertIn("flash --rollback returns the phone to known_good_slot a", err)

    def test_no_pair_dev_slot_advice_and_obligation(self):
        """R7-2 (Opus minor 2): with no pair and a dev slot active, the
        halt never suggests flash --recover. R7-1: a clear that succeeded
        but booted nothing leaves the obligation for flash --rollback."""
        with bench_home():
            state(pair_kind=None)
            p = FailingPhone(active="b", bootable=(), clear_fails=True)
            arm(p)
            rc, err = run_main(p, "--recover")
            self.assertHaltedIntact(rc, err, pair=False)
            self.assertNotIn("flash --recover", err)
            self.assertIn("flash --rollback", err)
        with bench_home():
            state(pair_kind=None)
            p = FailingPhone(active="b", bootable=("a",))
            arm(p)
            rc, err = run_main(p, "--recover")
            self.assertEqual(rc, flashing.EXIT_HALT, err)      # R8-2
            self.assertEqual((p.where, p.boots), ("fastboot", []))
            self.assertIsNotNone(fbmode.pending())
            self.assertNotIn("flash --recover", err)
            # flash --rollback then resolves it, once slot a is verified.
            rc, err = run_main(p, "--rollback")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertEqual(p.running, "a")
            self.assertIsNone(fbmode.pending())

    def test_device_bench_flash_with_a_pair_never_touches_the_phone(self):
        """R7-3: device-bench --flash with a reserved pair and an obligation
        pending refuses before fb_mode recovery. At 62f73de the phone was
        taken to fastboot and the obligation resolved first."""
        from riverqa import bench
        with bench_home():
            state(pair_kind="calibration")
            p = SlotPhone(where="android", active="a", fb=False)
            arm(p)
            p.calls.clear()
            with mock.patch.object(bench, "Device", lambda *a: p), mock.patch.object(flashing, "Device", lambda *a: p), \
                    patches(dev_inputs()), redirect_stderr(io.StringIO()) as err:
                try:
                    rc = bench.main(["stripped", "--flash"])
                except SystemExit as e:
                    rc = str(e)
            self.assertIn("unresolved slot pair", str(rc) + err.getvalue())
            self.assertEqual((p.calls, p.where), ([], "android"))
            self.assertIsNotNone(fbmode.pending())
            self.assertIsNotNone(flashing.load_state()["pair"])

    def test_bad_flash_inputs_never_touch_the_phone(self):
        """R7-3: a dev flash or a batch flash whose images fail their check
        refuses before fb_mode recovery (62f73de recovered first: the
        phone went to fastboot, and the batch case booted it)."""
        from riverqa import stage
        bad = mock.patch.object(flashing, "check_images", side_effect=SystemExit("error: bad image (test)"))
        for argv, extra in ((("/nonexistent-product",), []),
                            (("--batch", "x", "--wipe"),
                             [mock.patch.object(stage, "load", return_value={"batch": "x", "kind": "batch",
                                                                            "images": {}})])):
            with self.subTest(argv=argv), bench_home():
                state(pair_kind=None)
                p = SlotPhone(where="android", active="a", fb=False)
                arm(p)
                p.calls.clear()
                with patches([bad] + extra):
                    try:
                        rc, err = run_flash(p, *argv)
                    except SystemExit as e:
                        rc, err = str(e), ""
                self.assertIn("bad image", str(rc) + err)
                self.assertEqual((p.calls, p.where), ([], "android"))
                self.assertIsNotNone(fbmode.pending())


if __name__ == "__main__":
    unittest.main()
