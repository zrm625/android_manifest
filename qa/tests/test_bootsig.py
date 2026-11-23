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
                            "max_shutdown_tail": 6.5}}}
# 100.5 ... 109.5 every 0.5 s, then the three reset lines within a millisecond
# (river: "SMP: failed to stop", "Going down for restart now", "configuring PO").
END = [110.0, 110.0003, 110.0006]
NORMAL = [100.5 + i * 0.5 for i in range(19)] + END
# The reset is the final line: host 1000 + 10.0006 - 0.05 (half-width) - 0.0006 (last-3 span).
NORMAL_ANCHOR = 1009.95


def flip_bytes(text: bytes, positions) -> bytes:
    """``text`` with one bit flipped in each byte at ``positions``."""
    out = bytearray(text)
    for p in positions:
        out[p] ^= 0x04
    return bytes(out)


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


class ProvenMarker(unittest.TestCase):
    """A1′ rules 1-2: find copies tolerantly, prove one by its stamp."""

    def test_last_proven_copy(self):
        log = shutdown_log(NORMAL, copies=4)
        (off, ts), _ = b.proven_marker(log, MARKER, copies_at(100.0, 4))
        self.assertAlmostEqual(ts, 100.0 + 3 * COPY_STEP)          # the last proven copy
        self.assertTrue(log[off:].startswith(MARKER.encode()))
        self.assertEqual(log[off - 15:off], b"[  100.000090] ")

    def test_damaged_text_within_three_flips_is_found(self):
        for n in (1, 2, 3):
            log = shutdown_log(NORMAL).replace(MARKER.encode(), flip_bytes(MARKER.encode(), range(4, 4 + n)))
            rec, why = b.proven_marker(log, MARKER, [100.0])
            self.assertIsNotNone(rec, (n, why))
        log = shutdown_log(NORMAL).replace(MARKER.encode(), flip_bytes(MARKER.encode(), range(4, 8)))
        self.assertIsNone(b.proven_marker(log, MARKER, [100.0])[0])     # 4 flips: not a copy

    def test_damaged_stamp_is_not_proof(self):
        for bad in (b"[  100/000000] RQM", b"[ 0100.000000] RQM", b"[  100.000000]\xa0RQM"):
            log = shutdown_log(NORMAL).replace(b"[  100.000000] RQM", bad)
            rec, why = b.proven_marker(log, MARKER, [100.0])
            self.assertIsNone(rec, bad)
            self.assertIn("not proven the same write", why)

    def test_stamp_off_by_one_microsecond_is_rejected(self):
        log = shutdown_log(NORMAL, marker_ts=100.000001)
        rec, why = b.proven_marker(log, MARKER, [100.0, 100.000002])
        self.assertIsNone(rec)
        self.assertIn("100.000001", why)

    def test_merged_line_uses_the_stamp_next_to_the_marker(self):
        # The newline before the marker flipped, so its line starts with an
        # earlier message: only the stamp next to the marker counts.
        for joined in (b"init[  100.000000]", b"init\x8a[  100.000000]"):
            log = shutdown_log(NORMAL).replace(b"init\n[  100.000000]", joined)
            self.assertEqual(b.proven_marker(log, MARKER, [100.0])[0][1], 100.0)
        # An earlier stamp on the line is never used in place of a damaged one.
        log = shutdown_log(NORMAL).replace(b"init\n[  100.000000] RQM", b"init [    1.000000]\x8a  RQM")
        self.assertIsNone(b.proven_marker(log, MARKER, [1.0, 100.0])[0])


