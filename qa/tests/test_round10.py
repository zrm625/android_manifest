"""Review round 10, all on the bootstrap before the first baseline or the
dev flash. R10-1: device.mk's ``unstamped`` sentinel is no stamp, on either
partition. R10-2: the bootstrap docs. R10-3: before the first baseline, no
flash leaves the armed slot as the next write target. R10-4: clean exit
codes in the dev flash's slot read and in --no-reboot. R10-5: a failed
erase after --set-active halts. R10-6: SlotUnreadable names its read;
settle refuses a bool stamp. R10-7: the --no-reboot note.

The fakes are those of round 9, so these regressions also run at the
round-9 code (ceb1574). No adb or fastboot."""
import io
import re
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from riverqa import fbmode, flashing, reboots
from riverqa.core import host_boot_id
from riverqa.device import GuardError, Unreachable
from test_round8 import DEV_BID, arm_entry
from test_round9 import R9Phone, booting_reboot, dev_flash, flashes
from test_slot_recovery import BID, SHA, batch_inputs, order, patches, run_main, state

SENTINEL = "unstamped"
ROOT = Path(__file__).resolve().parents[2]


class R10Phone(R9Phone):
    """R9Phone whose build.prop gives device.mk's sentinel on one partition
    (``sentinel`` is "system", "vendor" or "both")."""

    def __init__(self, *a, sentinel=None, **kw):
        self.sentinel = sentinel
        super().__init__(*a, **kw)

    def sh(self, cmd, timeout=30):
        if self.sentinel and "build_id" in cmd:
            part = "vendor" if "vendor" in cmd else "system"
            if self.sentinel in (part, "both"):
                self._note(cmd)
                key = "ro.vendor.river.build_id" if part == "vendor" else "ro.river.build_id"
                return f"{key}={SENTINEL}\n"
        return super().sh(cmd, timeout)

    def fastboot_continue(self):
        raise AssertionError(f"fb_mode was armed ({fbmode.pending()}) and the phone booted on")


def recover_cmd(p):
    """phone-qa recover on the fake phone."""
    from riverqa import qa
    out = io.StringIO()
    with mock.patch.object(qa, "Device", lambda *a: p), redirect_stderr(out), redirect_stdout(out):
        return qa.main(["recover"]), out.getvalue()


def boot_timing(p):
    """phone-qa boot-timing and controls --boot-timing on the fake phone;
    any calibration reboot or measurement raises."""
    from riverqa import calibrate
    one = mock.Mock(side_effect=AssertionError("a calibration reboot"))
    with mock.patch.object(calibrate, "Device", lambda *a: p), mock.patch.object(calibrate, "one", one), \
            mock.patch.object(calibrate, "UsbWatcher"), mock.patch.object(calibrate, "prove", one), \
            mock.patch.object(calibrate, "boot_count_now", one), \
            mock.patch.object(reboots, "load_timing", return_value={"paths": {"sysrq": {}}}), \
            mock.patch.object(calibrate, "record_problems", return_value=[]), \
            redirect_stderr(io.StringIO()) as err:
        try:
            rcs = (calibrate.boot_timing_main([]), calibrate.prove_main())
        except AssertionError as e:          # the old code went on to measure
            rcs = ("measured", str(e))
    return rcs, err.getvalue(), one.call_count


def writes_to(p, slot):
    return [c[2] for c in flashes(p.calls) if c[2].endswith(f"_{slot}")]


