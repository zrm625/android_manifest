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
        # Round 3: a copy found only with bit flips is not a PASS.
        self.assertEqual(b.chain_verdict(log2, m)[0], UNVERIFIED)
        self.assertEqual(b.chain_verdict(log2 + b"[ 99.0] " + m.encode() + b"\n", m)[0], PASS)

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


# --- amendment A1: the adb-path clock starts at the old kernel's reset ------------

import itertools  # noqa: E402
from unittest import mock  # noqa: E402

from riverqa.device import Read, UsbEvent  # noqa: E402

MARKER = "RQM:11111111:aaaaaaaa"
COPY_STEP = 0.00003      # river writes 4 copies about 30 us apart


def line(t, text):
    sec, us = divmod(round(t * 1e6), 1000000)
    return b"[%5d.%06d] " % (sec, us) + text + b"\n"


def copies_at(marker_ts, n):
    return [marker_ts + k * COPY_STEP for k in range(n)]


def shutdown_log(stamps, marker_ts=100.0, copies=1, marker=MARKER):
    """A previous-kernel log: boot noise, the marker copies, then shutdown
    lines, in the kernel's own "[%5lu.%06lu] " layout."""
    out = line(0.0, b"Linux version 4.9.337") + line(1.0, b"init")
    for t in copies_at(marker_ts, copies):
        out += line(t, marker.encode())
    for t in stamps:
        out += line(t, b"shutdown step")
    return out


def pair_for(marker_ts=100.0, copies=1, host=1000.0, hw=0.05, marker=MARKER):
    live = copies_at(marker_ts, copies)
    return {"marker": marker, "printk_ts": live[-1], "printk_ts_all": live, "host_mono": host, "half_width": hw}


def record(**kw):
    rec = reboots.BootRecord(path="adb", marker=MARKER, boot_id_before="x", marker_pair=pair_for())
    rec.disconnect_mono, rec.completed_mono, rec.uptime = 1005.0, 1050.0, 30.0
    rec.elapsed = rec.elapsed_disconnect = rec.completed_mono - rec.disconnect_mono
    for k, v in kw.items():
        setattr(rec, k, v)
    return rec


TIMING = {"paths": {"adb": {"A": 12.0, "proof_normal_pass": True, "proof_control_rejected": True,
                            "max_shutdown_tail": 6.5, "max_stamp_gap": 1.5}}}
# 100.5 ... 109.5 every 0.5 s, then the three reset lines within a millisecond
# (river: "SMP: failed to stop", "Going down for restart now", "configuring PO").
END = [110.0, 110.0003, 110.0006]
NORMAL = [100.5 + i * 0.5 for i in range(19)] + END
# The reset is the final line: host 1000 + 10.0006 - 0.05 (half-width) - 0.0006 (last-3 span).
NORMAL_ANCHOR = 1009.95


class StrictParse(unittest.TestCase):
    def test_kernel_layout_only(self):
        self.assertEqual(b.strict_stamp(b"[  100.000000] x"), 100.0)
        self.assertEqual(b.strict_stamp(b"[ 2751.918245] x"), 2751.918245)
        self.assertEqual(b.strict_stamp(b"[123456.000001] x"), 123456.000001)
        for bad in (b"[ 0100.000000] x",        # padding space flipped to '0'
                    b"[ 100.000000] x",         # wrong padding
                    b"[  100/000000] x",        # '.' flipped
                    b"[  100.00\xb0000] x",     # digit flipped to a non-digit
                    b"K  100.000000] x",        # '[' flipped
                    b"[  100.000000]\xa0x",     # the space after ']' flipped
                    b"[  100.000000] x\x8a[ 0100.500000] y"):   # a damaged stamp later in the line
            self.assertIsNone(b.strict_stamp(bad), bad)
        # A record printed without a newline: the next record's stamp is on
        # the same line, and both count (a flipped newline reads the same way).
        self.assertEqual(b.line_stamps(b"[  192.533183] remove request[  192.534649] kworker"),
                         [192.533183, 192.534649])
        self.assertEqual(b.line_stamps(b"[  100.000000] x\x8a[  100.500000] y"), [100.0, 100.5])

    def test_marker_record_is_an_exact_whole_line(self):
        log = shutdown_log(NORMAL, copies=4)
        off, ts = b.marker_record(log, MARKER)
        self.assertAlmostEqual(ts, 100.0 + 3 * COPY_STEP)          # the last intact copy
        self.assertTrue(log[off:].startswith(b"[  100.000090] " + MARKER.encode() + b"\n"))
        # A copy whose stamp or text is damaged is not a record.
        self.assertIsNone(b.marker_record(log.replace(MARKER.encode(), b"RQM:11111111:aaaaaaab"), MARKER))
        one = shutdown_log(NORMAL).replace(b"[  100.000000] RQM", b"[  100/000000] RQM")
        self.assertIsNone(b.marker_record(one, MARKER))