class FinalLines(unittest.TestCase):
    """A1′ rule 3: the reset comes only from the final lines."""

    def final(self, log):
        (off, _ts), _ = b.proven_marker(log, MARKER, [100.0])
        return b.final_lines(log, off + len(MARKER))

    def test_clean(self):
        ts, why = self.final(shutdown_log(NORMAL))
        self.assertEqual(ts, [109.0, 109.5, 110.0, 110.0003, 110.0006], why)

    def test_each_rule(self):
        cases = {
            "decrease": NORMAL[:-2] + [109.9, 110.0006],
            "end window": NORMAL[:-3] + [111.2, 111.2003, 111.2006],     # last 5 span 2.2 s
            "final window": NORMAL[:-1] + [110.07],                     # last 3 span 70 ms
            "too short": [100.5, 100.5003, 100.5006],
        }
        for name, stamps in cases.items():
            ts, why = self.final(shutdown_log(stamps))
            self.assertIsNone(ts, name)
            self.assertTrue(why, name)

    def test_damaged_lines_before_the_final_5_are_ignored(self):
        log = shutdown_log(NORMAL)
        for old, new in ((b"[  105.000000]", b"[  105\xae000000]"),
                         (b"[  105.000000]", b"[ 0105.000000]"),
                         (b"[  105.000000]", b"K  105.000000]"),
                         (b"[  105.000000]", b"[  145.000000]"),        # still parses, out of order
                         (b"step\n[  105.000000]", b"step\x8a[ 0105.000000]"),
                         (b"step\n[  105.000000]", b"step[  105.000000]"),
                         (b"[  108.500000]", b"[  108.5\x80\x80\x80\x80\x80]")):
            ts, why = self.final(log.replace(old, new))
            self.assertEqual(ts, [109.0, 109.5, 110.0, 110.0003, 110.0006], (new, why))

    def test_a_long_quiet_stretch_before_the_final_lines_is_fine(self):
        # No consecutive-gap rule (R3-1's max_stamp_gap is gone): only the
        # final lines decide.
        ts, why = self.final(shutdown_log([100.5, 107.0, 109.0, 109.5] + END))
        self.assertEqual(ts[-1], 110.0006, why)


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
        self.assertAlmostEqual(rec.reset_tail_s, NORMAL_ANCHOR - 1005.0, places=3)

    def test_four_copies_pair_by_the_microsecond(self):
        rec = self.judge(record(marker_pair=pair_for(copies=4)), shutdown_log(NORMAL, copies=4))
        self.assertEqual(rec.uptime_check[0], PASS, rec.anchor_note)
        self.assertAlmostEqual(rec.anchor["marker_ts"], 100.00009)

    def test_extra_pass_fails(self):
        rec = self.judge(record(uptime=26.0), shutdown_log(NORMAL))
        self.assertEqual(rec.uptime_check[0], FAIL)

    def test_real_style_dump_all_copies_damaged(self):
        # A1′ data: on a real dump the 4 copies had 2, 1, 2 and 4 wrong
        # bytes. The 4-byte one is not a copy; the stamps of copies 1 and 3
        # are damaged; copy 2 keeps a stamp equal to a live one: it is used.
        m = MARKER.encode()
        log = line(0.0, b"Linux version 4.9.337") + line(1.0, b"init")
        log += b"[  100.00\xb0000] " + flip_bytes(m, (5, 17)) + b"\n"
        log += b"[  100.000030] " + flip_bytes(m, (9,)) + b"\n"
        log += b"[\xa0 100.000060] " + flip_bytes(m, (4, 20)) + b"\n"
        log += b"[  100.000090] " + flip_bytes(m, (4, 8, 12, 16)) + b"\n"
        log += b"".join(line(t, b"shutdown step") for t in NORMAL)
        self.assertEqual(log.count(m), 0)
        rec = self.judge(record(marker_pair=pair_for(copies=4)), log)
        self.assertEqual(rec.chain[0], PASS, rec.chain)
        self.assertEqual(rec.clock_source, "reset-anchor", rec.anchor_note)
        self.assertAlmostEqual(rec.anchor["marker_ts"], 100.00003)
        self.assertEqual(rec.uptime_check[0], PASS)
        # Without the live readback the damaged copies prove nothing.
        self.assertEqual(b.chain_verdict(log, MARKER)[0], UNVERIFIED)

    def test_river_step1_dump(self):
        # The 2026-11-23 step-1 log behind 1faf721: the only exact copy has a
        # damaged stamp; a fuzzy copy's stamp is 1 us off every live stamp
        # (rejected, where 1faf721's 50 ms check took it); the third copy's
        # stamp equals a live one, so it is the record.
        log = line(0.0, b"Linux version 4.9.337") + line(1.0, b"init")
        log += b"[\xa0 100.00\xb3000] " + MARKER.encode() + b"\n"
        log += line(100.000031, MARKER.replace(":1111", ":1i11").encode())
        log += line(100.00006, MARKER.replace(":", "\xaa", 1).encode("latin-1"))
        log += b"".join(line(t, b"shutdown step") for t in NORMAL)
        pair = pair_for(copies=3)
        anchor, why = b.reset_anchor(log, MARKER, pair)
        self.assertAlmostEqual(anchor["marker_ts"], 100.00006, msg=why)
        rec = self.judge(record(marker_pair=pair), log)
        self.assertEqual(rec.uptime_check[0], PASS, rec.anchor_note)
        # Only the 1-us-off copy left: rejected.
        log2 = log.replace(MARKER.replace(":", "\xaa", 1).encode("latin-1"), b"other message here...")
        anchor, why = b.reset_anchor(log2, MARKER, pair)
        self.assertIsNone(anchor)
        self.assertIn("not proven the same write", why)

    def test_sol_104_25_to_104_05(self):
        # Sol's round-3 counterexample. The true tail is unclean (104.25 then
        # 104.1); the old rule anchored at 100.1. Flipping 104.250000 to
        # 104.050000 made a later run qualify, moved the anchor to 103.1 and
        # turned FAIL into PASS. Under A1′ both final-line sets fail rule 3.
        true = [100.1, 100.2, 100.3, 103.1, 104.25, 104.1]
        flipped = [100.1, 100.2, 100.3, 103.1, 104.05, 104.1]
        timing = {"paths": {"adb": dict(TIMING["paths"]["adb"], max_shutdown_tail=10.0)}}
        for tail in (true, flipped):
            for measuring in (False, True):
                rec = self.judge(record(uptime=36.0), shutdown_log(tail), timing, measuring)
                self.assertEqual(rec.uptime_check[0], UNVERIFIED, (tail, measuring))
                self.assertIsNone(rec.anchor)
                self.assertIn("final lines give no reset", rec.anchor_note)

    def test_nonce_one_character_off_is_unverified(self):
        # The log holds only another write's marker, one nonce character
        # away from this boot's. Its copies are found (1 flip) but their
        # stamps belong to that write, not this one: never used.
        other = "RQM:11111112:aaaaaaaa"
        log = shutdown_log(NORMAL, marker_ts=99.0, copies=4, marker=other)
        rec = self.judge(record(marker_pair=pair_for(copies=4)), log)
        self.assertEqual(rec.chain[0], UNVERIFIED)
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)
        self.assertIsNone(rec.anchor)
        anchor, why = b.reset_anchor(log, MARKER, pair_for(copies=4))
        self.assertIsNone(anchor)
        self.assertIn("not proven the same write", why)
        # Both writes in the log: only this write's copy is the record.
        both = shutdown_log(NORMAL, copies=1).replace(
            b"[  100.000000] ", b"[   99.000000] " + other.encode() + b"\n[  100.000000] ")
        anchor, why = b.reset_anchor(both, MARKER, pair_for())
        self.assertEqual(anchor["marker_ts"], 100.0, why)

    def test_copy_mixing(self):
        # Copy 1 has the exact text but a damaged stamp; copy 2 has its live
        # stamp but text 4 flips away (not a copy). Nothing may combine the
        # stamp of one with the text of the other.
        m = MARKER.encode()
        log = shutdown_log(NORMAL, copies=2)
        log = log.replace(b"[  100.000000] " + m, b"[  100.0\xb00000] " + m)
        log = log.replace(b"[  100.000030] " + m, b"[  100.000030] " + flip_bytes(m, (4, 8, 12, 16)))
        anchor, why = b.reset_anchor(log, MARKER, pair_for(copies=2))
        self.assertIsNone(anchor)
        self.assertIn("not proven the same write", why)
        # Copy 1: live stamp, one flipped nonce byte. Copy 2: exact text, a
        # strict stamp that is not live (30 -> 70 us). Every value comes from
        # copy 1, including its place in the log.
        log2 = shutdown_log(NORMAL, copies=2)
        log2 = log2.replace(b"[  100.000000] " + m, b"[  100.000000] RQM:11111311:aaaaaaaa")
        log2 = log2.replace(b"[  100.000030] ", b"[  100.000070] ")
        anchor, why = b.reset_anchor(log2, MARKER, pair_for(copies=2))
        self.assertEqual(anchor["marker_ts"], 100.0, why)
        self.assertEqual(log2[anchor["marker_offset"] - 15:anchor["marker_offset"]], b"[  100.000000] ")
        # A single exact copy whose stamp is not live: not the same write.
        log3 = shutdown_log(NORMAL).replace(b"[  100.000000] ", b"[  100.000040] ")
        anchor, why = b.reset_anchor(log3, MARKER, pair_for())
        self.assertIsNone(anchor)
        self.assertIn("not proven the same write", why)

    def flips(self, log, start):
        for i in range(start, len(log)):
            for bit in range(8):
                bad = bytearray(log)
                bad[i] ^= 1 << bit
                yield i, bit, bytes(bad)

    def test_every_single_bit_flip_never_moves_the_anchor_later(self):
        # Flip each bit of each byte from the marker copies to the end, one
        # at a time. A flip may make the boot UNVERIFIED, never give a later
        # anchor. Any proven copy gives a valid earliest time (every copy was
        # written inside the paired bracket), so the oracle is the true
        # final stamp mapped through the earliest copy.
        pair = pair_for(copies=4)
        log = shutdown_log(NORMAL, copies=4)
        start = log.index(b"[  100.000000]")
        oracle = pair["host_mono"] - pair["half_width"] + (NORMAL[-1] - min(pair["printk_ts_all"]))
        clean, _ = b.reset_anchor(log, MARKER, pair)
        self.assertLessEqual(clean["host_mono"], oracle + 1e-9)
        anchored = 0
        for i, bit, bad in self.flips(log, start):
            got, _ = b.reset_anchor(bad, MARKER, pair)
            if got is not None:
                anchored += 1
                self.assertLessEqual(got["host_mono"], oracle + 1e-9, (i, bit, bad[i - 8:i + 8]))
        self.assertGreater(anchored, 0)        # flips in message text leave the anchor alone

    def test_every_single_bit_flip_never_passes_an_extra_pass(self):
        # The same sweep on a boot with an extra bootloader pass (FAIL when
        # clean): no single flip from the marker to the end gives PASS.
        pair = pair_for(copies=4)
        log = shutdown_log(NORMAL, copies=4)
        start = log.index(b"[  100.000000]")
        self.assertEqual(self.judge(record(marker_pair=pair, uptime=26.0), log).uptime_check[0], FAIL)
        for i, bit, bad in self.flips(log, start):
            for measuring in (False, True):
                rec = self.judge(record(marker_pair=pair, uptime=26.0), bad, TIMING, measuring)
                self.assertNotEqual(rec.uptime_check[0], PASS, (i, bit, bad[i - 8:i + 8]))

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
        # 110.000600 -> 110.040600 ('0' -> '4'): still within 50 ms, but the
        # last-3 span is taken off, so the anchor is no later than the true one.
        rec = self.judge(record(), shutdown_log(NORMAL[:-1] + [110.0406]))
        self.assertEqual(rec.clock_source, "reset-anchor")
        self.assertLessEqual(rec.anchor["host_mono"], NORMAL_ANCHOR + 1e-9)

    def test_flip_in_the_final_3_past_50ms_is_unverified(self):
        # A flip that pushes one of the final 3 lines more than 50 ms later.
        for stamps in (NORMAL[:-1] + [110.1006],                 # the final line: last 3 span 100 ms
                       NORMAL[:-2] + [110.1003, 110.0006],       # the middle one: the stamps decrease
                       NORMAL[:-3] + [110.08, 110.0003, 110.0006]):
            for measuring in (False, True):
                rec = self.judge(record(), shutdown_log(stamps), TIMING, measuring)
                self.assertEqual(rec.uptime_check[0], UNVERIFIED, stamps)
                self.assertIsNone(rec.anchor)

    def test_silent_tail_only_makes_it_stricter(self):
        # A quiet stretch S between the last printk line and the real reset
        # (a slower shutdown that logs nothing) shifts the new kernel's start
        # later by S, so uptime is S smaller while the anchor stays at the
        # last line: the measured pass grows by S. It can turn PASS into
        # FAIL, never FAIL into PASS (DESIGN.md, A1 rule 2, A1′ rule 5).
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
        self.assertAlmostEqual(rec.reset_tail_s, 4.95, places=2)    # still recorded for the samples
        rec = self.judge(record(disconnect_mono=1011.0), shutdown_log(NORMAL))   # reset before the disconnect
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)

    def test_no_calibrated_tail_is_unverified(self):
        timing = {"paths": {"adb": {k: v for k, v in TIMING["paths"]["adb"].items() if k != "max_shutdown_tail"}}}
        rec = self.judge(record(), shutdown_log(NORMAL), timing)
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)
        self.assertIn("no calibrated shutdown-tail bound", rec.anchor_note)

    def test_anchor_before_marker_is_refused(self):
        anchor, why = b.reset_anchor(shutdown_log([99.0, 99.1, 99.2, 99.3, 99.3001, 99.3002]), MARKER,
                                     pair_for(host=0.0, hw=0.0))
        self.assertIsNone(anchor)
        self.assertIn("before the marker", why)

    def test_fastboot_path_keeps_the_disconnect_clock(self):
        t = {"paths": {"fastboot": {"A": 12.0, "proof_normal_pass": True, "proof_control_rejected": True}}}
        rec = self.judge(record(path="fastboot", marker_pair=None, uptime=35.0), None, t)
        self.assertEqual(rec.clock_source, "disconnect")
        self.assertEqual(rec.uptime_check[0], PASS)       # 35 >= 45 - 12
        rec = self.judge(record(path="fastboot", marker_pair=None, uptime=30.0), None, t)
        self.assertEqual(rec.uptime_check[0], FAIL)       # 30 < 45 - 12