class R10_1Sentinel(unittest.TestCase):
    """device.mk sets ``RIVER_BUILD_ID ?= unstamped``. At ceb1574 the
    sentinel counted as a stamp."""

    def test_partition_reads_map_the_sentinel_to_none(self):
        for part, want in (("system", (None, BID)), ("vendor", (BID, None)), ("both", (None, None))):
            with self.subTest(part=part), bench_home():
                p = R10Phone(where="android", active="a", bootable=("a",), fb=False, sentinel=part)
                self.assertEqual(fbmode.partition_build_ids(p), want)
                with self.assertRaises(fbmode.Unstamped):
                    fbmode.running_identity(p)

    def test_first_baseline_refuses_a_sentinel_on_either_partition(self):
        """Nothing pending, no slot-state.json: exit 1, nothing written. At
        ceb1574 the flash went on to reserve the pair."""
        for part in ("system", "vendor"):
            with self.subTest(part=part), bench_home():
                p = R10Phone(where="android", active="a", bootable=("a",), fb=False, sentinel=part)
                with patches(batch_inputs(kind="baseline")):
                    rc, err = run_main(p, "--batch", "x", "--wipe")
                self.assertEqual(rc, flashing.EXIT_ERROR, err)
                self.assertIn("running build is unstamped", err)
                self.assertIsNone(flashing.load_state())
                self.assertEqual((p.boots, order(p.calls)), ([], []))

    def test_calibration_refuses_a_sentinel_on_either_partition(self):
        """At ceb1574 calibration began measuring."""
        for part in ("system", "vendor"):
            with self.subTest(part=part), bench_home():
                p = R10Phone(where="android", active="a", bootable=("a",), fb=False, sentinel=part)
                rcs, err, measured = boot_timing(p)
                self.assertEqual(rcs, (1, 1), err)
                self.assertEqual(err.count("running build is unstamped"), 2)
                self.assertEqual((measured, p.calls, p.boots), (0, [], []))

    def test_arming_refuses_a_sentinel_on_either_partition(self):
        """At ceb1574 fb_mode was armed with the sentinel as an identity."""
        for part in ("system", "vendor"):
            with self.subTest(part=part), bench_home():
                p = R10Phone(where="android", active="a", bootable=("a",), fb=False, sentinel=part)
                with self.assertRaisesRegex(RuntimeError, "cannot arm fb_mode: running build is unstamped"):
                    reboots.arm_bootloader_stop(p, 10)
                self.assertEqual((p.calls, fbmode.pending()), ([], None))
                self.assertFalse(p.fb)
            with self.subTest(direct=part), bench_home():
                with self.assertRaises(ValueError):
                    fbmode.arm(p, "a", (BID, SENTINEL) if part == "vendor" else (SENTINEL, BID))
                self.assertIsNone(fbmode.pending())

    def test_product_want_treats_the_sentinel_as_no_stamp(self):
        """A product with the sentinel on one partition is verified by its
        boot hash. At ceb1574 the Want carried the sentinel as a build ID."""
        from riverqa import stage
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "boot.img").write_bytes(b"boot")
            for sid, vid in ((SENTINEL, DEV_BID), (DEV_BID, SENTINEL), ("", DEV_BID)):
                with self.subTest(system=sid, vendor=vid):
                    props = {"ro.river.build_id": sid, "ro.vendor.river.build_id": vid}
                    with mock.patch.object(stage, "read_prop", side_effect=lambda path, key: props[key]):
                        want = flashing.product_want(Path(d), "b")
                    self.assertIsNone(want.build_ids)
                    self.assertEqual(want.boot_bytes, 4)

    def test_an_existing_sentinel_entry_is_like_a_pre_round8_one(self):
        """An obligation armed (before this fix) with the sentinel identity.
        Generic recovery and the first-baseline flash refuse it before any
        phone call; at ceb1574 they verified the sentinel against the phone
        and settled. A dev flash's own proof settles it, and so does a
        known-good proof."""
        for armed in ((SENTINEL, SENTINEL), (BID, SENTINEL), (SENTINEL, BID)):
            with self.subTest(path="phone-qa recover", armed=armed), bench_home():
                p = R10Phone(where="fastboot", active="a", bootable=("a",), sentinel="both")
                arm_entry("a", armed)
                rc, out = recover_cmd(p)
                self.assertEqual((p.calls, p.boots), ([], []), out)
                self.assertEqual(rc, 3, out)
                self.assertIn("no recorded slot and build IDs", out)
                self.assertIsNotNone(fbmode.pending())
            with self.subTest(path="first baseline", armed=armed), bench_home():
                p = R10Phone(where="fastboot", active="a", bootable=("a",), sentinel="both")
                arm_entry("a", armed)
                with patches(batch_inputs(kind="baseline")):
                    rc, err = run_main(p, "--batch", "x", "--wipe")
                self.assertEqual(rc, flashing.EXIT_HALT, err)
                self.assertEqual((p.calls, p.boots), ([], []))
                self.assertIsNone(flashing.load_state())
                self.assertIsNotNone(fbmode.pending())
        with self.subTest(path="dev flash"), bench_home():
            p = R9Phone(where="fastboot", active="a", bootable=("a", "b"), builds={"a": BID, "b": DEV_BID})
            arm_entry("a", (SENTINEL, SENTINEL))
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertEqual(writes_to(p, "a"), [])
            self.assertIsNone(fbmode.pending())
        with self.subTest(path="known-good proof"), bench_home():
            state(pair_kind=None)
            p = R9Phone(where="fastboot", active="a", bootable=("a",))
            arm_entry("a", (SENTINEL, SENTINEL))
            rc, err = run_main(p, "--recover")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertIsNone(fbmode.pending())


