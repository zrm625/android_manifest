import itertools
import unittest
from unittest import mock

import _path  # noqa: F401
from riverqa import bootsig as b
from riverqa import reboots
from riverqa.device import Read, UsbEvent
from riverqa.results import FAIL, INFO, PASS, UNVERIFIED

MARKER = "RQM:1111111111111111:aaaaaaaa"
COPY_STEP = 0.00003      # river writes 4 copies about 30 us apart


def line(t, text):
    sec, us = divmod(round(t * 1e6), 1000000)
    return b"[%5d.%06d] " % (sec, us) + text + b"\n"


def copies_at(marker_ts, n):
    return [marker_ts + k * COPY_STEP for k in range(n)]


SHUTDOWN = [100.5 + i * 0.5 for i in range(19)] + [110.0, 110.0003, 110.0006]


def shutdown_log(stamps=SHUTDOWN, marker_ts=100.0, copies=1, marker=MARKER):
    """A previous-kernel log: boot noise, the marker copies, then shutdown
    lines, in the kernel's own "[%5lu.%06lu] " layout."""
    out = line(0.0, b"Linux version 4.9.337") + line(1.0, b"init")
    for t in copies_at(marker_ts, copies):
        out += line(t, marker.encode())
    for t in stamps:
        out += line(t, b"shutdown step")
    return out


def other_kernel_log():
    """The log of an extra kernel that ran past ramoops init: it replaced
    the marker kernel's log, so it holds no copy of the marker."""
    return line(0.0, b"Linux version 4.9.337") + b"".join(line(t, b"boot step") for t in (1.0, 2.1, 9.8, 14.2))


def flip_bytes(text: bytes, positions) -> bytes:
    """``text`` with one bit flipped in each byte at ``positions``."""
    out = bytearray(text)
    for p in positions:
        out[p] ^= 0x04
    return bytes(out)