class Chain(unittest.TestCase):
    def test_exact_copy_or_a_proven_copy_needed_for_pass(self):
        log = shutdown_log(NORMAL)
        self.assertEqual(b.chain_verdict(log, MARKER)[0], PASS)
        flipped = log.replace(MARKER.encode(), b"RQM:11111111:aaaaaaab")
        self.assertEqual(b.chain_verdict(flipped, MARKER)[0], UNVERIFIED)
        # A1′: the damaged copy's stamp equals a live stamp of this write.
        self.assertEqual(b.chain_verdict(flipped, MARKER, pair_for())[0], PASS)
        self.assertEqual(b.chain_verdict(flipped, MARKER, pair_for(marker_ts=100.000001))[0], UNVERIFIED)
        self.assertEqual(b.chain_verdict(flipped, MARKER, pair_for(marker="RQM:11111111:aaaaaaab"))[0],
                         UNVERIFIED)
        # A new session after a damaged copy, proven or not, is a FAIL.
        new = flipped + line(0.0, b"Linux version 4.9.337")
        self.assertEqual(b.chain_verdict(new, MARKER)[0], FAIL)
        self.assertEqual(b.chain_verdict(new, MARKER, pair_for())[0], FAIL)


def raw(path, bootloader_pass, tail=None, first_after_wipe=False, anchored=True):
    r = {"bootloader_pass": bootloader_pass, "chain": [PASS, ""], "marker_copies_exact": 4,
         "marker_found_fuzzy": True, "first_after_wipe": first_after_wipe}
    if path == "adb":
        r["clock_source"] = "reset-anchor" if anchored else "none"
        r["anchor"] = {"tail_s": tail} if anchored else None
        r["reset_tail_s"] = tail if anchored else None
        r["anchor_note"] = "" if anchored else "chain is UNVERIFIED, so there is no reset anchor"
    return r


