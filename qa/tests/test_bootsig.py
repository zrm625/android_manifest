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


# --- amendment A1: the adb-path clock starts at the old kernel's reset ------------

import itertools  # noqa: E402
from unittest import mock  # noqa: E402

from riverqa.device import Read, UsbEvent  # noqa: E402

MARKER = "RQM:11111111:aaaaaaaa"


def shutdown_log(stamps, marker_ts=100.0):
    """A previous-kernel log: boot noise, the marker, then shutdown lines."""
    out = b"[    0.000000] Linux version 4.9.337\n[    1.000000] init\n"
    out += b"[%11.6f] " % marker_ts + MARKER.encode() + b"\n"
    for t in stamps:
        out += b"[%11.6f] shutdown step\n" % t
    return out


def record(**kw):
    rec = reboots.BootRecord(path="adb", marker=MARKER, boot_id_before="x",
                             marker_pair={"printk_ts": 100.0, "host_mono": 1000.0, "half_width": 0.05})
    rec.disconnect_mono, rec.completed_mono, rec.uptime = 1005.0, 1050.0, 30.0
    rec.elapsed = rec.elapsed_disconnect = rec.completed_mono - rec.disconnect_mono
    for k, v in kw.items():
        setattr(rec, k, v)
    return rec


TIMING = {"paths": {"adb": {"A": 12.0, "proof_normal_pass": True, "proof_control_rejected": True,
                            "max_shutdown_tail": 6.5}}}
NORMAL = [100.5 + i * 0.5 for i in range(20)]           # ... 109.5, then 110.0 is the reset
NORMAL.append(110.0)


class Corroboration(unittest.TestCase):
    def test_last_corroborated(self):
        self.assertEqual(b.corroborated_last([1.0, 1.1, 1.2, 1.3]), 1.3)
        self.assertIsNone(b.corroborated_last([1.0, 1.1]))

    def test_flipped_last_stamp_cannot_move_it_later(self):
        self.assertEqual(b.corroborated_last([119.8, 119.9, 120.0, 128.0]), 120.0)   # '0' -> '8'
        self.assertEqual(b.corroborated_last([40.9, 41.0, 45.1]), None)              # nothing corroborated
        self.assertEqual(b.corroborated_last([40.8, 40.9, 41.0, 45.1]), 41.0)        # '1' -> '5'

    def test_flip_in_the_middle_moves_it_earlier_only(self):
        self.assertEqual(b.corroborated_last([40.8, 40.9, 41.0, 45.1, 41.2, 41.3]), 41.0)


