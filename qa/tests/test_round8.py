"""Review round 8. R8-1: one proof type (fbmode.VerifiedBoot, made only by
verify_boot) and one way to settle an fb_mode obligation (fbmode.settle,
which requires it). R8-2: an unreadable active slot is never a wrong slot.
R8-3: host-only prerequisites before any phone call. R8-4: halt messages.

The obligation is set up by writing the journal entry directly (arm_entry),
so the regressions also run against the round-7 code (1897d45). No adb or
fastboot."""
import ast
import io
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from riverqa import fbmode, flashing, journal as jmod, reboots, slots
from riverqa.device import GuardError
from test_slot_recovery import (BID, FailingPhone, SlotPhone, StopHere, batch_inputs, order, patches,
                                promote_mocks, run_main, state)

DEV_BID = "river-dev"
LIB = Path(__file__).resolve().parents[1] / "lib" / "riverqa"


def arm_entry(slot="a", ids=(BID, BID)):
    """A pending obligation armed on ``slot`` with build ``ids``, as
    fbmode.arm records it (R8-1)."""
    fbmode.journal().record(jmod.HOST, "fb_mode", "fb_mode", "clear", extra={
        "serial": "ZY225DPT7F", "armed_at": "t0", "slot": slot, "build_ids": list(ids), "cleared_at": None})


class R8Phone(FailingPhone):
    """FailingPhone with a build per slot, reads that time out, a guard
    that fails once Android is back, a getvar that can't read the slot,
    and a log of every read made after the obligation was resolved."""

    def __init__(self, *a, builds=None, timeout_on=None, guard_fails_after_boot=False, slot_unreadable=False,
                 **kw):
        super().__init__(*a, **kw)
        self.builds = builds or {"a": BID, "b": "river-cand"}
        self.timeout_on = timeout_on               # (regex, nth)
        self.hits = 0
        self.guard_fails_after_boot = guard_fails_after_boot
        self.slot_unreadable = slot_unreadable
        self.after_settle = []

    def _note(self, what):
        if fbmode.pending() is None:
            self.after_settle.append(what)

    def guard(self):
        self._note("guard")
        if self.guard_fails_after_boot and self.boots:
            raise GuardError("adb -s ZY225DPT7F reached a phone with serial XYZ. Refusing.")
        super().guard()

    def getprop(self, name):
        self._note(f"getprop {name}")
        return super().getprop(name)

    def getvar(self, name):
        if name == "current-slot" and self.slot_unreadable:
            return ""
        return super().getvar(name)

    def sh(self, cmd, timeout=30):
        self._note(cmd)
        if self.timeout_on and re.search(self.timeout_on[0], cmd):
            self.hits += 1
            if self.hits == self.timeout_on[1]:
                raise TimeoutError(f"timed out: {cmd}")
        if "build_id" in cmd:
            key = "ro.vendor.river.build_id" if "vendor" in cmd else "ro.river.build_id"
            return f"{key}={self.builds.get(self.running)}\n"
        return super().sh(cmd)


def promote(p, cmd=("--promote", "cand")):
    m = promote_mocks()
    with m[0], m[1], m[2], m[3], m[4]:
        return run_main(p, *cmd)