class R10_2Docs(unittest.TestCase):
    """The bootstrap's step 0 is a wiping dev flash, and the docs name the
    device PR with the build-ID stamp. At ceb1574 step 0 was `flash
    stripped` without --wipe."""

    def section(self, path, start, end):
        text = (ROOT / path).read_text()
        self.assertIn(start, text, path)
        m = re.search(re.escape(start) + r"(.*?)" + end, text, re.S | re.M)
        self.assertIsNotNone(m, f"{path}: nothing after {start!r}")
        return " ".join(m.group(1).split())

    def test_step0_wipes_and_names_the_stamp_pr(self):
        for path, start, end in (("qa/README.md", "## Calibration and bootstrap", r"^1\. "),
                                 ("workspace/AGENTS.md", "## Install on the phone", r"^## ")):
            with self.subTest(path=path):
                s = self.section(path, start, end)
                self.assertIn("flash stripped --wipe", s)
                self.assertIn("PR #7", s)
                self.assertIn("9851f11", s)


class R10_3PreBaseline(unittest.TestCase):
    """No slot-state.json, an obligation armed on slot a (the only slot
    known to boot)."""

    def test_no_reboot_is_refused_and_recovery_leaves_a_untouched(self):
        """Sol's reproduction. At ceb1574 `flash stripped --no-reboot` wrote
        slot b and left it active with the obligation pending; the next
        flash then wrote slot a. Now the flash is refused before the phone
        is touched, and the prescribed recovery (phone-qa recover) boots,
        verifies and settles slot a without writing it."""
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a",), fb=False, builds={"a": BID, "b": DEV_BID})
            arm_entry("a")
            rc, err = dev_flash(p, "--no-reboot")
            self.assertEqual(rc, flashing.EXIT_ERROR, err)
            self.assertIn("flash --no-reboot is refused", err)
            self.assertIn("run phone-qa recover", err)
            self.assertNotIn("--rollback", err)
            self.assertEqual((p.calls, p.where), ([], "android"))
            self.assertIsNotNone(fbmode.pending())
            rc, out = recover_cmd(p)
            self.assertEqual(rc, 0, out)
            self.assertIsNone(fbmode.pending())
            self.assertEqual((writes_to(p, "a"), p.running), ([], "a"))

    def test_a_flash_that_would_write_the_armed_slot_is_refused(self):
        """Slot b active (an earlier flash stopped after --set-active), in
        fastboot or booted: the flash would write slot a. At ceb1574 it
        wrote boot_a. Now HALT 3 before anything is cleared or written, and
        the advice (set a active, phone-qa recover) leaves slot a
        untouched."""
        for where in ("fastboot", "android"):
            with self.subTest(where=where), bench_home():
                p = R9Phone(where=where, active="b", bootable=("a", "b"), fb=where == "fastboot",
                            builds={"a": BID, "b": DEV_BID})
                arm_entry("a")
                rc, err = dev_flash(p, reboot=booting_reboot)
                self.assertEqual(rc, flashing.EXIT_HALT, err)
                self.assertIn("where the pending fb_mode obligation was armed", err)
                self.assertIn("--set-active=a", err)
                self.assertIn("phone-qa recover", err)
                self.assertNotIn("--rollback", err)
                self.assertEqual((flashes(p.calls), order(p.calls)), ([], []))
                self.assertIsNotNone(fbmode.pending())
                # The advice: set a active by hand, then phone-qa recover.
                if where == "android":
                    p.reboot_adb("bootloader")
                p.active = "a"
                rc, out = recover_cmd(p)
                self.assertEqual(rc, 0, out)
                self.assertIsNone(fbmode.pending())
                self.assertEqual((writes_to(p, "a"), p.running), ([], "a"))

    def test_halts_after_the_write_never_advise_rollback_or_a_flash(self):
        """A boot timeout or failed verify of slot b before the first
        baseline: the advice is phone-qa recover, never flash --rollback
        (no slot-state.json) or another flash (it would write slot a)."""
        def timeout(dev, path, *, boot_timeout, in_fastboot=None, **kw):
            in_fastboot()
            raise TimeoutError("no boot completed")
        for case, reboot, builds in (("timeout", timeout, {"a": BID, "b": DEV_BID}),
                                     ("verify", booting_reboot, {"a": BID, "b": "river-other"})):
            with self.subTest(case=case), bench_home():
                p = R9Phone(where="android", active="a", bootable=("a", "b"), fb=False, builds=builds)
                arm_entry("a")
                rc, err = dev_flash(p, reboot=reboot)
                self.assertEqual(rc, flashing.EXIT_HALT, err)
                self.assertIn("phone-qa recover", err)
                self.assertNotIn("--rollback", err)
                self.assertNotIn("Flash again", err)
                self.assertIsNotNone(fbmode.pending())