class Anchor(unittest.TestCase):
    def judge(self, rec, log, timing=TIMING, measuring=False):
        reboots.judge_record(rec, log, timing, measuring=measuring)
        return rec

    def test_normal_boot_passes_on_the_reset_clock(self):
        rec = self.judge(record(), shutdown_log(NORMAL))
        self.assertEqual(rec.clock_source, "reset-anchor")
        self.assertAlmostEqual(rec.anchor["host_mono"], 1009.95)     # earliest defensible
        self.assertAlmostEqual(rec.bootloader_pass, 10.05, places=2)
        self.assertEqual(rec.uptime_check[0], PASS)

    def test_extra_pass_fails(self):
        rec = self.judge(record(uptime=26.0), shutdown_log(NORMAL))
        self.assertEqual(rec.uptime_check[0], FAIL)

    def test_corrupt_stamp_that_still_parses_does_not_pass(self):
        # 110.000000 read back as 118.000000: anchored there, elapsed would
        # shrink by 8 s and hide the extra pass. Bound or not, no PASS.
        log = shutdown_log(NORMAL[:-1] + [118.0])
        for timing, measuring in ((TIMING, False), (TIMING, True)):
            rec = self.judge(record(uptime=26.0), log, timing, measuring)
            self.assertNotEqual(rec.uptime_check[0], PASS)
        rec = self.judge(record(uptime=26.0), log, TIMING, True)
        self.assertLess(rec.anchor["printk_ts"], 118.0)

    def test_41_1_to_45_1_flip_does_not_pass(self):
        pair = {"printk_ts": 30.0, "host_mono": 1000.0, "half_width": 0.05}
        tail = [30.5 + i * 0.5 for i in range(21)] + [41.1]     # reset at 41.1
        good = shutdown_log(tail, marker_ts=30.0)
        bad = shutdown_log(tail[:-1] + [45.1], marker_ts=30.0)
        timing = {"paths": {"adb": dict(TIMING["paths"]["adb"], max_shutdown_tail=7.0)}}
        # True reset at host 1011.1; disconnect 1005; completion 1050; an
        # extra 4 s pass leaves uptime 24.9 < 38.9 - 12.
        r1 = self.judge(record(marker_pair=pair, uptime=24.9), good, timing)
        self.assertEqual(r1.uptime_check[0], FAIL)
        r2 = self.judge(record(marker_pair=pair, uptime=24.9), bad, timing)
        self.assertNotEqual(r2.uptime_check[0], PASS)

    def test_missing_marker_is_unverified_never_the_disconnect(self):
        log = b"[    0.000000] Linux version 4.9.337\n" + b"".join(b"[%11.6f] x\n" % t for t in NORMAL)
        rec = self.judge(record(), log)
        self.assertEqual(rec.chain[0], FAIL)       # a log without the marker
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)
        self.assertEqual(rec.clock_source, "none")
        rec = self.judge(record(), None)
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)

    def test_unpaired_marker_is_unverified(self):
        rec = self.judge(record(marker_pair=None), shutdown_log(NORMAL))
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)

    def test_out_of_bounds_anchor_is_unverified(self):
        timing = {"paths": {"adb": dict(TIMING["paths"]["adb"], max_shutdown_tail=3.0)}}
        rec = self.judge(record(), shutdown_log(NORMAL), timing)    # tail 4.95 s > 3 s
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)
        rec = self.judge(record(disconnect_mono=1011.0), shutdown_log(NORMAL))   # reset before the disconnect
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)

    def test_no_calibrated_tail_is_unverified(self):
        timing = {"paths": {"adb": {k: v for k, v in TIMING["paths"]["adb"].items() if k != "max_shutdown_tail"}}}
        self.assertEqual(self.judge(record(), shutdown_log(NORMAL), timing).uptime_check[0], UNVERIFIED)

    def test_anchor_before_marker_is_refused(self):
        anchor, why = b.reset_anchor(shutdown_log([99.0, 99.1, 99.2]), MARKER,
                                     {"printk_ts": 100.0, "host_mono": 0.0, "half_width": 0.0}, None)
        self.assertIsNone(anchor)

    def test_fastboot_path_keeps_the_disconnect_clock(self):
        t = {"paths": {"fastboot": {"A": 12.0, "proof_normal_pass": True, "proof_control_rejected": True}}}
        rec = self.judge(record(path="fastboot", marker_pair=None, uptime=35.0), None, t)
        self.assertEqual(rec.clock_source, "disconnect")
        self.assertEqual(rec.uptime_check[0], PASS)       # 35 >= 45 - 12
        rec = self.judge(record(path="fastboot", marker_pair=None, uptime=30.0), None, t)
        self.assertEqual(rec.uptime_check[0], FAIL)       # 30 < 45 - 12