class Chain(unittest.TestCase):
    """Amendment A2.1: PASS needs a copy of this attempt's marker (within 3
    flips) and no new session after it; stamps play no part. An extra kernel
    after ramoops init leaves no copy, which is a FAIL."""

    def test_copy_passes(self):
        self.assertEqual(b.chain_verdict(shutdown_log(), MARKER)[0], PASS)
        self.assertEqual(b.chain_verdict(shutdown_log(copies=4), MARKER)[0], PASS)

    def test_damaged_stamp_passes(self):
        # Under A2 these were UNVERIFIED; the stamp no longer matters.
        for bad in (b"[  100.0\xb00000] RQM", b"[  100/000000] RQM", b"[ 0100.000000] RQM",
                    b"[  100.000000]\xa0RQM", b"K  100.000000] RQM"):
            log = shutdown_log().replace(b"[  100.000000] RQM", bad)
            self.assertEqual(b.chain_verdict(log, MARKER)[0], PASS, bad)

    def test_copies_within_three_flips_pass(self):
        m = MARKER.encode()
        for n in (1, 2, 3):
            log = shutdown_log().replace(m, flip_bytes(m, range(4, 4 + n)))
            self.assertEqual(b.chain_verdict(log, MARKER)[0], PASS, n)
        log = shutdown_log().replace(m, flip_bytes(m, range(4, 8)))
        self.assertEqual(b.chain_verdict(log, MARKER)[0], FAIL)      # 4 flips: not a copy

    def test_every_three_flip_copy_is_found(self):
        # The 6-character anchor scan must never miss a copy within 3 flips,
        # wherever the flips land in the 29-character marker.
        m = MARKER.encode()
        self.assertEqual(len(m), 29)
        head, tail = line(1.0, b"init"), b"\n" + line(110.0, b"shutdown step")
        for pos in itertools.combinations(range(len(m)), 3):
            log = head + b"[  100.000000] " + flip_bytes(m, pos) + tail
            self.assertEqual(b.marker_offsets(log, MARKER), [len(head) + 15], pos)

    def test_real_style_dump_all_stamps_damaged(self):
        # A1'/A2 data: the 4 copies had 2, 1, 2 and 4 wrong bytes and three
        # damaged stamps. Three copies are within 3 flips: PASS.
        m = MARKER.encode()
        log = line(0.0, b"Linux version 4.9.337") + line(1.0, b"init")
        log += b"[  100.00\xb0000] " + flip_bytes(m, (5, 17)) + b"\n"
        log += b"[\xa0 100.000030] " + flip_bytes(m, (9,)) + b"\n"
        log += b"[\xa0 100.00006\xb0] " + flip_bytes(m, (4, 20)) + b"\n"
        log += b"[  100.000090] " + flip_bytes(m, (4, 8, 12, 16)) + b"\n"
        log += b"".join(line(t, b"shutdown step") for t in SHUTDOWN)
        self.assertEqual(log.count(m), 0)
        st, why = b.chain_verdict(log, MARKER)
        self.assertEqual(st, PASS)
        self.assertIn("3 copies", why)

    def test_extra_kernel_log_is_fail(self):
        st, why = b.chain_verdict(other_kernel_log(), MARKER)
        self.assertEqual(st, FAIL)
        self.assertIn("another kernel ran after ramoops init", why)
        # A long log with no copy and no banner is the same: the marker
        # kernel's log is gone.
        self.assertEqual(b.chain_verdict(b"[  500.000000] something\n" * 10, MARKER)[0], FAIL)

    def test_new_session_after_the_marker_is_fail(self):
        new = shutdown_log() + line(0.0, b"Linux version 4.9.337")
        st, why = b.chain_verdict(new, MARKER)
        self.assertEqual(st, FAIL)
        self.assertIn("new kernel session", why)
        # Also between copies: the session counts from the first copy.
        log = shutdown_log() + line(0.0, b"Linux version 4.9.337") + line(5.0, MARKER.encode())
        self.assertEqual(b.chain_verdict(log, MARKER)[0], FAIL)

    def test_missing_pstore_or_marker_is_unverified(self):
        self.assertEqual(b.chain_verdict(None, MARKER)[0], UNVERIFIED)
        self.assertEqual(b.chain_verdict(b"", MARKER)[0], UNVERIFIED)
        self.assertEqual(b.chain_verdict(shutdown_log(), None)[0], UNVERIFIED)
        self.assertEqual(b.chain_verdict(shutdown_log(), "")[0], UNVERIFIED)

    def test_marker_from_another_write_does_not_match(self):
        # Same boot, an independently generated 64-bit nonce: a different
        # write. Its copies are not this attempt's marker, so the log is
        # another kernel's (FAIL), never PASS. A copy of this attempt's
        # marker with 3 flipped nonce characters still matches.
        mine = b.make_marker("aaaaaaaa-bbbb")
        other = b.make_marker("aaaaaaaa-bbbb")
        self.assertEqual(len(mine.split(":")[1]), b.NONCE_HEX)
        self.assertNotEqual(mine, other)
        self.assertEqual(b.chain_verdict(shutdown_log(copies=4, marker=other), mine)[0], FAIL)
        flipped = mine[:4] + "".join("x" if i in (0, 7, 15) else c for i, c in enumerate(mine[4:20])) + mine[20:]
        self.assertEqual(b.hamming(flipped.encode(), mine.encode()), 3)
        self.assertEqual(b.chain_verdict(shutdown_log(marker=flipped), mine)[0], PASS)

    def test_random_nonces_never_match_each_other(self):
        # 10,000 pairs of different writes from the same boot: none is ever
        # within MAX_MARKER_FLIPS of the other, at any alignment.
        for _ in range(10000):
            m1, m2 = b.make_marker("aaaaaaaa-bbbb"), b.make_marker("aaaaaaaa-bbbb")
            if m1 == m2:
                continue
            self.assertEqual(b.marker_offsets(b"[  100.000000] " + m2.encode() + b"\n", m1), [], (m1, m2))

    def test_no_single_bit_flip_turns_a_good_log_into_a_fail(self):
        # ramoops damage alone must never look like an extra kernel: flip
        # each bit from the first marker copy to the end, one at a time.
        log = shutdown_log(copies=4)
        start = log.index(b"[  100.000000]")
        for i in range(start, len(log)):
            for bit in range(8):
                bad = bytearray(log)
                bad[i] ^= 1 << bit
                self.assertEqual(b.chain_verdict(bytes(bad), MARKER)[0], PASS, (i, bit))


