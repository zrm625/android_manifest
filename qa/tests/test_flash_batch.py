"""flash --batch with a fake phone: the pair is reserved before any write,
the target is never the known-good slot, a new attempt archives the old bench
runs, and a failed boot is rescued (E, B1, C7). No adb or fastboot."""
import io
import subprocess
import unittest
from contextlib import redirect_stderr
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from fakes import Boots
from riverqa import core, flashing, reboots, slots
from riverqa.device import GuardError

BOOT = "f" * 64


class FakePhone(Boots):
    serial = "ZY225DPT7F"

    def __init__(self, home, slot="a"):
        self.home, self.slot, self.events = home, slot, []

    def guard(self):
        self.events.append(("guard",))

    def state(self):
        return "device"

    def root(self, seconds=60):
        pass

    def getprop(self, name):
        return f"_{self.slot}" if name == "ro.boot.slot_suffix" else ""

    def sh(self, cmd, timeout=30):
        if "sha256sum" in cmd:
            return f"{BOOT}  x\n"
        return ""

    def fastboot_guard(self):
        self.events.append(("fastboot_guard",))

    def getvar(self, name):
        if name == "current-slot":
            return self.slot
        if name.startswith("slot-successful:"):
            return "No"
        if name.startswith("slot-retry-count:"):
            return "6"
        return ""

    def fastboot_ok(self, *args, timeout=300):
        st = core.read_json(self.home / "slot-state.json")
        self.events.append(("fastboot", args, dict(st["pair"]) if st.get("pair") else None))
        if args[0].startswith("--set-active="):
            self.slot = args[0].split("=", 1)[1]

    def fastboot(self, *args, timeout=120):
        self.events.append(("fastboot", args, None))
        return subprocess.CompletedProcess(args, 0, "", "")

    def fb_mode_clear(self):
        self.events.append(("fb_mode_clear",))

    def in_fastboot(self):
        return False


def stage_batch(home, kind="calibration"):
    imgs = home / "batches/b1/images"
    imgs.mkdir(parents=True)
    images = {}
    for n in flashing.IMAGES:
        (imgs / f"{n}.img").write_bytes(n.encode())
        images[n] = {"file": f"images/{n}.img", "sha256": core.sha256_file(imgs / f"{n}.img"), "size": len(n)}
    core.write_json(home / "batches/b1/batch.json", {
        "batch": "b1", "kind": kind, "build_id": "b1-x", "images": images, "dates": {},
        "bench_runs": ["old-run"], "flash_attempt": "flash-old"})
    st = slots.first_run("a", BOOT, "t0")
    st["baseline_closed"] = {"batch": "base", "build_id": None, "at": "t0", "seed": None, "seed_written": True}
    flashing.save_state(st)


def failing_reboot(dev, path, *, boot_timeout, in_fastboot=None, **kw):
    if in_fastboot:
        in_fastboot()
    raise TimeoutError("boot to complete: no result within 900 s")


class FlashBatch(unittest.TestCase):
    def flash(self, home, phone, rollback):
        with mock.patch.object(flashing, "Device", lambda *a: phone), \
                mock.patch.object(reboots, "planned_reboot", failing_reboot), \
                mock.patch.object(flashing, "rollback", rollback), redirect_stderr(io.StringIO()):
            return flashing.flash_batch("b1", None, True)

    def test_reserve_before_write_and_target_not_known_good(self):
        calls = []
        with bench_home() as h:
            stage_batch(h)
            phone = FakePhone(h)
            rc = self.flash(h, phone, lambda dev, st, reason, attempt_id=None: calls.append(st) or {"ok": 1})
            writes = [e for e in phone.events if e[0] == "fastboot" and e[1][0] == "flash"]
            self.assertEqual(len(writes), 4)
            # fb_mode is cleared in fastboot before the first write (R4-3).
            first_write = phone.events.index(writes[0])
            self.assertIn(("fb_mode_clear",), phone.events[:first_write])
            for _, args, pair in writes:
                self.assertIsNotNone(pair, "an image was written before the pair was saved")
                self.assertTrue(pair["written"])
                self.assertEqual(pair["fallback"], "a")
                self.assertTrue(args[1].endswith("_b"), args)       # never the known-good slot a
            batch = core.read_json(h / "batches/b1/batch.json")
            self.assertEqual(batch["bench_runs"], [])
            self.assertEqual(batch["prior_bench_runs"], [{"flash_attempt": "flash-old", "runs": ["old-run"]}])
            self.assertTrue(batch["flash_attempt"].startswith("flash-"))
            self.assertNotEqual(batch["flash_attempt"], "flash-old")
        self.assertEqual(rc, flashing.EXIT_RESCUED)
        self.assertEqual(calls[0]["pair"]["batch"], "b1")

    def test_rescue_guard_failure_halts(self):
        def guard_fails(dev, st, reason, attempt_id=None):
            raise GuardError("bootloader reports product 'lynx', not river. Refusing.")
        with bench_home() as h:
            stage_batch(h)
            rc = self.flash(h, FakePhone(h), guard_fails)
            st = flashing.load_state()
        self.assertEqual(rc, flashing.EXIT_HALT)
        self.assertEqual(st["pair"]["batch"], "b1")   # still reserved for recovery

    def test_rescue_failure_prints_an_error_line(self):
        """R7-5: a failed rescue goes through recovery_halt: an error: line,
        then HALT (62f73de printed only 'HALT: <reason>')."""
        def guard_fails(dev, st, reason, attempt_id=None):
            raise GuardError("bootloader reports product 'lynx', not river. Refusing.")
        with bench_home() as h:
            stage_batch(h)
            with mock.patch.object(flashing, "Device", lambda *a: FakePhone(h)), \
                    mock.patch.object(reboots, "planned_reboot", failing_reboot), \
                    mock.patch.object(flashing, "rollback", guard_fails), redirect_stderr(io.StringIO()) as err:
                rc = flashing.flash_batch("b1", None, True)
        self.assertEqual(rc, flashing.EXIT_HALT)
        self.assertIn("error: bootloader reports product 'lynx'", err.getvalue())
        self.assertIn("HALT: notify Zak.", err.getvalue())

    def test_running_slot_must_be_known_good(self):
        """R8-2: a phone in Android on a confirmed other slot halts (3) with
        the rollback advice; nothing is reserved."""
        with bench_home() as h:
            stage_batch(h)
            with mock.patch.object(flashing, "Device", lambda *a: FakePhone(h, slot="b")), \
                    redirect_stderr(io.StringIO()) as err:
                rc = flashing.flash_batch("b1", None, True)
            self.assertIsNone(flashing.load_state()["pair"])
        self.assertEqual(rc, flashing.EXIT_HALT, err.getvalue())
        self.assertIn("the phone runs slot b, not slot a", err.getvalue())
        self.assertIn("flash --rollback returns the phone to known_good_slot a", err.getvalue())


if __name__ == "__main__":
    unittest.main()