class CleanTail(unittest.TestCase):
    def tail(self, stamps, gap=1.5):
        log = shutdown_log(stamps)
        return b.clean_tail(log, b.marker_record(log, MARKER)[0], gap)

    def test_clean(self):
        ts, why = self.tail(NORMAL)
        self.assertEqual(ts[-1], 110.0006, why)

    def test_each_rule(self):
        cases = {
            "decrease": NORMAL[:10] + [NORMAL[9] - 0.1] + NORMAL[10:],
            "gap": [100.5, 103.0] + [103.0 + i * 0.5 for i in range(1, 12)] + [109.0, 109.0003, 109.0006],
            "end window": NORMAL[:-3] + [111.2, 111.2003, 111.2006],     # last 5 span 2.2 s
            "final window": NORMAL[:-1] + [110.07],                     # last 3 span 70 ms
            "too short": [100.5, 100.5003, 100.5006],
        }
        for name, stamps in cases.items():
            ts, why = self.tail(stamps)
            self.assertIsNone(ts, name)
            self.assertTrue(why, name)

    def test_any_damaged_line_is_unclean(self):
        log = shutdown_log(NORMAL)
        start = b.marker_record(log, MARKER)[0]
        for old, new in ((b"[  105.000000]", b"[  105\xae000000]"),       # mid-tail stamp damaged
                         (b"[  105.000000]", b"[ 0105.000000]"),
                         (b"[  105.000000]", b"K  105.000000]"),
                         (b"step\n[  105.000000]", b"step\x8a[ 0105.000000]")):
            bad = log.replace(old, new)
            self.assertIsNone(b.clean_tail(bad, start, 1.5)[0], new)
        # A missing or flipped newline alone is not a damaged stamp.
        for new in (b"step[  105.000000]", b"step\x8a[  105.000000]"):
            ts, why = b.clean_tail(log.replace(b"step\n[  105.000000]", new), start, 1.5)
            self.assertEqual(ts[-1], 110.0006, why)
        # The marker record may itself follow a record with no newline.
        joined = log.replace(b"init\n[  100.000000]", b"init[  100.000000]")
        self.assertEqual(b.marker_record(joined, MARKER)[1], 100.0)