class R10_4ExitCodes(unittest.TestCase):
    def test_guard_failure_in_the_slot_read_with_an_obligation_halts(self):
        """At ceb1574 main caught the GuardError: exit 1."""
        with bench_home():
            state(pair_kind=None)
            p = R9Phone(where="android", active="a", bootable=("a", "b"), fb=False)
            p.guard = mock.Mock(side_effect=GuardError("adb -s ZY225DPT7F reached a phone with serial XYZ"))
            arm_entry("a")
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertIn("serial XYZ", err)
            self.assertEqual(p.calls, [])
            self.assertIsNotNone(fbmode.pending())

    def test_pre_baseline_slot_timeout_halts(self):
        """No slot-state.json, nothing pending, getvar times out. At ceb1574:
        exit 1."""
        with bench_home():
            p = R9Phone(where="fastboot", active="a", bootable=("a", "b"))
            p.getvar = mock.Mock(side_effect=TimeoutError("fastboot getvar: timed out after 20 s"))
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertIn("timed out", err)
            self.assertEqual(order(p.calls), [])

    def test_unreachable_in_no_reboot_halts(self):
        """to_fastboot gives up in --no-reboot. At ceb1574 write_failed
        caught the Unreachable: exit 1."""
        with bench_home():
            state(pair_kind=None)
            p = R9Phone(where="fastboot", active="a", bootable=("a", "b"))
            real, n = flashing.to_fastboot, []

            def to_fastboot(dev, seconds=180):
                n.append(seconds)
                if len(n) > 1:
                    raise Unreachable("ZY225DPT7F: neither adb nor fastboot answers.")
                return real(dev, seconds)
            with mock.patch.object(flashing, "to_fastboot", to_fastboot):
                rc, err = dev_flash(p, "--no-reboot")
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertIn("HALT: ZY225DPT7F: neither adb nor fastboot answers", err)
            self.assertEqual(flashes(p.calls), [])

    def test_failed_adb_read_before_the_clear_is_a_clean_error(self):
        """At ceb1574 the RuntimeError escaped main as a traceback."""
        with bench_home():
            state(pair_kind=None)
            p = R9Phone(where="android", active="a", bootable=("a", "b"), fb=False)
            p.getprop = mock.Mock(side_effect=RuntimeError("adb: device offline"))
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_ERROR, err)
            self.assertIn("error: cannot read the active slot: adb: device offline", err)
            self.assertEqual(p.calls, [])


    def test_device_bench_flash_halts_on_unreachable(self):
        """Found in the self-review: flash_dev lets Unreachable through to
        its caller (R10-4), and device-bench --flash halts on it as
        scripts/flash does. At ceb1574 it escaped device-bench as a
        traceback."""
        from riverqa import bench, stage
        with bench_home():
            p = R9Phone(where="hung", active="a", bootable=("a", "b"), fb=False)

            def unreachable(dev, seconds=180):
                raise Unreachable("ZY225DPT7F: neither adb nor fastboot answers.")
            ps = [mock.patch.object(flashing, "Device", lambda *a: p), mock.patch.object(bench, "Device", lambda *a: p),
                  mock.patch.object(flashing, "to_fastboot", unreachable),
                  mock.patch.object(flashing, "check_images", return_value={}),
                  mock.patch.object(stage, "read_prop", return_value=None)]
            with patches(ps), redirect_stderr(io.StringIO()) as err:
                try:
                    rc = bench.main(["stripped", "--flash"])
                except Exception as e:        # noqa: BLE001
                    rc = ("raised", repr(e))
            self.assertEqual(rc, flashing.EXIT_HALT, err.getvalue())
            self.assertIn("HALT: ZY225DPT7F: neither adb nor fastboot answers", err.getvalue())