class Pairing(unittest.TestCase):
    class Dev:
        def __init__(self, dmesg_ts=100.25):
            self.ts, self.writes = dmesg_ts, 0

        def root(self):
            pass

        def boot_id(self):
            return "aaaaaaaa-bbbb"

        def kmsg(self, text):
            self.writes += 1
            self.marker = text

        def read(self, cmd, timeout=30):
            out = "" if self.ts is None else f"[{self.ts:11.6f}] {self.marker}\n"
            return Read(0, out)

    def test_marker_paired_with_its_printk_timestamp(self):
        dev = self.Dev()
        with mock.patch.object(reboots.time, "monotonic", side_effect=[50.0, 50.1]):
            marker, bid, pair = reboots.leave_marker(dev)
        self.assertEqual(pair["printk_ts"], 100.25)
        self.assertAlmostEqual(pair["host_mono"], 50.05)
        self.assertAlmostEqual(pair["half_width"], 0.05)

    def test_slow_write_is_retried_then_unpaired(self):
        dev = self.Dev()
        with mock.patch.object(reboots.time, "monotonic", side_effect=[0.0, 1.0, 2.0, 3.0, 4.0, 5.0]):
            marker, bid, pair = reboots.leave_marker(dev)
        self.assertEqual(dev.writes, 3)
        self.assertIsNotNone(marker)
        self.assertIsNone(pair)

    def test_no_readback_is_unpaired(self):
        with mock.patch.object(reboots.time, "monotonic", side_effect=[0.0, 0.1]):
            self.assertIsNone(reboots.leave_marker(self.Dev(dmesg_ts=None))[2])


class Enumeration(unittest.TestCase):
    """planned_reboot's enumeration accounting with a fake watcher (E, D4)."""

    class W:
        def __init__(self):
            self.events, self.clock = [], itertools.count(1)

        def add(self, kind, ids=None):
            self.events.append(UsbEvent(float(next(self.clock)), 0.0, kind, ids))

        def mark(self):
            return len(self.events)

        def since(self, m):
            return list(self.events[m:])

        def wait_gone(self, m, seconds):
            return next(e for e in self.events[m:] if e.kind == "gone")

    class Dev:
        serial = "ZY225DPT7F"

        def __init__(self, w, stray=False):
            self.w, self.stray = w, stray

        def state(self):
            return "unknown"      # no marker for these tests

        def reboot_adb(self, target=None):
            self.w.add("gone")
            self.w.add("present", "18d1:d00d" if target == "bootloader" else "18d1:4ee7")

        def reboot_fastboot(self, target=None):
            self.w.add("gone")
            if self.stray and target is None:
                # The bootloader enumerates before the watcher's mark could
                # be sampled: it must still count (D4).
                self.w.add("present", "18d1:d00d")
                self.w.add("gone")
            self.w.add("present", "18d1:d00d" if target == "bootloader" else "18d1:4ee7")

        def wait_fastboot(self, seconds):
            pass

        def wait_boot_completed(self, seconds):
            from riverqa.device import BootStatus
            return BootStatus("bid", True, 30.0, 100.0, 0.0)

    def reboot(self, path, stray=False, **kw):
        w = self.W()
        with mock.patch.dict("os.environ", {"RIVERQA_CONTROL": "x"} if kw else {}):
            return reboots.planned_reboot(self.Dev(w, stray), path, boot_timeout=10, watcher=w, **kw)

    def test_normal_paths(self):
        for path in ("adb", "fastboot"):
            rec = self.reboot(path)
            self.assertEqual(rec.unplanned_fastboot, 0, path)
        self.assertEqual(self.reboot("fastboot").planned_fastboot, 1)

    def test_marked_extra_pass(self):
        for path in ("adb", "fastboot"):
            rec = self.reboot(path, inject_extra_pass=True)
            self.assertEqual(rec.unplanned_fastboot, 0, path)

    def test_unmarked_extra_pass_counts(self):
        for path in ("adb", "fastboot"):
            rec = self.reboot(path, inject_extra_pass=True, mark_planned=False)
            self.assertEqual(rec.unplanned_fastboot, 1, path)

    def test_enumeration_right_after_the_disconnect_counts(self):
        rec = self.reboot("fastboot", stray=True)
        self.assertEqual(rec.unplanned_fastboot, 1)
        # The clock starts at the first disconnect after the final reboot.
        self.assertEqual(rec.disconnect_mono, 1.0)