class Anchor(unittest.TestCase):
    def judge(self, rec, log, timing=TIMING, measuring=False):
        reboots.judge_record(rec, log, timing, measuring=measuring)
        return rec

    def test_normal_boot_passes_on_the_reset_clock(self):
        rec = self.judge(record(), shutdown_log(NORMAL))
        self.assertEqual(rec.clock_source, "reset-anchor", rec.anchor_note)
        self.assertAlmostEqual(rec.anchor["printk_ts"], 110.0006)
        self.assertAlmostEqual(rec.anchor["host_mono"], NORMAL_ANCHOR)
        self.assertAlmostEqual(rec.bootloader_pass, 10.05, places=2)
        self.assertEqual(rec.uptime_check[0], PASS)

    def test_four_copies_pair_by_the_microsecond(self):
        rec = self.judge(record(marker_pair=pair_for(copies=4)), shutdown_log(NORMAL, copies=4))
        self.assertEqual(rec.uptime_check[0], PASS, rec.anchor_note)
        self.assertAlmostEqual(rec.anchor["marker_ts"], 100.00009)

    def test_extra_pass_fails(self):
        rec = self.judge(record(uptime=26.0), shutdown_log(NORMAL))
        self.assertEqual(rec.uptime_check[0], FAIL)

    def test_sol_104_25_to_104_05(self):
        # Sol's round-3 counterexample. The true tail is unclean (104.25 then
        # 104.1); the old rule anchored at 100.1. Flipping 104.250000 to
        # 104.050000 made a later run qualify, moved the anchor to 103.1 and
        # turned FAIL into PASS. Now both are UNVERIFIED with no anchor.
        true = [100.1, 100.2, 100.3, 103.1, 104.25, 104.1]
        flipped = [100.1, 100.2, 100.3, 103.1, 104.05, 104.1]
        timing = {"paths": {"adb": dict(TIMING["paths"]["adb"], max_shutdown_tail=10.0, max_stamp_gap=5.0)}}
        for tail in (true, flipped):
            for measuring in (False, True):
                rec = self.judge(record(uptime=36.0), shutdown_log(tail), timing, measuring)
                self.assertEqual(rec.uptime_check[0], UNVERIFIED, (tail, measuring))
                self.assertIsNone(rec.anchor)
                self.assertIn("not clean", rec.anchor_note)

    def test_nonce_one_character_off_is_unverified(self):
        # The log holds a marker one nonce character away from this boot's
        # (another write). Fuzzy matching used to take it and PASS.
        other = "RQM:11111112:aaaaaaaa"
        log = shutdown_log(NORMAL, copies=4, marker=other)
        rec = self.judge(record(marker_pair=pair_for(copies=4)), log)
        self.assertEqual(rec.chain[0], UNVERIFIED)
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)
        self.assertIsNone(rec.anchor)
        anchor, why = b.reset_anchor(log, MARKER, pair_for(copies=4), None, 1.5)
        self.assertIsNone(anchor)
        self.assertIn("no intact exact copy", why)

    def test_copy_mixing(self):
        # Copy 1 has a good stamp but a damaged nonce; copy 2 has the exact
        # text but a damaged stamp. Nothing may combine the two.
        log = shutdown_log(NORMAL, copies=2, marker=MARKER)
        log = log.replace(b"[  100.000000] " + MARKER.encode(), b"[  100.000000] RQM:11111311:aaaaaaaa")
        log = log.replace(b"[  100.000030] ", b"[  100.0\xb00030] ")
        anchor, why = b.reset_anchor(log, MARKER, pair_for(copies=2), None, 1.5)
        self.assertIsNone(anchor)
        self.assertIn("no intact exact copy", why)
        # The exact copy's stamp is strict but flipped (30 -> 70 us): it is
        # not one of the live stamps, so it is not the same write.
        log2 = shutdown_log(NORMAL, copies=2).replace(b"[  100.000030] ", b"[  100.000070] ")
        anchor, why = b.reset_anchor(log2, MARKER, pair_for(copies=2), None, 1.5)
        self.assertIsNone(anchor)
        self.assertIn("not the same write", why)

    def test_every_single_bit_flip_never_moves_the_anchor_later(self):
        # Flip each bit of each byte from the marker copies to the end, one
        # at a time. A flip may make the boot UNVERIFIED, never give a later
        # anchor. Any intact copy gives a valid earliest time (every copy was
        # written inside the paired bracket), so the oracle is the true
        # final stamp mapped through the earliest copy.
        pair = pair_for(copies=4)
        log = shutdown_log(NORMAL, copies=4)
        start = log.index(b"[  100.000000]")
        oracle = pair["host_mono"] - pair["half_width"] + (NORMAL[-1] - min(pair["printk_ts_all"]))
        clean, _ = b.reset_anchor(log, MARKER, pair, None, 1.5)
        self.assertLessEqual(clean["host_mono"], oracle + 1e-9)
        anchored = 0
        for i in range(start, len(log)):
            for bit in range(8):
                bad = bytearray(log)
                bad[i] ^= 1 << bit
                got, _ = b.reset_anchor(bytes(bad), MARKER, pair, None, 1.5)
                if got is not None:
                    anchored += 1
                    self.assertLessEqual(got["host_mono"], oracle + 1e-9, (i, bit, bytes(bad[i - 8:i + 8])))
        self.assertGreater(anchored, 0)        # flips in message text leave the anchor alone

    def test_flips_that_still_parse_do_not_pass(self):
        # 110.000600 read back as 118.000600, and 41.1 as 45.1: never PASS.
        log = shutdown_log(NORMAL[:-1] + [118.0006])
        for measuring in (False, True):
            rec = self.judge(record(uptime=26.0), log, TIMING, measuring)
            self.assertNotEqual(rec.uptime_check[0], PASS)
        tail = [30.5 + i * 0.5 for i in range(21)] + [41.1, 41.1003, 41.1006]
        timing = {"paths": {"adb": dict(TIMING["paths"]["adb"], max_shutdown_tail=7.0)}}
        good = shutdown_log(tail, marker_ts=30.0)
        bad = shutdown_log(tail[:-1] + [45.1006], marker_ts=30.0)
        r1 = self.judge(record(marker_pair=pair_for(30.0), uptime=24.9), good, timing)
        self.assertEqual(r1.uptime_check[0], FAIL, r1.anchor_note)
        r2 = self.judge(record(marker_pair=pair_for(30.0), uptime=24.9), bad, timing)
        self.assertNotEqual(r2.uptime_check[0], PASS)

    def test_flip_inside_the_final_window_cannot_move_it_later(self):
        # 110.000600 -> 110.040600 ('0' -> '4'): still clean, but the last-3
        # span is taken off, so the anchor is no later than the true one.
        rec = self.judge(record(), shutdown_log(NORMAL[:-1] + [110.0406]))
        self.assertEqual(rec.clock_source, "reset-anchor")
        self.assertLessEqual(rec.anchor["host_mono"], NORMAL_ANCHOR + 1e-9)

    def test_silent_tail_only_makes_it_stricter(self):
        # A quiet stretch S between the last printk line and the real reset
        # (a slower shutdown that logs nothing) shifts the new kernel's start
        # later by S, so uptime is S smaller while the anchor stays at the
        # last line: the measured pass grows by S. It can turn PASS into
        # FAIL, never FAIL into PASS (DESIGN.md, A1 rule 2).
        last = None
        for s in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 8.0):
            rec = self.judge(record(uptime=30.0 - s), shutdown_log(NORMAL))
            self.assertAlmostEqual(rec.bootloader_pass, 10.05 + s, places=2)
            if last == FAIL:
                self.assertEqual(rec.uptime_check[0], FAIL, s)
            last = rec.uptime_check[0]
        self.assertEqual(last, FAIL)

    def test_missing_marker_is_unverified_never_the_disconnect(self):
        log = line(0.0, b"Linux version 4.9.337") + b"".join(line(t, b"x") for t in NORMAL)
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

    def test_no_calibrated_tail_or_gap_is_unverified(self):
        for key in ("max_shutdown_tail", "max_stamp_gap"):
            timing = {"paths": {"adb": {k: v for k, v in TIMING["paths"]["adb"].items() if k != key}}}
            rec = self.judge(record(), shutdown_log(NORMAL), timing)
            self.assertEqual(rec.uptime_check[0], UNVERIFIED, key)

    def test_gap_over_the_calibrated_bound_is_unverified(self):
        timing = {"paths": {"adb": dict(TIMING["paths"]["adb"], max_stamp_gap=0.4)}}
        rec = self.judge(record(), shutdown_log(NORMAL), timing)         # gaps are 0.5 s
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)
        self.assertIn("exceeds", rec.anchor_note)

    def test_anchor_before_marker_is_refused(self):
        anchor, why = b.reset_anchor(shutdown_log([99.0, 99.1, 99.2, 99.3, 99.3001, 99.3002]), MARKER,
                                     pair_for(host=0.0, hw=0.0), None, 1.5)
        self.assertIsNone(anchor)

    def test_fastboot_path_keeps_the_disconnect_clock(self):
        t = {"paths": {"fastboot": {"A": 12.0, "proof_normal_pass": True, "proof_control_rejected": True}}}
        rec = self.judge(record(path="fastboot", marker_pair=None, uptime=35.0), None, t)
        self.assertEqual(rec.clock_source, "disconnect")
        self.assertEqual(rec.uptime_check[0], PASS)       # 35 >= 45 - 12
        rec = self.judge(record(path="fastboot", marker_pair=None, uptime=30.0), None, t)
        self.assertEqual(rec.uptime_check[0], FAIL)       # 30 < 45 - 12