class Uptime(unittest.TestCase):
    def test_rule(self):
        tight = {"disconnect_gap_s": 0.06, "status_read_s": 0.1}
        self.assertEqual(b.uptime_verdict(29.0, 45.7, 18.5, 0, **tight)[0], PASS)
        self.assertEqual(b.uptime_verdict(29.0, 49.9, 18.5, 0, **tight)[0], FAIL)
        self.assertEqual(b.uptime_verdict(29.0, 45.7, None, 0, **tight)[0], UNVERIFIED)
        self.assertEqual(b.uptime_verdict(29.0, 45.7, 18.5, 1, **tight)[0], FAIL)

    def test_allowance_window(self):
        a, why = b.compute_allowance([16.55, 16.73, 16.56], 3.84)
        self.assertGreaterEqual(a, 16.73)
        self.assertLess(a, 16.55 + 3.84)
        self.assertIsNone(b.compute_allowance([10.0, 14.0], 3.0)[0])   # no workable A
        self.assertIsNone(b.compute_allowance([10.0], 3.0)[0])
        self.assertIsNone(b.compute_allowance([10.0, 10.1], None)[0])

    def test_river_numbers(self):
        # A2 data: sysrq passes 10.06-10.08 s, like the fastboot path, and an
        # injected extra pass adds about 4.4 s.
        sysrq = [10.06, 10.07, 10.08, 10.065]
        a, _ = b.compute_allowance(sysrq, min(10.064, 4.4))
        self.assertTrue(all(p <= a for p in sysrq))
        self.assertGreater(min(sysrq) + 4.4, a)


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


GATED = {"A": 12.0, "proof_normal_pass": True, "proof_control_rejected": True}
TIMING = {"paths": {"sysrq": dict(GATED), "fastboot": dict(GATED)}}


def record(path="sysrq", **kw):
    rec = reboots.BootRecord(path=path, marker=MARKER, boot_id_before="x")
    rec.uptime, rec.elapsed = 35.0, 45.0
    rec.disconnect_gap, rec.status_read_s = 0.06, 0.1
    for k, v in kw.items():
        setattr(rec, k, v)
    return rec


class Judge(unittest.TestCase):
    """judge_record: the disconnect clock on the gated paths, INFO on adb."""

    def judge(self, rec, log=None, timing=TIMING):
        reboots.judge_record(rec, shutdown_log() if log is None else log, timing)
        return rec

    def test_gated_paths_use_the_disconnect_clock(self):
        for path in reboots.GATED_PATHS:
            self.assertEqual(self.judge(record(path)).uptime_check[0], PASS, path)               # 35 >= 45 - 12
            self.assertEqual(self.judge(record(path, uptime=30.0)).uptime_check[0], FAIL, path)  # 30 < 45 - 12
            self.assertEqual(self.judge(record(path), timing={}).uptime_check[0], UNVERIFIED, path)
            self.assertEqual(self.judge(record(path, unplanned_fastboot=1)).uptime_check[0], FAIL, path)

    def test_adb_uptime_is_info_only(self):
        # Even an old boot-timing.json with an adb A is ignored: bench boot 1
        # has no uptime gate (A2 rule 1).
        timing = {"paths": {"adb": dict(GATED)}}
        for uptime in (35.0, 5.0):
            rec = self.judge(record("adb", uptime=uptime), timing=timing)
            self.assertEqual(rec.uptime_check[0], INFO)
            self.assertEqual(rec.chain[0], PASS)
        self.assertEqual(self.judge(record("adb", unplanned_fastboot=1)).uptime_check[0], FAIL)

    def test_adb_extra_kernel_fails_through_the_chain(self):
        rec = self.judge(record("adb"), other_kernel_log())
        self.assertEqual(rec.chain[0], FAIL)
        sig = {"chain": list(rec.chain), "uptime": list(rec.uptime_check), "boot_count": [PASS, "ok"]}
        self.assertEqual(reboots.combined_verdict(sig, "adb", {})[0], FAIL)

    def test_info_uptime_does_not_count_in_the_combined_verdict(self):
        rec = self.judge(record("adb"))
        sig = {"chain": list(rec.chain), "uptime": list(rec.uptime_check), "boot_count": [PASS, "ok"]}
        self.assertEqual(reboots.combined_verdict(sig, "adb", {})[0], PASS)

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
        t = {"paths": {"sysrq": {"A": 12.3, "proof_normal_pass": True, "proof_control_rejected": False}}}
        self.assertIsNone(reboots.allowance_for("sysrq", t))
        t["paths"]["sysrq"]["proof_control_rejected"] = True
        self.assertEqual(reboots.allowance_for("sysrq", t), 12.3)


