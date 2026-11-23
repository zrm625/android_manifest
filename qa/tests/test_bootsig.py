import unittest

import _path  # noqa: F401
from riverqa import bootsig as b
from riverqa import reboots
from riverqa.results import FAIL, PASS, UNVERIFIED


class Marker(unittest.TestCase):
    def test_exact_and_flipped(self):
        m = b.make_marker("d52f8b5f-24c2-4a08", "ca393a06")
        self.assertEqual(m, "RQM:ca393a06:d52f8b5f")
        log = b"[ 1.0] foo\n[ 99.0] " + m.encode() + b"\n[ 99.1] reboot: Restarting system\n"
        self.assertEqual(b.chain_verdict(log, m)[0], PASS)
        flipped = bytearray(m.encode())
        flipped[5] ^= 0x04
        flipped[15] ^= 0x10
        log2 = b"[ 1.0] foo\n[ 99.0] " + bytes(flipped) + b"\n[ 99.1] bye\n"
        self.assertEqual(b.chain_verdict(log2, m)[0], PASS)

    def test_short_session_is_fail(self):
        m = b.make_marker("aaaaaaaa", "11111111")
        log = b"[    0.000000] Booting Linux\n[    2.163442] i2c NACK\n"
        self.assertEqual(b.chain_verdict(log, m)[0], FAIL)

    def test_missing_pstore_or_marker(self):
        self.assertEqual(b.chain_verdict(None, "RQM:x:y")[0], UNVERIFIED)
        self.assertEqual(b.chain_verdict(b"x", None)[0], UNVERIFIED)
        long_log = b"[ 500.000000] something\n" * 10
        self.assertEqual(b.chain_verdict(long_log, "RQM:12345678:abcdefab")[0], UNVERIFIED)


class Uptime(unittest.TestCase):
    def test_rule(self):
        self.assertEqual(b.uptime_verdict(29.0, 45.7, 18.5, 0)[0], PASS)
        self.assertEqual(b.uptime_verdict(29.0, 49.9, 18.5, 0)[0], FAIL)
        self.assertEqual(b.uptime_verdict(29.0, 45.7, None, 0)[0], UNVERIFIED)
        self.assertEqual(b.uptime_verdict(29.0, 45.7, 18.5, 1)[0], FAIL)

    def test_allowance_window(self):
        a, why = b.compute_allowance([16.55, 16.73, 16.56], 3.84)
        self.assertGreaterEqual(a, 16.73)
        self.assertLess(a, 16.55 + 3.84)
        self.assertIsNone(b.compute_allowance([10.0, 14.0], 3.0)[0])   # no workable A
        self.assertIsNone(b.compute_allowance([10.0], 3.0)[0])
        self.assertIsNone(b.compute_allowance([10.0, 10.1], None)[0])

    def test_river_numbers(self):
        # Step-1 data from the phone: A must accept every normal pass and
        # reject the injected pass.
        adb = [16.55, 16.73, 16.56, 16.62]
        inject = 20.39
        a, _ = b.compute_allowance(adb, min(10.02, inject - min(adb)))
        self.assertTrue(all(p <= a for p in adb))
        self.assertGreater(inject, a)


class Count(unittest.TestCase):
    DUMP = """Drop box contents: 3 entries
Max entries: 1000

2026-11-22 18:43:57 system_app_crash (text, 5489 bytes)
2026-11-22 18:44:04 SYSTEM_BOOT (text, 443 bytes)
2026-11-22 18:44:04 SYSTEM_BOOT (text, 442 bytes)

Usage: dumpsys dropbox [--print|--file] [YYYY-mm-dd] [HH:MM:SS] [tag]
"""

    def test_tags_and_count(self):
        tags = b.dropbox_tags(self.DUMP)
        self.assertEqual(tags, ["system_app_crash", "SYSTEM_BOOT", "SYSTEM_BOOT"])
        self.assertEqual(b.boot_count_verdict(tags, 2)[0], PASS)
        self.assertEqual(b.boot_count_verdict(tags, 1)[0], FAIL)
        self.assertEqual(b.boot_count_verdict(tags, 3)[0], UNVERIFIED)


class Combined(unittest.TestCase):
    def test_chain_not_used_on_path_where_it_does_not_survive(self):
        timing = {"chain": {"fastboot": {"survives": False}}}
        sig = {"chain": [UNVERIFIED, "x"], "uptime": [PASS, "ok"], "boot_count": [PASS, "ok"]}
        self.assertEqual(reboots.combined_verdict(sig, "fastboot", timing)[0], PASS)
        timing = {"chain": {"fastboot": {"survives": True}}}
        self.assertEqual(reboots.combined_verdict(sig, "fastboot", timing)[0], UNVERIFIED)
        sig["chain"] = [FAIL, "x"]
        self.assertEqual(reboots.combined_verdict(sig, "fastboot", {"chain": {"fastboot": {"survives": False}}})[0],
                         FAIL)

    def test_allowance_needs_proofs(self):
        t = {"paths": {"adb": {"A": 18.5, "proof_normal_pass": True, "proof_control_rejected": False}}}
        self.assertIsNone(reboots.allowance_for("adb", t))
        t["paths"]["adb"]["proof_control_rejected"] = True
        self.assertEqual(reboots.allowance_for("adb", t), 18.5)


if __name__ == "__main__":
    unittest.main()