class ChainExact(unittest.TestCase):
    def test_exact_copy_needed_for_pass(self):
        log = shutdown_log(NORMAL)
        self.assertEqual(b.chain_verdict(log, MARKER)[0], PASS)
        flipped = log.replace(MARKER.encode(), b"RQM:11111111:aaaaaaab")
        self.assertEqual(b.chain_verdict(flipped, MARKER)[0], UNVERIFIED)
        # A new session after a damaged copy is still a FAIL.
        self.assertEqual(b.chain_verdict(flipped + line(0.0, b"Linux version 4.9.337"), MARKER)[0], FAIL)


def raw(path, bootloader_pass, tail=None, first_after_wipe=False, anchored=True):
    r = {"bootloader_pass": bootloader_pass, "chain": [PASS, ""], "marker_copies_exact": 4,
         "marker_found_fuzzy": True, "first_after_wipe": first_after_wipe}
    if path == "adb":
        r["clock_source"] = "reset-anchor" if anchored else "none"
        r["anchor"] = {"tail_s": tail, "max_gap": 1.1} if anchored else None
        r["anchor_note"] = "" if anchored else "chain is UNVERIFIED, so there is no reset anchor"
    return r


class Calibration(unittest.TestCase):
    """compute_paths and the report (R2-7)."""

    def data(self, adb, inj_adb=None, fastboot=None):
        return {"raw": {"adb": adb, "fastboot": fastboot if fastboot is not None else
                        [raw("fastboot", 10.02), raw("fastboot", 10.05)]},
                "inject_measure": {"adb": inj_adb or raw("adb", 13.8, 4.0), "fastboot": raw("fastboot", 14.0)},
                "paths": {}, "chain": {}, "controls": {}}

    def test_tail_bound_includes_the_first_reboot_after_a_wipe(self):
        from riverqa import calibrate
        d = self.data([raw("adb", 10.0, 6.4, first_after_wipe=True), raw("adb", 10.01, 4.1),
                       raw("adb", 10.01, 4.0)])
        calibrate.compute_paths(d)
        self.assertEqual(d["paths"]["adb"]["max_shutdown_tail"], 6.4)
        self.assertEqual(d["paths"]["adb"]["first_after_wipe_tails"], [6.4])
        self.assertEqual(d["paths"]["adb"]["max_stamp_gap"], 2.1)     # largest gap + 1 s margin

    def test_no_first_reboot_after_a_wipe_means_no_bound(self):
        from riverqa import calibrate
        for adb in ([raw("adb", 10.0, 4.1), raw("adb", 10.01, 4.0)],
                    [raw("adb", 10.0, first_after_wipe=True, anchored=False), raw("adb", 10.01, 4.0),
                     raw("adb", 10.02, 4.1)]):
            d = self.data(adb)
            calibrate.compute_paths(d)
            self.assertIsNone(d["paths"]["adb"]["max_shutdown_tail"])
            self.assertIn("flash --wipe", d["paths"]["adb"]["tail_why"])
            self.assertTrue(any("no shutdown-tail bound" in line for line in calibrate.summary(d)))

    def test_slower_tail_than_calibrated_is_unverified(self):
        # Calibrated bound 6.5 s; this boot's reset came 7.45 s after the
        # disconnect (a slower first-after-wipe shutdown): UNVERIFIED, never PASS.
        rec = record(disconnect_mono=1002.0)
        reboots.judge_record(rec, shutdown_log(NORMAL), TIMING)
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)
        self.assertIn("shutdown tail", rec.anchor_note)

    def test_no_usable_anchors_reports_the_reason(self):
        from riverqa import calibrate
        d = self.data([raw("adb", 16.5, anchored=False), raw("adb", 16.6, anchored=False)],
                      inj_adb=raw("adb", 20.4, anchored=False))
        calibrate.compute_paths(d)
        p = d["paths"]["adb"]
        self.assertIsNone(p["A"])
        self.assertIn("no usable adb reboots", p["why"])
        self.assertIn("chain is UNVERIFIED", p["why"])
        lines = calibrate.summary(d)            # no KeyError
        self.assertTrue(any("adb" in line and "not qualified" in line for line in lines))
        self.assertFalse(calibrate.qualified(d))
        # Nothing at all measured.
        empty = {"raw": {"adb": [], "fastboot": []}, "inject_measure": {}, "paths": {}, "chain": {}}
        calibrate.compute_paths(empty)
        calibrate.summary(empty)
        self.assertFalse(calibrate.qualified(empty))

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
        rec = self.judge((0, shutdown_log(NORMAL)))
        self.assertEqual(rec.chain[0], PASS)
        self.assertEqual(rec.uptime_check[0], PASS)

    def test_failed_or_partial_read_is_unverified(self):
        log = shutdown_log(NORMAL)
        import base64
        cut = f"__FILE__ {len(log) + 100} /sys/fs/pstore/console-ramoops-0\n" + \
            base64.encodebytes(log).decode() + "__EOF__\n__RC__ 1\n"
        for files in ((1, log), (0, cut), (0, b"")):
            rec = self.judge(files)
            self.assertEqual(rec.chain[0], UNVERIFIED, files[0])
            self.assertEqual(rec.uptime_check[0], UNVERIFIED)
            self.assertTrue(rec.pstore_read_error)
            sig = {"chain": list(rec.chain), "uptime": list(rec.uptime_check), "boot_count": [PASS, "ok"],
                   "reboot": rec.to_json()}
            # Even on a path whose chain "doesn't survive", a failed read is UNVERIFIED.
            self.assertEqual(reboots.combined_verdict(sig, "adb", {"chain": {"adb": {"survives": False}}})[0],
                             UNVERIFIED)