def raw(bootloader_pass, chain=PASS):
    return {"bootloader_pass": bootloader_pass, "chain": [chain, ""], "marker_copies_exact": 4,
            "marker_found_fuzzy": True}


DROPBOX0 = "Drop box contents: 0 entries\n"
DROPBOX1 = "Drop box contents: 1 entries\n\n2026-11-22 18:44:04 SYSTEM_BOOT (text, 443 bytes)\n"
PROOFS = {"proof_normal_pass": True, "proof_control_rejected": True, "proof_unmarked_rejected": True}


class Calibration(unittest.TestCase):
    """compute_paths, the adb chain control and the report (A2 rules 5-6)."""

    def data(self, sysrq=None, inj_sysrq=None):
        return {"raw": {"fastboot": [raw(10.072), raw(10.065), raw(10.064), raw(10.08)],
                        "sysrq": sysrq if sysrq is not None else [raw(10.06), raw(10.07), raw(10.08)]},
                "inject_measure": {"fastboot": raw(14.465), "sysrq": inj_sysrq or raw(14.46)},
                "paths": {}, "chain": {}, "controls": {}}

    def test_both_gated_paths_get_an_allowance(self):
        from riverqa import calibrate
        d = self.data()
        calibrate.compute_paths(d)
        self.assertEqual(set(d["paths"]), {"fastboot", "sysrq"})
        for path in ("fastboot", "sysrq"):
            p = d["paths"][path]
            self.assertIsNotNone(p["A"], p["why"])
            self.assertTrue(max(p["normal_passes"]) <= p["A"] < min(p["normal_passes"]) + p["extra_pass"])
            self.assertTrue(d["chain"][path]["survives"])
        self.assertAlmostEqual(d["paths"]["sysrq"]["extra_pass"], 4.4, places=2)   # the injected increment
        self.assertTrue(any(line.startswith("  sysrq    A=") for line in calibrate.summary(d)))

    def test_missing_measurements_report_the_reason(self):
        from riverqa import calibrate
        d = self.data(sysrq=[])
        calibrate.compute_paths(d)
        self.assertIsNone(d["paths"]["sysrq"]["A"])
        self.assertIn("no sysrq reboots measured", d["paths"]["sysrq"]["why"])
        lines = calibrate.summary(d)            # no KeyError
        self.assertTrue(any("sysrq" in line and "not qualified" in line for line in lines))
        self.assertFalse(calibrate.qualified(d))
        empty = {"raw": {}, "inject_measure": {}, "paths": {}, "chain": {}}
        calibrate.compute_paths(empty)
        calibrate.summary(empty)
        self.assertFalse(calibrate.qualified(empty))

    def test_chain_record_counts_boots_that_did_not_pass(self):
        from riverqa import calibrate
        d = self.data(sysrq=[raw(10.06), raw(10.07, chain=UNVERIFIED)])
        calibrate.compute_paths(d)
        self.assertFalse(d["chain"]["sysrq"]["survives"])
        self.assertEqual(d["chain"]["sysrq"]["verdicts"], [PASS, UNVERIFIED])
        self.assertNotIn("proven", d["chain"]["sysrq"])

    def test_qualified_needs_every_proof_and_the_adb_chain_control(self):
        from riverqa import calibrate
        d = {"paths": {"sysrq": dict(PROOFS), "fastboot": dict(PROOFS)},
             "controls": {calibrate.ADB_CHAIN_CONTROL: {"qualified": True}}}
        self.assertTrue(calibrate.qualified(d))
        d["paths"]["sysrq"]["proof_unmarked_rejected"] = False
        self.assertFalse(calibrate.qualified(d))
        d["paths"]["sysrq"]["proof_unmarked_rejected"] = True
        d["controls"][calibrate.ADB_CHAIN_CONTROL]["qualified"] = False
        self.assertFalse(calibrate.qualified(d))
        del d["controls"][calibrate.ADB_CHAIN_CONTROL]
        self.assertFalse(calibrate.qualified(d))

    def test_adb_chain_control_needs_the_fault_and_a_chain_fail(self):
        from riverqa import calibrate
        cases = (("early-reboot", [FAIL, "x"], True),
                 ("early-reboot-missed", [FAIL, "x"], False),     # the fault never went in
                 ("early-reboot", [UNVERIFIED, "x"], False),      # not rejected by the chain
                 ("early-reboot", [PASS, "x"], False))
        for control, chain, want in cases:
            d = {"controls": {}}
            with mock.patch.object(calibrate, "one", return_value={"control": control, "chain": chain}) as one:
                calibrate.prove_adb_chain(None, None, d)
            self.assertEqual(one.call_args.args[2], "adb")
            self.assertTrue(one.call_args.kwargs["early_reboot"])
            c = d["controls"][calibrate.ADB_CHAIN_CONTROL]
            self.assertEqual(c["qualified"], want, (control, chain))
            self.assertEqual(c["check"], "chain (boot-timing)")

    def test_step1_controls_are_in_the_control_set(self):
        from riverqa import controls
        for name in ("boot-timing-sysrq-extra-pass", "boot-timing-sysrq-extra-pass-unmarked",
                     "boot-timing-fastboot-extra-pass", "boot-timing-fastboot-extra-pass-unmarked",
                     "boot-timing-adb-extra-reboot"):
            self.assertEqual(controls.CONTROLS[name][0], "step1", name)
        self.assertFalse(any(n.startswith("boot-timing-adb-extra-pass") for n in controls.CONTROLS))

    def test_boot_count_now_waits_for_a_boot_entry(self):
        from riverqa import calibrate
        dumps = iter([DROPBOX0, DROPBOX0, DROPBOX1, DROPBOX1])

        class Dev:
            def sh(self, cmd, timeout=30):
                return next(dumps)
        with mock.patch.object(calibrate.time, "sleep", lambda s: None):
            self.assertEqual(calibrate.boot_count_now(Dev()), 1)

    def test_controls_boot_timing_command(self):
        from riverqa import calibrate, qa
        with mock.patch.object(calibrate, "prove_main", return_value=0) as pm:
            self.assertEqual(qa.main(["controls", "--boot-timing"]), 0)
        pm.assert_called_once()