class Round8Majors(unittest.TestCase):
    def assertPending(self, rc, err, *, pair):
        self.assertEqual(rc, flashing.EXIT_HALT, err)
        self.assertIn("HALT", err)
        self.assertIsNotNone(fbmode.pending(), "the fb_mode obligation was resolved")
        self.assertEqual(bool(flashing.load_state()["pair"]), pair)

    # Sol 1: promotion
    def test_promote_read_timeout_keeps_pair_and_obligation(self):
        """A timeout on any verification read (system build ID, vendor build
        ID, the boot hash) halts with the pair reserved and the obligation
        pending. At 1897d45 the boot-hash timeout came after the settle."""
        for on in (("ro.river.build_id", 1), ("ro.vendor.river.build_id", 1), ("sha256sum", 1)):
            with self.subTest(on=on[0]), bench_home():
                state(pair_kind="batch")
                p = R8Phone(active="b", bootable=("a", "b"), timeout_on=on)
                arm_entry("b", ("river-cand", "river-cand"))
                rc, err = promote(p)
                self.assertPending(rc, err, pair=True)
                self.assertIn("timed out", err)

    def test_promote_reads_nothing_after_settling(self):
        """The promotion uses the values on the proof: no verification read
        after the obligation is resolved (1897d45 read the slot, both build
        IDs and the boot hash again after it)."""
        with bench_home():
            state(pair_kind="batch")
            p = R8Phone(active="b", bootable=("a", "b"))
            arm_entry("b", ("river-cand", "river-cand"))
            rc, err = promote(p)
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertEqual(flashing.load_state()["known_good_slot"], "b")
            self.assertEqual(p.after_settle, [])

    # Sol 2: batch preflight
    def test_batch_preflight_hash_timeout_keeps_obligation(self):
        """The known-good boot hash is part of the proof: a timeout halts
        (3) with the obligation pending. At 1897d45 the hash was read after
        the settle, and the TimeoutError ended in exit 1."""
        with bench_home():
            state(pair_kind=None)
            p = R8Phone(active="a", bootable=("a",), timeout_on=("sha256sum", 1))
            arm_entry("a")
            with patches(batch_inputs()):
                rc, err = run_main(p, "--batch", "x", "--wipe")
            self.assertPending(rc, err, pair=False)

    def test_batch_preflight_reads_nothing_after_settling(self):
        with bench_home():
            state(pair_kind=None)
            p = R8Phone(active="a", bootable=("a",))
            arm_entry("a")
            with patches(batch_inputs()):
                rc, err = run_main(p, "--batch", "x", "--wipe")
            self.assertEqual(rc, "raised", err)          # StopHere at slots.reserve
            self.assertIn("stopped after fb_mode recovery", err)
            self.assertIsNone(fbmode.pending())
            self.assertEqual(p.after_settle, [])

    # R8-1: generic recovery verifies the armed identity
    def recover_cmd(self, p):
        from riverqa import qa
        out = io.StringIO()
        with mock.patch.object(qa, "Device", lambda *a: p), redirect_stderr(out), redirect_stdout(out):
            return qa.main(["recover"]), out.getvalue()

    def test_generic_recovery_checks_the_armed_build(self):
        """phone-qa recover: the armed slot boots, but with another build:
        HALT 3, obligation pending (1897d45: exit 0, settled)."""
        with bench_home():
            p = R8Phone(active="a", bootable=("a",), builds={"a": "river-other"})
            arm_entry("a", (BID, BID))
            rc, out = self.recover_cmd(p)
            self.assertEqual(rc, 3, out)
            self.assertIn(f"expected {BID}/{BID}", out)
            self.assertIsNotNone(fbmode.pending())

    def test_generic_recovery_refuses_another_active_slot(self):
        """Armed on slot a, fastboot reports b: HALT 3 before anything is
        cleared or booted (1897d45: booted b, settled)."""
        with bench_home():
            p = R8Phone(active="b", bootable=("a", "b"))
            arm_entry("a")
            rc, out = self.recover_cmd(p)
            self.assertEqual(rc, 3, out)
            self.assertEqual((p.boots, order(p.calls)), ([], []))
            self.assertIsNotNone(fbmode.pending())

    # R8-1: no clear-only settlement in a dev flash
    def dev_flash(self, p, *argv, reboot=None):
        from riverqa import stage
        ps = [mock.patch.object(flashing, "check_images", return_value={}),
              mock.patch.object(stage, "read_prop", side_effect=lambda path, key: DEV_BID if "build_id" in key
                                else None)]
        if reboot is not None:
            ps.append(mock.patch.object(reboots, "planned_reboot", reboot))
        with patches(ps):
            return run_main(p, "/nonexistent-product", *argv)

    @staticmethod
    def booting_reboot(dev, path, *, boot_timeout, in_fastboot=None, **kw):
        in_fastboot()
        dev.reboot_fastboot()
        return mock.Mock(host_boot_seconds=42.0)

    def test_dev_flash_no_reboot_leaves_the_obligation(self):
        with bench_home():
            state(pair_kind=None)
            p = R8Phone(active="b", bootable=())
            arm_entry("a")
            rc, err = self.dev_flash(p, "--no-reboot")
            self.assertEqual(rc, flashing.EXIT_OK, err)
            self.assertIn(("fastboot", "flash", "boot_b", "/nonexistent-product/boot.img"), p.calls)
            self.assertIsNotNone(fbmode.pending())

    def test_dev_flash_killed_before_the_first_write_leaves_the_obligation(self):
        with bench_home():
            state(pair_kind=None)
            p = R8Phone(active="b", bootable=())
            arm_entry("a")
            real = p.fastboot_ok

            def killed(*args, **kw):
                if args[0] == "flash":
                    raise KeyboardInterrupt
                return real(*args, **kw)
            p.fastboot_ok = killed
            with self.assertRaises(KeyboardInterrupt):
                self.dev_flash(p)
            self.assertIn(("fb_mode_clear",), p.calls)
            self.assertIsNotNone(fbmode.pending())

    def test_dev_flash_settles_only_with_a_verified_boot(self):
        for falls_back, want_rc in ((False, flashing.EXIT_OK), (True, flashing.EXIT_HALT)):
            with self.subTest(falls_back=falls_back), bench_home():
                state(pair_kind=None)
                p = R8Phone(active="a", bootable=("a",) if falls_back else ("a", "b"), falls_back=falls_back,
                            builds={"a": BID, "b": DEV_BID})
                arm_entry("a")
                rc, err = self.dev_flash(p, reboot=self.booting_reboot)
                self.assertEqual(rc, want_rc, err)
                self.assertEqual(fbmode.pending() is None, not falls_back, err)

    # Gemini g2: the post-boot guard on rollback and known-good recovery
    def test_rollback_and_known_good_recovery_guard_the_booted_phone(self):
        for cmd, pair_kind, active in ((("--rollback",), "calibration", "b"), (("--recover",), None, "a")):
            with self.subTest(cmd=cmd), bench_home():
                state(pair_kind=pair_kind)
                p = R8Phone(active=active, bootable=("a",), guard_fails_after_boot=True)
                arm_entry("a")
                rc, err = run_main(p, *cmd)
                self.assertEqual(rc, flashing.EXIT_HALT, err)
                self.assertIn("Refusing", err)
                self.assertIsNotNone(fbmode.pending())

    # R8-2 (Sol 3): an unreadable active slot
    def test_unreadable_active_slot_halts_without_rollback_advice(self):
        msg = "cannot read the active slot; recheck with `fastboot -s ZY225DPT7F getvar current-slot`"
        for cmd in (("--batch", "x", "--wipe"), ("--recover",)):
            with self.subTest(cmd=cmd[0]), bench_home():
                state(pair_kind=None)
                p = R8Phone(active="a", bootable=("a",), slot_unreadable=True)
                arm_entry("a")
                with patches(batch_inputs()):
                    rc, err = run_main(p, *cmd)
                self.assertEqual(rc, flashing.EXIT_HALT, err)
                self.assertIn(msg, err)
                self.assertNotIn("rollback", err)
                self.assertEqual(order(p.calls), [], "fb_mode was cleared or a slot touched")
                self.assertTrue(p.fb)
                self.assertIsNotNone(fbmode.pending())

    def test_no_pair_recover_on_a_dev_slot_halts(self):
        """R8-2, R8-4 (Opus m1): a confirmed dev slot halts (3); the message
        says flash --rollback (wipes) or the next flash stripped (no wipe)."""
        with bench_home():
            state(pair_kind=None)
            p = R8Phone(active="b", bootable=("a", "b"))
            arm_entry("a")
            rc, err = run_main(p, "--recover")
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertIn("flash --rollback", err)
            self.assertIn("flash stripped", err)
            self.assertNotIn("flash --recover", err)
            self.assertEqual(p.boots, [])
            self.assertIsNotNone(fbmode.pending())

    # R8-3 (Sol 4): host-only prerequisites first
    def test_host_only_refusals_never_touch_the_phone(self):
        from riverqa import stage
        for case in ("no state", "checkpoint"):
            with self.subTest(case=case), bench_home():
                if case == "checkpoint":
                    st = state(pair_kind=None)
                    st["baseline_closed"] = {"batch": "base", "build_id": BID, "at": "t", "seed": {"x": 1},
                                             "seed_written": False}
                    flashing.save_state(st)
                p = R8Phone(where="android", active="a", bootable=("a",), fb=False)
                arm_entry("a")
                with mock.patch.object(stage, "load", return_value={"batch": "x", "kind": "batch", "images": {}}), \
                        mock.patch.object(flashing, "check_images", return_value={}):
                    rc, err = run_main(p, "--batch", "x", "--wipe")
                self.assertEqual(rc, flashing.EXIT_ERROR, err)
                self.assertEqual((p.calls, p.where), ([], "android"))
                self.assertIsNotNone(fbmode.pending())

    # R8-4 (Opus m4)
    def test_rollback_clear_failure_names_flash_rollback(self):
        with bench_home():
            state(pair_kind="calibration")
            p = R8Phone(active="b", bootable=("a",), clear_fails=True)
            arm_entry("a")
            rc, err = run_main(p, "--rollback")
            self.assertEqual(rc, flashing.EXIT_HALT, err)
            self.assertIn("flash --rollback finishes it", err)
            self.assertNotIn("flash --recover", err)