class R10_5EraseFailure(unittest.TestCase):
    def test_erase_failure_after_set_active_halts(self):
        """Nothing pending: slot b is active and unwiped. At ceb1574: exit
        1."""
        for argv, reboot in ((("--wipe", "--no-reboot"), None), (("--wipe",), booting_reboot)):
            for st in (True, False):
                with self.subTest(argv=argv, state=st), bench_home():
                    if st:
                        state(pair_kind=None)
                    p = R9Phone(where="fastboot", active="a", bootable=("a", "b"))
                    real = p.fastboot

                    def fastboot(*args, timeout=120, real=real):
                        if args[:2] == ("erase", "userdata"):
                            return mock.Mock(returncode=1, stdout="", stderr="FAILED (remote: 'denied')")
                        return real(*args, timeout=timeout)
                    p.fastboot = fastboot
                    rc, err = dev_flash(p, *argv, reboot=reboot)
                    self.assertEqual(rc, flashing.EXIT_HALT, err)
                    self.assertIn("Slot b is active and userdata was not erased", err)
                    self.assertIn("--set-active=a", err)
                    self.assertEqual(p.active, "b")


class R10_6Minors(unittest.TestCase):
    def test_slot_unreadable_names_getprop(self):
        """No slot-state.json, the phone in Android, getprop gives nothing.
        At ceb1574 the message said "fastboot reported ''"."""
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a", "b"), fb=False)
            p.getprop = lambda name: ""
            rc, err = dev_flash(p, reboot=booting_reboot)
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertIn("adb -s ZY225DPT7F shell getprop ro.boot.slot_suffix", err)
            self.assertNotIn("fastboot reported", err)

    def test_settle_refuses_a_bool_stamp(self):
        """At ceb1574 True passed as a CLOCK_BOOTTIME of 1 s."""
        with bench_home():
            p = R9Phone(where="android", active="a", bootable=("a",), fb=False)
            arm_entry("a")
            j = fbmode.journal()
            j.annotate(fbmode.pending(j), cleared_at={"boottime": True, "host_boot_id": host_boot_id(),
                                                      "wall": time.time()})
            with self.assertRaisesRegex(fbmode.NotSettled, "no host boot clock"):
                fbmode.settle(fbmode.verify_boot(p, fbmode.Want("a", (BID, BID))))
            self.assertIsNotNone(fbmode.pending())


class R10_7NoRebootNote(unittest.TestCase):
    def test_no_reboot_note_says_whether_generic_recovery_settles(self):
        """With a known-good slot, --no-reboot with an obligation pending.
        Armed on the slot and build written: generic recovery settles it,
        and does (at ceb1574 the note said phone-qa --quick halts). Armed
        on another slot: the warning."""
        for armed, settles in ((("b", (DEV_BID, DEV_BID)), True), (("a", (BID, BID)), False)):
            with self.subTest(armed=armed[0]), bench_home():
                state(pair_kind=None)
                p = R9Phone(where="fastboot", active="b", bootable=("a", "b"), builds={"a": BID, "b": DEV_BID})
                arm_entry(*armed)
                rc, err = dev_flash(p, "--no-reboot")
                self.assertEqual(rc, flashing.EXIT_OK, err)
                self.assertIsNotNone(fbmode.pending())
                if settles:
                    self.assertIn("generic recovery (phone-qa recover", err)
                    self.assertNotIn("halts", err)
                    rc, out = recover_cmd(p)
                    self.assertEqual(rc, 0, out)
                    self.assertIsNone(fbmode.pending())
                else:
                    self.assertIn("phone-qa --quick halts (exit 3)", err)


if __name__ == "__main__":
    unittest.main()