class CheckedPstoreRead(unittest.TestCase):
    """The boot judge reads pstore through the checked binary read (R2-4)."""

    def judge(self, files):
        from fakes import FakeShell
        dev = FakeShell({}, {r"console-ramoops": files})
        rec = record()
        reboots.judge_signals(dev, rec, TIMING)
        return rec

    def test_good_read_passes(self):
        rec = self.judge((0, shutdown_log()))
        self.assertEqual(rec.chain[0], PASS)
        self.assertEqual(rec.uptime_check[0], PASS)

    def test_failed_or_partial_read_is_unverified(self):
        log = shutdown_log()
        import base64
        cut = f"__FILE__ {len(log) + 100} /sys/fs/pstore/console-ramoops-0\n" + \
            base64.encodebytes(log).decode() + "__EOF__\n__RC__ 1\n"
        for files in ((1, log), (0, cut), (0, b"")):
            rec = self.judge(files)
            self.assertEqual(rec.chain[0], UNVERIFIED, files[0])
            self.assertTrue(rec.pstore_read_error)
            sig = {"chain": list(rec.chain), "uptime": list(rec.uptime_check), "boot_count": [PASS, "ok"],
                   "reboot": rec.to_json()}
            # Even on a path whose chain "doesn't survive", a failed read is UNVERIFIED.
            self.assertEqual(reboots.combined_verdict(sig, "sysrq", {"chain": {"sysrq": {"survives": False}}})[0],
                             UNVERIFIED)


