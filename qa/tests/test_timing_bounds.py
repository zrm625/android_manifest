"""Review round 4, item 1: the uptime clock's brackets.

The clock starts at the earliest possible disconnect (the last USB poll that
still saw the phone) and stops at the end of the status read. A loose
bracket on either end is UNVERIFIED, never PASS."""
import unittest
from unittest import mock

import _path  # noqa: F401
from riverqa import bootsig, device, reboots
from riverqa.device import BootStatus, UsbEvent, UsbWatcher
from riverqa.results import FAIL, PASS, UNVERIFIED

MARKER = "RQM:0123456789abcdef:aaaaaaaa"
TIMING = {"paths": {p: {"A": 12.09, "proof_normal_pass": True, "proof_control_rejected": True,
                        "proof_unmarked_rejected": True} for p in reboots.GATED_PATHS}}


class W:
    """A watcher whose sysrq disconnect is seen at ``first_gone`` after the
    phone was last seen at ``last_present``."""

    def __init__(self, last_present, first_gone):
        self.events, self.lp, self.fg = [], last_present, first_gone

    def mark(self):
        return len(self.events)

    def since(self, m):
        return list(self.events[m:])

    def wait_gone(self, m, seconds):
        return next(e for e in self.events[m:] if e.kind == "gone")


class Dev:
    serial = "ZY225DPT7F"

    def __init__(self, w, statuses):
        self.w, self.statuses, self.reads = w, list(statuses), 0

    def state(self):
        return "unknown"

    def reboot_sysrq(self):
        self.w.events.append(UsbEvent(self.w.fg, 0.0, "gone", None, self.w.lp))
        self.w.events.append(UsbEvent(self.w.fg + 20, 0.0, "present", "18d1:4ee7"))

    def wait_boot_completed(self, seconds):
        self.reads += 1
        return self.statuses.pop(0)

    def boot_status(self):
        self.reads += 1
        return self.statuses.pop(0) if self.statuses else None


def status(uptime, t1, read_s=0.1, boot_id="bid"):
    return BootStatus(boot_id, True, uptime, t1 - read_s, t1, 0.0)


def judged(last_present, first_gone, statuses):
    w = W(last_present, first_gone)
    dev = Dev(w, statuses)
    with mock.patch.object(reboots, "leave_marker", return_value=(MARKER, "x")):
        rec = reboots.planned_reboot(dev, "sysrq", boot_timeout=10, watcher=w)
    reboots.judge_record(rec, None, TIMING)
    return rec, dev


class DisconnectBracket(unittest.TestCase):
    def test_sol_2_3_s_observation_delay_is_unverified_never_pass(self):
        # A real 14.23 s extra pass: the phone dropped at 100.0, but the busy
        # host first saw it gone at 102.3. Timed from first_gone the pass
        # looks like 11.93 s, under A = 12.09, which used to PASS.
        uptime = 140.0 - 100.0 - 14.23
        rec, _ = judged(100.0, 102.3, [status(uptime, 140.0)])
        self.assertAlmostEqual(rec.disconnect_gap, 2.3)
        self.assertAlmostEqual(rec.elapsed, 40.0)            # from last_present, not first_gone
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)
        self.assertIn("bracketed", rec.uptime_check[1])

    def test_normal_tight_poll_passes(self):
        uptime = 140.0 - 100.0 - 9.9
        rec, _ = judged(100.0, 100.06, [status(uptime, 140.0)])
        self.assertEqual(rec.uptime_check[0], PASS)
        self.assertAlmostEqual(rec.bootloader_pass, 9.9, places=2)

    def test_tight_poll_extra_pass_fails(self):
        uptime = 140.0 - 100.0 - 14.23
        rec, _ = judged(100.0, 100.06, [status(uptime, 140.0)])
        self.assertEqual(rec.uptime_check[0], FAIL)

    def test_no_last_present_poll_is_unverified(self):
        uptime = 140.0 - 100.0 - 9.9
        rec, _ = judged(None, 100.06, [status(uptime, 140.0)])
        self.assertIsNone(rec.disconnect_gap)
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)

    def test_gap_at_the_bound(self):
        self.assertIsNone(bootsig.timing_bracket_problem(bootsig.DISCONNECT_BOUND_S, 0.1))
        self.assertIsNotNone(bootsig.timing_bracket_problem(bootsig.DISCONNECT_BOUND_S + 0.01, 0.1))