class SingleSettlePath(unittest.TestCase):
    """R8-1: settle is the only resolver, and it takes only a VerifiedBoot
    from verify_boot of a boot that began after the clear."""

    SETTLE_CALLERS = {("fbmode", "recover"), ("flashing", "rollback"), ("flashing", "flash_dev"),
                      ("flashing", "recover_fb_mode_to_known_good"), ("reboots", "planned_reboot")}

    @staticmethod
    def calls(name):
        """(module, enclosing function) of every call of ``name`` in qa/lib."""
        out = set()
        for f in sorted(LIB.glob("*.py")):
            tree = ast.parse(f.read_text())

            def walk(node, fn):
                for ch in ast.iter_child_nodes(node):
                    if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        walk(ch, ch.name if fn is None else fn)
                        continue
                    if isinstance(ch, ast.Call):
                        func = ch.func
                        got = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                        if got == name:
                            out.add((f.stem, fn))
                    walk(ch, fn)
            walk(tree, None)
        return out

    @staticmethod
    def seal_writers():
        """The functions in fbmode that set ``_seal`` (by setattr or by
        passing it to the constructor)."""
        out = set()
        tree = ast.parse((LIB / "fbmode.py").read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for c in ast.walk(fn):
                if isinstance(c, ast.Call) and any(isinstance(a, ast.Constant) and a.value == "_seal" for a in c.args):
                    out.add(fn.name)
                if isinstance(c, ast.keyword) and c.arg == "_seal":
                    out.add(fn.name)
        return out

    def test_only_settle_resolves_an_obligation(self):
        self.assertEqual(self.calls("_settle_fb_mode"), {("fbmode", "settle")})
        self.assertEqual(self.calls("VerifiedBoot"), {("fbmode", "verify_boot")})
        self.assertEqual(self.calls("settle"), self.SETTLE_CALLERS)
        # R9-8: only verify_boot seals a proof, and only fbmode knows the seal.
        self.assertEqual(self.seal_writers(), {"verify_boot"})
        for f in LIB.glob("*.py"):
            if f.stem != "fbmode":
                self.assertNotIn("_seal", f.read_text().lower(), f.name)
        # Nothing writes journal.json but the Journal class.
        for f in LIB.glob("*.py"):
            if f.stem != "journal":
                self.assertNotIn("_drop(", f.read_text(), f.name)
                self.assertNotIn("_save(", f.read_text(), f.name)

    def test_journal_resolve_refuses_an_obligation(self):
        with bench_home():
            arm_entry("a")
            j = fbmode.journal()
            with self.assertRaises(ValueError):
                j.resolve(fbmode.pending(j))
            self.assertIsNotNone(fbmode.pending())

    def test_a_stale_journal_cannot_drop_the_obligation(self):
        """phone-qa's run journal, loaded before the sysrq control armed
        fb_mode through another Journal object, resolves one of its own
        entries: the obligation must survive (at 1897d45 the stale list was
        written back, and the obligation was gone)."""
        with bench_home():
            run_j = fbmode.journal()
            mine = run_j.record(jmod.PHONE, "setting", "global/x", "0")
            arm_entry("a")
            run_j.resolve(mine)
            self.assertIsNotNone(fbmode.pending())

    def phone_with_obligation(self):
        p = R8Phone(where="android", active="a", bootable=("a",), fb=False)
        arm_entry("a")
        return p

    def test_no_proof_no_settle(self):
        with bench_home():
            p = self.phone_with_obligation()
            with self.assertRaises(TypeError):
                fbmode.settle(object())
            # R9-8: a VerifiedBoot built outside verify_boot is unsealed.
            with self.assertRaises(TypeError):
                fbmode.settle(fbmode.VerifiedBoot("ZY225DPT7F", "boot-1", 0.0, "host", "a", (BID, BID), None, p))
            self.assertIsNotNone(fbmode.pending())

    def test_settle_needs_a_clear_then_a_boot_then_the_same_boot(self):
        with bench_home():
            p = self.phone_with_obligation()
            want = fbmode.Want("a", (BID, BID))
            before = fbmode.verify_boot(p, want)
            with self.assertRaisesRegex(fbmode.NotSettled, "never confirmed"):
                fbmode.settle(before)                        # not cleared
            p.where = "fastboot"
            fbmode.clear(p)
            with self.assertRaisesRegex(fbmode.NotSettled, "before fb_mode was cleared"):
                fbmode.settle(before)                        # a proof of the boot before the clear
            p.reboot_fastboot()
            after = fbmode.verify_boot(p, want)
            p.reboot_adb()                                   # rebooted after the check
            with self.assertRaisesRegex(fbmode.NotSettled, "rebooted after the check"):
                fbmode.settle(after)
            self.assertIsNotNone(fbmode.pending())
            self.assertTrue(fbmode.settle(fbmode.verify_boot(p, want)))
            self.assertIsNone(fbmode.pending())

    def test_arm_records_identity_and_needs_a_stamped_build(self):
        with bench_home():
            p = R8Phone(where="fastboot", active="a", fb=False)
            with self.assertRaises(ValueError):
                fbmode.arm(p, "a", (None, None))
            self.assertIsNone(fbmode.pending())
            fbmode.arm(p, "a", (BID, BID))
            self.assertEqual(fbmode.armed_want(fbmode.pending()), fbmode.Want("a", (BID, BID)))


if __name__ == "__main__":
    unittest.main()