class LeaveMarker(unittest.TestCase):
    """leave_marker writes a fresh 64-bit nonce per attempt, checks the
    write, and reads nothing back (A2.1)."""

    class Dev:
        """kmsg() results are taken from ``writes_ok`` in order."""

        def __init__(self, writes_ok=(True, True, True)):
            self.ok = list(writes_ok)
            self.markers = []

        def root(self):
            pass

        def boot_id(self):
            return "aaaaaaaa-bbbb"

        def kmsg(self, text):
            self.markers.append(text)
            return self.ok.pop(0)

        def read(self, cmd, timeout=30):
            raise AssertionError(f"no live readback under A2.1: {cmd}")

    def test_marker_written(self):
        dev = self.Dev()
        marker, bid = reboots.leave_marker(dev)
        self.assertEqual(marker, dev.markers[0])
        self.assertEqual(bid, "aaaaaaaa-bbbb")
        self.assertRegex(marker, r"^RQM:[0-9a-f]{16}:aaaaaaaa$")

    def test_failed_write_is_retried_with_a_new_nonce(self):
        dev = self.Dev(writes_ok=(False, True))
        marker, _bid = reboots.leave_marker(dev)
        self.assertEqual(marker, dev.markers[1])
        self.assertNotEqual(dev.markers[0], dev.markers[1])

    def test_all_writes_fail(self):
        dev = self.Dev(writes_ok=(False, False, False))
        marker, bid = reboots.leave_marker(dev)
        self.assertIsNone(marker)
        self.assertEqual(bid, "aaaaaaaa-bbbb")
        self.assertEqual(len(dev.markers), 3)