def samples(*tails):
    return [{"wipe_seq": i + 1, "tail_s": t} for i, t in enumerate(tails)]


DROPBOX0 = "Drop box contents: 0 entries\n"
DROPBOX1 = "Drop box contents: 1 entries\n\n2026-11-22 18:44:04 SYSTEM_BOOT (text, 443 bytes)\n"


class Calibration(unittest.TestCase):
    """compute_paths and the report (R2-7, A1′ rule 4)."""

    def data(self, adb, inj_adb=None, fastboot=None, tails=(8.52, 9.08, 9.17)):
        return {"raw": {"adb": adb, "fastboot": fastboot if fastboot is not None else
                        [raw("fastboot", 10.02), raw("fastboot", 10.05)]},
                "inject_measure": {"adb": inj_adb or raw("adb", 13.8, 4.0), "fastboot": raw("fastboot", 14.0)},
                "paths": {}, "chain": {}, "controls": {}, "tail_samples": samples(*tails)}

    def test_tail_bound_is_the_largest_of_3_samples_plus_a_margin(self):
        from riverqa import calibrate
        d = self.data([raw("adb", 10.0, 9.0, first_after_wipe=True), raw("adb", 10.01, 4.1),
                       raw("adb", 10.01, 4.0)])
        calibrate.compute_paths(d)
        self.assertEqual(d["paths"]["adb"]["max_shutdown_tail"], 10.17)       # 9.17 + 1.0
        self.assertEqual(d["paths"]["adb"]["first_after_wipe_tails"], [8.52, 9.08, 9.17])
        self.assertNotIn("max_stamp_gap", d["paths"]["adb"])
        self.assertTrue(any("shutdown-tail bound 10.17 s from 3" in line for line in calibrate.summary(d)))

    def test_fewer_than_3_samples_means_no_bound(self):
        from riverqa import calibrate
        for tails in ((), (9.17,), (8.52, 9.17)):
            d = self.data([raw("adb", 10.0, 9.0, first_after_wipe=True), raw("adb", 10.01, 4.0)], tails=tails)
            calibrate.compute_paths(d)
            self.assertIsNone(d["paths"]["adb"]["max_shutdown_tail"])
            self.assertIn(f"{len(tails)} of 3", d["paths"]["adb"]["tail_why"])
            self.assertIn("flash --rollback", d["paths"]["adb"]["tail_why"])
            self.assertTrue(any("no shutdown-tail bound" in line for line in calibrate.summary(d)))

    def test_fewer_than_3_samples_is_unverified(self):
        # The adb path stays UNVERIFIED until the third sample exists.
        from riverqa import calibrate
        for tails, want in (((9.17,), UNVERIFIED), ((8.52, 9.17), UNVERIFIED), ((8.52, 9.08, 9.17), PASS)):
            p = dict(TIMING["paths"]["adb"])
            calibrate.set_tail_bound(p, samples(*tails))
            rec = record()
            reboots.judge_record(rec, shutdown_log(NORMAL), {"paths": {"adb": p}})
            self.assertEqual(rec.uptime_check[0], want, (tails, rec.anchor_note))

    def test_slower_tail_than_calibrated_is_unverified(self):
        # Calibrated bound 6.5 s; this boot's reset came 7.45 s after the
        # disconnect (a slower shutdown): UNVERIFIED, never PASS.
        rec = record(disconnect_mono=1002.0)
        reboots.judge_record(rec, shutdown_log(NORMAL), TIMING)
        self.assertEqual(rec.uptime_check[0], UNVERIFIED)
        self.assertIn("shutdown-tail bound", rec.anchor_note)

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

    def test_samples_accumulate_one_per_wipe(self):
        import benchhome
        with benchhome.bench_home():
            first = raw("adb", 10.0, 9.08) | {"path": "adb", "boot_id_before": "B1", "marker": MARKER}
            self.assertTrue(reboots.add_tail_sample(first, {"seq": 3, "how": "rollback"}, "boot-timing", "b"))
            # A second sample for the same wipe is not added.
            self.assertFalse(reboots.add_tail_sample(dict(first, reset_tail_s=12.0), {"seq": 3}, "bench:x"))
            # No anchor: nothing to add.
            self.assertFalse(reboots.add_tail_sample(dict(first, reset_tail_s=None), {"seq": 4}, "bench:x"))
            self.assertTrue(reboots.add_tail_sample(dict(first, reset_tail_s=8.52), {"seq": 4}, "bench:x"))
            got = reboots.load_tail_samples()
            self.assertEqual([(s["wipe_seq"], s["tail_s"], s["source"]) for s in got],
                             [(3, 9.08, "boot-timing"), (4, 8.52, "bench:x")])

    def test_bench_first_boot_adds_a_sample_only_when_proven(self):
        import benchhome
        from riverqa import bench
        wipe = {"seq": 7, "how": "flash", "first_boot_id": "first"}
        rec = record(boot_id_before="first", reset_tail_s=8.9)
        with benchhome.bench_home(), mock.patch("riverqa.journal.last_wipe", return_value=wipe):
            self.assertFalse(bench.record_first_tail(rec, (UNVERIFIED, "x"), 2, "b1", "build"))   # dropbox unproven
            self.assertFalse(bench.record_first_tail(record(boot_id_before="other", reset_tail_s=8.9),
                                                     (PASS, "x"), 2, "b1", "build"))             # another boot between
            self.assertTrue(bench.record_first_tail(rec, (PASS, "x"), 2, "b1", "build"))
            self.assertEqual([s["tail_s"] for s in reboots.load_tail_samples()], [8.9])

    def test_first_after_wipe_is_tied_to_the_flash_record(self):
        from riverqa import calibrate
        wipe = {"seq": 9, "how": "flash", "first_boot_id": "boot-after-wipe"}
        self.assertTrue(calibrate.first_after_wipe("adb", 0, "boot-after-wipe", 1, wipe)[0])
        for args in (("fastboot", 0, "boot-after-wipe", 1, wipe),     # not adb
                     ("adb", 1, "boot-after-wipe", 1, wipe),          # not step 1's first reboot
                     ("adb", 0, "another-boot", 1, wipe),             # a boot came in between
                     ("adb", 0, "boot-after-wipe", 2, wipe),          # dropbox disagrees
                     ("adb", 0, "boot-after-wipe", 0, wipe),          # dropbox not written yet
                     ("adb", 0, "boot-after-wipe", 1, dict(wipe, first_boot_id=None)),
                     ("adb", 0, "boot-after-wipe", 1, None)):
            self.assertFalse(calibrate.first_after_wipe(*args)[0], args)

    def test_last_wipe_reads_the_first_boot(self):
        import tempfile
        from pathlib import Path
        from riverqa import journal
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            self.assertIsNone(journal.last_wipe(home))
            s1 = journal.record_wipe(home, "a1", "flash")
            journal.record_wipe_boot(home, s1, "B1")
            s2 = journal.record_wipe(home, "a2", "rollback")
            self.assertEqual(journal.last_wipe(home)["seq"], s2)
            self.assertIsNone(journal.last_wipe(home)["first_boot_id"])      # not booted yet
            journal.record_wipe_boot(home, s2, "B2")
            self.assertEqual(journal.last_wipe(home)["first_boot_id"], "B2")
            self.assertEqual(journal.current_wipe_seq(home), s2)

    def test_boot_count_now_waits_for_a_boot_entry(self):
        from riverqa import calibrate
        dumps = iter([DROPBOX0, DROPBOX0, DROPBOX1, DROPBOX1])

        class Dev:
            def sh(self, cmd, timeout=30):
                return next(dumps)
        with mock.patch.object(calibrate.time, "sleep", lambda s: None):
            self.assertEqual(calibrate.boot_count_now(Dev()), 1)

    def test_qualified_needs_all_three_proofs(self):
        from riverqa import calibrate
        proofs = {"proof_normal_pass": True, "proof_control_rejected": True, "proof_unmarked_rejected": True}
        d = {"paths": {"adb": dict(proofs), "fastboot": dict(proofs)}}
        self.assertTrue(calibrate.qualified(d))
        d["paths"]["fastboot"]["proof_unmarked_rejected"] = False
        self.assertFalse(calibrate.qualified(d))

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
