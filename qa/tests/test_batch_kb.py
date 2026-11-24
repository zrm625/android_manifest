"""KB: the known-good build check without root (I1, I2), and checked reads."""
import unittest

import _path  # noqa: F401
from batchfakes import FakeDev, write_json
from benchhome import bench_home
from riverbatch import kb

SS = {"known_good_slot": "a", "known_good": {"batch": "kg-1", "build_id": "kg-1-abc"}}


def staged(home, vendor="100"):
    write_json(home / "batches/kg-1/batch.json", {"batch": "kg-1", "dates": {"vendor": vendor}})


class KB(unittest.TestCase):
    def check(self, dev, ss=SS, vendor="100"):
        with bench_home() as h:
            staged(h, vendor)
            return kb.check(dev, ss)

    def test_holds_with_one_non_elevating_read(self):
        dev = FakeDev()
        c = self.check(dev)
        self.assertTrue(c.ok, c.message)
        self.assertEqual(len(dev.cmds), 1)                       # one sh_rc call (I2)
        for p in ("ro.product.device", "ro.serialno", "ro.boot.slot_suffix", "sys.boot_completed",
                  "ro.river.build_id", "ro.vendor.build.date.utc"):
            self.assertIn(p, dev.cmds[0])
        self.assertNotIn("ro.vendor.river.build_id", dev.cmds[0])   # I1: unreadable without root
        self.assertNotIn("/vendor/build.prop", dev.cmds[0])

    def test_vendor_matched_by_staged_vendor_date(self):
        c = self.check(FakeDev({"ro.vendor.build.date.utc": "999"}))
        self.assertEqual(c.kind, kb.BLOCKED)
        self.assertIn("flash --rollback", c.message)

    def test_missing_staged_date_blocks(self):
        with bench_home():
            c = kb.check(FakeDev(), SS)
        self.assertEqual(c.kind, kb.BLOCKED)

    def test_halt_only_on_rc0_identity_mismatch(self):
        self.assertEqual(self.check(FakeDev({"ro.product.device": "lynx"})).kind, kb.HALT)
        self.assertEqual(self.check(FakeDev({"ro.serialno": "OTHER"})).kind, kb.HALT)
        # rc != 0 with a "mismatched" product is not trusted: re-check, pause
        self.assertEqual(self.check(FakeDev({"ro.product.device": "lynx"}, rc=1)).kind, kb.PAUSE)

    def test_empty_values_recheck_state(self):
        c = self.check(FakeDev({"ro.serialno": ""}))
        self.assertEqual(c.kind, kb.PAUSE)

        class Flaky(FakeDev):
            n = 0

            def state(self):
                self.n += 1
                return "device" if self.n == 1 else ""

            def in_fastboot(self):
                return True
        c = self.check(Flaky({"ro.product.device": ""}))
        self.assertEqual((c.kind, c.message), (kb.BLOCKED, "phone in fastboot"))

    def test_usb_states(self):
        self.assertEqual(self.check(FakeDev(state="", fastboot=True)).message, "phone in fastboot")
        self.assertEqual(self.check(FakeDev(state="")).kind, kb.PAUSE)
        self.assertIn("hold the power button", self.check(FakeDev(state="")).message)
        self.assertEqual(self.check(FakeDev(state="unauthorized")).kind, kb.BLOCKED)
        self.assertEqual(self.check(FakeDev(state="recovery")).kind, kb.BLOCKED)

    def test_booting_unstamped_other_slot(self):
        self.assertEqual(self.check(FakeDev({"sys.boot_completed": "0"})).message, "phone still booting")
        self.assertIn("unstamped", self.check(FakeDev({"ro.river.build_id": "unstamped"})).message)
        self.assertIn("slot b", self.check(FakeDev({"ro.boot.slot_suffix": "_b"})).message)
        self.assertEqual(self.check(FakeDev({"ro.river.build_id": "dev-1"})).kind, kb.BLOCKED)

    def test_checked_read(self):
        c, out = kb.checked_read(FakeDev(reads={"wifi": "ok"}), "cmd wifi status")
        self.assertTrue(c.ok)
        self.assertEqual(out, "ok")
        c, _ = kb.checked_read(FakeDev(state=""), "x")
        self.assertEqual(c.kind, kb.PAUSE)


if __name__ == "__main__":
    unittest.main()