class Pairing(unittest.TestCase):
    class Dev:
        """kmsg() results are taken from ``writes_ok`` in order; dmesg holds
        every marker whose write succeeded, at ``dmesg_ts`` + 1 s per write."""

        def __init__(self, dmesg_ts=100.25, writes_ok=(True, True, True)):
            self.ts, self.ok = dmesg_ts, list(writes_ok)
            self.markers, self.dmesg, self.reads = [], [], []

        def root(self):
            pass

        def boot_id(self):
            return "aaaaaaaa-bbbb"

        def kmsg(self, text):
            self.markers.append(text)
            ok = self.ok.pop(0)
            if ok and self.ts is not None:
                self.dmesg.append(f"[{self.ts + len(self.dmesg):11.6f}] {text}")
            return ok

        def read(self, cmd, timeout=30):
            self.reads.append(cmd)
            want = cmd.rsplit(" ", 1)[1]
            return Read(0, "".join(line + "\n" for line in self.dmesg if want in line))

    def test_marker_paired_with_its_printk_timestamp(self):
        dev = self.Dev()
        with mock.patch.object(reboots.time, "monotonic", side_effect=[50.0, 50.1]):
            marker, bid, pair = reboots.leave_marker(dev)
        self.assertEqual(pair["printk_ts"], 100.25)
        self.assertEqual(pair["printk_ts_all"], [100.25])
        self.assertEqual(pair["marker"], marker)
        self.assertAlmostEqual(pair["host_mono"], 50.05)
        self.assertAlmostEqual(pair["half_width"], 0.05)

    def test_slow_write_is_retried_then_unpaired(self):
        dev = self.Dev()
        with mock.patch.object(reboots.time, "monotonic", side_effect=[0.0, 1.0, 2.0, 3.0, 4.0, 5.0]):
            marker, bid, pair = reboots.leave_marker(dev)
        self.assertEqual(len(dev.markers), 3)
        self.assertEqual(len(set(dev.markers)), 3)        # a new nonce for every attempt
        self.assertEqual(marker, dev.markers[-1])
        self.assertIsNone(pair)

    def test_no_readback_is_unpaired(self):
        with mock.patch.object(reboots.time, "monotonic", side_effect=[0.0, 0.1]):
            self.assertIsNone(reboots.leave_marker(self.Dev(dmesg_ts=None))[2])

    def test_failed_retry_does_not_pair(self):
        # Attempt 1 is written but slow; attempts 2 and 3 are fast but their
        # writes fail. Nothing may pair attempt 1's printk time with a later
        # host interval, and the leaving marker is the one really written.
        dev = self.Dev(writes_ok=(True, False, False))
        with mock.patch.object(reboots.time, "monotonic", side_effect=[0.0, 1.0, 2.0, 2.1, 3.0, 3.1]):
            marker, bid, pair = reboots.leave_marker(dev)
        self.assertIsNone(pair)
        self.assertEqual(marker, dev.markers[0])
        self.assertEqual(dev.reads, [])

    def test_retry_pairs_only_its_own_write(self):
        dev = self.Dev(writes_ok=(True, True))
        with mock.patch.object(reboots.time, "monotonic", side_effect=[0.0, 1.0, 2.0, 2.1]):
            marker, bid, pair = reboots.leave_marker(dev)
        self.assertEqual(marker, dev.markers[1])
        self.assertNotEqual(dev.markers[0], dev.markers[1])
        self.assertEqual(pair["marker"], marker)
        self.assertEqual(pair["printk_ts"], 101.25)        # attempt 2's own stamp, not attempt 1's
        self.assertAlmostEqual(pair["host_mono"], 2.05)

    def test_all_writes_fail(self):
        with mock.patch.object(reboots.time, "monotonic", side_effect=[0.0, 0.1] * 3):
            marker, bid, pair = reboots.leave_marker(self.Dev(writes_ok=(False, False, False)))
        self.assertIsNone(marker)
        self.assertIsNone(pair)


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


if __name__ == "__main__":
    unittest.main()