class StatusRead(unittest.TestCase):
    def test_slow_status_read_is_unverified(self):
        uptime = 140.0 - 100.0 - 9.9
        slow = [status(uptime, 140.0, read_s=1.4)] + [status(uptime + 2, 142.0, read_s=1.3)] * 3
        rec, _ = judged(100.0, 100.06, slow)
        self.assertAlmostEqual(rec.status_read_s, 1.4)
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)
        self.assertIn("status read", rec.uptime_check[1])

    def test_a_later_fast_read_of_the_same_boot_is_used(self):
        # A later read only makes elapsed longer: safe to use.
        uptime = 140.0 - 100.0 - 9.9
        rec, dev = judged(100.0, 100.06, [status(uptime, 140.0, read_s=1.4), status(uptime + 1.5, 141.5)])
        self.assertAlmostEqual(rec.status_read_s, 0.1)
        self.assertAlmostEqual(rec.elapsed, 41.5)
        self.assertEqual(rec.uptime_check[0], PASS)

    def test_retry_never_moves_the_boot_time(self):
        """R5-2 (Sol): boot completion first seen at 54.90 s with a slow
        read; the fast re-read of the same boot comes 1.15 s later. The boot
        time is the first observation, not 56.05 s; the uptime evidence
        comes from the re-read."""
        from riverqa import regression
        uptime = 140.0 - 100.0 - 9.9
        first = BootStatus("bid", True, uptime, 139.3, 140.7, 0.0)     # read 1.4 s, midpoint 140.0
        again = BootStatus("bid", True, uptime + 1.2, 141.1, 141.2, 0.0)
        with mock.patch.object(reboots.time, "monotonic", return_value=140.0 - 54.90):
            rec, _ = judged(100.0, 100.06, [first, again])
        self.assertLessEqual(rec.host_boot_seconds, 54.90 + first.read_s)
        self.assertAlmostEqual(rec.host_boot_seconds, 54.90)
        self.assertAlmostEqual(rec.elapsed, 141.2 - 100.0)            # uptime evidence: the re-read
        self.assertAlmostEqual(rec.status_read_s, 0.1)
        self.assertEqual([r["used"] for r in rec.status_reads], [False, True])
        self.assertAlmostEqual(rec.status_reads[0]["at_s"], 54.90)
        self.assertAlmostEqual(rec.status_reads[1]["at_s"], 56.05)
        row = {"used_ram_kb": "1000000", "host_boot_seconds": f"{rec.host_boot_seconds:.2f}",
               "system_used_kb": "1", "vendor_used_kb": "1"}
        self.assertIn("boot 1 54.9 s", regression.evaluate([row], None).summary)

    def test_a_fast_read_of_another_boot_is_not_used(self):
        uptime = 140.0 - 100.0 - 9.9
        rec, _ = judged(100.0, 100.06, [status(uptime, 140.0, read_s=1.4),
                                         status(3.0, 141.5, boot_id="other")] + [None] * 2)
        self.assertEqual(rec.boot_id, "bid")
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)

    def test_boot_status_records_both_ends_of_the_read(self):
        class R:
            returncode, stdout = 0, "abc\n1\n31.50 12.0\n"
        ticks = iter([10.0, 11.3])
        d = device.Device.__new__(device.Device)
        d.serial = "ZY225DPT7F"
        with mock.patch.object(device.Device, "adb", return_value=R()), \
                mock.patch.object(device.time, "monotonic", side_effect=lambda: next(ticks)), \
                mock.patch.object(device.time, "time", return_value=0.0):
            st = d.boot_status()
        self.assertEqual((st.t0, st.t1), (10.0, 11.3))
        self.assertAlmostEqual(st.read_s, 1.3)
        self.assertEqual(st.uptime, 31.5)


class WatcherBracket(unittest.TestCase):
    def test_gone_event_carries_the_last_present_poll(self):
        """last_present is the start of the last poll that saw the phone;
        the gone event's t is the end of the first poll that didn't."""
        ticks = iter([1.0, 1.01,     # start(): present
                      1.05, 1.06,    # present
                      3.36, 3.37])   # gone, after the host stalled for 2.3 s
        seen = iter(["18d1:4ee7", "18d1:4ee7", None])
        w = UsbWatcher("ZY225DPT7F")
        with mock.patch.object(device.time, "monotonic", side_effect=lambda: next(ticks)), \
                mock.patch.object(device, "usb_presence", side_effect=lambda s: next(seen)):
            w._poll(record=False)
            w._poll()
            w._poll()
        self.assertEqual(len(w.events), 1)
        e = w.events[0]
        self.assertEqual((e.kind, e.last_present, e.t), ("gone", 1.05, 3.37))
        self.assertAlmostEqual(e.gap, 2.32)


if __name__ == "__main__":
    unittest.main()