class Enumeration(unittest.TestCase):
    """planned_reboot's enumeration accounting with a fake watcher (E, D4)."""

    class W:
        def __init__(self):
            self.events, self.clock = [], itertools.count(1)

        def add(self, kind, ids=None):
            t = float(next(self.clock))
            self.events.append(UsbEvent(t, 0.0, kind, ids, t - 0.05 if kind == "gone" else None))

        def mark(self):
            return len(self.events)

        def since(self, m):
            return list(self.events[m:])

        def wait_gone(self, m, seconds):
            return next(e for e in self.events[m:] if e.kind == "gone")

    class Dev:
        serial = "ZY225DPT7F"

        def __init__(self, w, stray=False, stop_fails=False):
            self.w, self.stray, self.stop_fails = w, stray, stop_fails
            self.fb, self.calls = False, []
            self.where = "android"

        def state(self):
            return "unknown"      # no marker for these tests

        def in_fastboot(self):
            return self.where == "fastboot"

        def reboot_adb(self, target=None):
            self.calls.append(("reboot_adb", target))
            self.w.add("gone")
            self.where = "fastboot" if target == "bootloader" else "android"
            self.w.add("present", "18d1:d00d" if target == "bootloader" else "18d1:4ee7")

        def reboot_sysrq(self):
            self.calls.append(("sysrq",))
            self.w.add("gone")
            self.where = "fastboot" if self.fb else "android"
            self.w.add("present", "18d1:d00d" if self.fb else "18d1:4ee7")

        def reboot_fastboot(self, target=None):
            self.calls.append(("reboot_fastboot", target))
            self.w.add("gone")
            if self.stray and target is None:
                # The bootloader enumerates before the watcher's mark could
                # be sampled: it must still count (D4).
                self.w.add("present", "18d1:d00d")
                self.w.add("gone")
            self.where = "fastboot" if target == "bootloader" or self.fb else "android"
            self.w.add("present", "18d1:d00d" if self.where == "fastboot" else "18d1:4ee7")

        def fb_mode(self, on):
            self.calls.append(("fb_mode", on))
            self.fb = on

        def fastboot_continue(self):
            self.calls.append(("continue",))
            self.where = "android"
            self.w.add("gone")
            self.w.add("present", "18d1:4ee7")

        def wait_fastboot(self, seconds):
            if self.stop_fails and ("sysrq",) in self.calls and self.where != "fastboot":
                raise TimeoutError("no fastboot")

        def wait_for_device(self, seconds):
            pass

        def wait_boot_completed(self, seconds):
            from riverqa.device import BootStatus
            return BootStatus("bid", True, 30.0, 99.9, 100.0, 0.0)

    def reboot(self, path, stray=False, dev=None, **kw):
        w = self.W()
        dev = dev or self.Dev(w, stray)
        dev.w = w
        with mock.patch.dict("os.environ", {"RIVERQA_CONTROL": "x"} if kw else {}):
            return reboots.planned_reboot(dev, path, boot_timeout=10, watcher=w, **kw), dev

    def test_normal_paths(self):
        for path in reboots.PATHS:
            rec, _ = self.reboot(path)
            self.assertEqual(rec.unplanned_fastboot, 0, path)
        self.assertEqual(self.reboot("fastboot")[0].planned_fastboot, 1)
        rec, dev = self.reboot("sysrq")
        self.assertEqual(rec.planned_fastboot, 0)
        self.assertEqual(dev.calls, [("sysrq",)])
        # The clock starts at the sysrq disconnect's last-present poll and
        # stops at the end of the status read.
        self.assertAlmostEqual(rec.elapsed, 100.0 - 0.95)
        self.assertAlmostEqual(rec.disconnect_gap, 0.05)
        self.assertAlmostEqual(rec.status_read_s, 0.1)

    def test_marked_extra_pass(self):
        for path in reboots.GATED_PATHS:
            rec, dev = self.reboot(path, inject_extra_pass=True)
            self.assertEqual(rec.unplanned_fastboot, 0, path)
            self.assertFalse(dev.fb, path)

    def test_sysrq_extra_pass_is_a_bootloader_stop(self):
        rec, dev = self.reboot("sysrq", inject_extra_pass=True)
        self.assertEqual(dev.calls, [("fb_mode", True), ("continue",), ("sysrq",), ("fb_mode", False),
                                     ("reboot_fastboot", None)])
        self.assertEqual(rec.planned_fastboot, 1)
        # The clock starts at the sysrq disconnect (event 3), not the final reboot's.
        self.assertAlmostEqual(rec.elapsed, 100.0 - 2.95)

    def test_unmarked_extra_pass_counts(self):
        for path in reboots.GATED_PATHS:
            rec, _ = self.reboot(path, inject_extra_pass=True, mark_planned=False)
            self.assertEqual(rec.unplanned_fastboot, 1, path)

    def test_adb_path_has_no_extra_pass_control(self):
        with self.assertRaises(ValueError):
            self.reboot("adb", inject_extra_pass=True)

    def test_failed_sysrq_control_clears_fb_mode(self):
        w = self.W()
        dev = self.Dev(w, stop_fails=True)
        dev.reboot_sysrq = lambda: (dev.calls.append(("sysrq",)), dev.w.add("gone"), dev.w.add("present", "18d1:4ee7"))
        with self.assertRaises(TimeoutError):
            self.reboot("sysrq", dev=dev, inject_extra_pass=True)
        self.assertFalse(dev.fb)
        self.assertEqual(dev.calls[-3:], [("reboot_adb", "bootloader"), ("fb_mode", False),
                                          ("reboot_fastboot", None)])

    def test_enumeration_right_after_the_disconnect_counts(self):
        rec, _ = self.reboot("fastboot", stray=True)
        self.assertEqual(rec.unplanned_fastboot, 1)


if __name__ == "__main__":
    unittest.main()
