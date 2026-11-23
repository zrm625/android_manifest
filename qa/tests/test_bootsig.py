import itertools
import unittest
from unittest import mock

import _path  # noqa: F401
from riverqa import bootsig as b
from riverqa import fbmode, reboots
from benchhome import bench_home
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


class Banner(unittest.TestCase):
    """Review rounds 4 and 5 (Opus 2, R5-10): the new-session check matches
    the banner within 2 flips, at the start of a printk line."""

    def session_after(self, banner: bytes, prefix: bytes | None = None) -> bytes:
        text = banner + b"4.9.337"
        first = line(0.0, text) if prefix is None else prefix + text + b"\n"
        return shutdown_log() + first + line(1.0, b"init")

    def test_damaged_banner_after_the_marker_fails(self):
        self.assertEqual(b.chain_verdict(self.session_after(b"Linux version "), MARKER)[0], FAIL)
        # The damage river's ramoops really does to the banner (every saved
        # pstore log since baseline-1 starts with this): 2 flips, with the
        # timestamp prefix damaged too.
        self.assertEqual(b.chain_verdict(self.session_after(b"Linu\xf8 ver\xf3ion "), MARKER)[0], FAIL)
        self.assertEqual(b.chain_verdict(self.session_after(b"Linu\xf8 ver\xf3ion ", b"[    0.\xb000000] "),
                                         MARKER)[0], FAIL)
        self.assertEqual(b.chain_verdict(self.session_after(b"Linux version ", b"<5>[    0.p00000] "),
                                         MARKER)[0], FAIL)
        for flips in ((0,), (0, 7), (2, 13)):
            damaged = flip_bytes(b.BANNER, flips)
            self.assertEqual(b.chain_verdict(self.session_after(damaged), MARKER)[0], FAIL, flips)
        # Three wrong characters is not a banner any more.
        self.assertEqual(b.chain_verdict(self.session_after(flip_bytes(b.BANNER, (0, 3, 7))), MARKER)[0], PASS)

    def test_minor_version_is_not_a_banner(self):
        """Opus minor 3: "minor version " is 3 flips from "Linux version ",
        and message text, not a line start."""
        self.assertEqual(b.hamming(b"minor version ", b.BANNER), 3)
        for text in (b"usb 1-1: config 1 interface 0 minor version 2", b"minor version 4.9.337",
                     b"hid-generic: Linux version 4.9 driver"):
            log = shutdown_log() + line(200.0, text)
            self.assertEqual(b.banner_offsets(log[len(line(0.0, b"Linux version 4.9.337")):]), [], text)
            self.assertEqual(b.chain_verdict(log, MARKER)[0], PASS, text)

    def test_every_two_flip_banner_at_a_line_start_is_found(self):
        pre = b"x" * 40 + b"\n[    1.000000] "
        for flips in itertools.combinations(range(len(b.BANNER)), 2):
            text = pre + flip_bytes(b.BANNER, flips) + b"4.9\n"
            self.assertIn(len(pre), b.banner_offsets(text), flips)

    def test_damaged_banner_before_the_marker_passes(self):
        log = line(0.0, b"Linu\xf8 ver\xf3ion 4.9.337") + shutdown_log()[len(line(0.0, b"Linux version 4.9.337")):]
        self.assertEqual(b.banner_offsets(log), [len(b"[    0.000000] ")])
        self.assertEqual(b.chain_verdict(log, MARKER)[0], PASS)

    def test_river_real_pstore_head(self):
        """The first two lines of a real saved river pstore log
        (pstore-nopass, 2026-11-23): the banner is found at its line start."""
        real = (b"[    0.000000] Booting Linux on physical CPU 0x0\n[    0.\xb000000] Linu\xf8 ver\xf3ion "
                b"4.9.337-perf (zik@a843a\xb888038d) (Android (")
        self.assertEqual(b.banner_offsets(real), [real.index(b"Linu\xf8")])

    def test_fuzzy_offsets_matches_a_brute_force_scan(self):
        import random
        rnd = random.Random(4)
        pat = b"Linux version "
        for _ in range(200):
            text = bytearray(rnd.choice(b"Linux vrsio") for _ in range(120))
            for _ in range(3):
                at = rnd.randrange(0, 100)
                text[at:at + len(pat)] = flip_bytes(pat, rnd.sample(range(len(pat)), rnd.randrange(0, 5)))
            text = bytes(text)
            brute = [i for i in range(len(text) - len(pat) + 1) if b.hamming(text[i:i + len(pat)], pat) <= 3]
            self.assertEqual(b.fuzzy_offsets(text, pat, 3), brute)


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


GATED = {"A": 12.0, "proof_normal_pass": True, "proof_control_rejected": True, "proof_unmarked_rejected": True}
TIMING = {"paths": {"sysrq": dict(GATED), "fastboot": dict(GATED)}, "rules_version": 3}


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

    def test_chain_counts_on_every_path(self):
        """R5-12: chain_survives is a record only. A chain UNVERIFIED counts
        even where step 1 recorded survives False."""
        sig = {"chain": [UNVERIFIED, "x"], "uptime": [PASS, "ok"], "boot_count": [PASS, "ok"]}
        for survives in (False, True, None):
            timing = {"chain": {"fastboot": {"survives": survives}}}
            self.assertEqual(reboots.combined_verdict(sig, "fastboot", timing)[0], UNVERIFIED, survives)
        sig["chain"] = [FAIL, "x"]
        self.assertEqual(reboots.combined_verdict(sig, "fastboot", {"chain": {"fastboot": {"survives": False}}})[0],
                         FAIL)

    def test_mixed_chain_path_qualifies_but_keeps_its_chain(self):
        """The case that made the old branch reachable: 3 clean normals
        (chain PASS) and a fourth with chain UNVERIFIED give the path an A
        and survives False. Its chain must still count."""
        from riverqa import calibrate
        d = {"raw": {"fastboot": [raw(10.07), raw(10.06), raw(10.08)],
                     "sysrq": [raw(10.06), raw(10.07), raw(10.08), raw(10.07, chain=UNVERIFIED)]},
             "inject_measure": {"fastboot": raw(14.47), "sysrq": raw(14.46)}, "paths": {}, "chain": {}}
        calibrate.compute_paths(d)
        self.assertIsNotNone(d["paths"]["sysrq"]["A"])
        self.assertFalse(d["chain"]["sysrq"]["survives"])
        sig = {"chain": [UNVERIFIED, "x"], "uptime": [PASS, "ok"], "boot_count": [PASS, "ok"]}
        self.assertEqual(reboots.combined_verdict(sig, "sysrq", d)[0], UNVERIFIED)

    def test_allowance_needs_proofs(self):
        t = {"paths": {"sysrq": {"A": 12.3, "proof_normal_pass": True, "proof_control_rejected": False}},
             "rules_version": 3}
        self.assertIsNone(reboots.allowance_for("sysrq", t))
        t["paths"]["sysrq"]["proof_control_rejected"] = True
        # Review round 4 (Gemini 1): the unmarked proof is needed too.
        self.assertIsNone(reboots.allowance_for("sysrq", t))
        t["paths"]["sysrq"]["proof_unmarked_rejected"] = True
        self.assertEqual(reboots.allowance_for("sysrq", t), 12.3)

    def test_allowance_needs_current_rules_and_stamps(self):
        """R7-6 (Opus 1): a proved A from a record under other calibration
        rules, or with an unstamped A-setting sample, is no A, so a boot it
        would pass is UNVERIFIED. At 62f73de both gave A = 12.3."""
        from riverqa import calibrate
        proved = {"A": 12.3, "proof_normal_pass": True, "proof_control_rejected": True,
                  "proof_unmarked_rejected": True}
        stamped = raw(10.0)
        good = {"paths": {"sysrq": dict(proved)}, "rules_version": calibrate.CALIBRATION_RULES,
                "raw": {"sysrq": [dict(stamped)] * 3}, "inject_measure": {"sysrq": dict(stamped)}}
        self.assertEqual(reboots.allowance_for("sysrq", good), 12.3)
        old_rules = dict(good, rules_version=2)
        no_rules = {k: v for k, v in good.items() if k != "rules_version"}
        unstamped_normal = dict(good, raw={"sysrq": [dict(stamped), dict(stamped), raw(10.0, rules_version=None)]})
        unstamped_inject = dict(good, inject_measure={"sysrq": raw(14.4, rules_version=None)})
        for name, t in (("old rules", old_rules), ("no rules", no_rules), ("unstamped normal", unstamped_normal),
                        ("unstamped inject", unstamped_inject)):
            with self.subTest(name):
                self.assertIsNone(reboots.allowance_for("sysrq", t))
                # 13 s elapsed, 12 s uptime: within A = 12.3, but UNVERIFIED without it.
                self.assertEqual(b.uptime_verdict(12.0, 13.0, reboots.allowance_for("sysrq", t), 0,
                                                        disconnect_gap_s=0.1, status_read_s=0.1)[0], UNVERIFIED)

    def test_chain_survives_only_on_gated_paths(self):
        """Review round 4 (Gemini 4): only the gated paths have a record;
        since R5-12 it never turns the chain off anywhere."""
        timing = {"chain": {"adb": {"survives": False}, "sysrq": {"survives": False}}}
        self.assertIsNone(reboots.chain_survives("adb", timing))
        self.assertFalse(reboots.chain_survives("sysrq", timing))
        sig = {"chain": [UNVERIFIED, "x"], "uptime": [INFO, "x"], "boot_count": [PASS, "ok"]}
        self.assertEqual(reboots.combined_verdict(sig, "adb", timing)[0], UNVERIFIED)
        self.assertEqual(reboots.combined_verdict(dict(sig, uptime=[PASS, "x"]), "sysrq", timing)[0], UNVERIFIED)

def raw(bootloader_pass, chain=PASS, **kw):
    from riverqa import calibrate
    r = {"bootloader_pass": bootloader_pass, "chain": [chain, ""], "marker_copies_exact": 4,
         "marker_found_fuzzy": True, "boot_count": [PASS, ""], "unplanned_fastboot": 0,
         "disconnect_gap": 0.06, "status_read_s": 0.1, "rules_version": calibrate.CALIBRATION_RULES}
    r.update(kw)
    return r


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

    def test_sol_bad_sample_never_sets_the_allowance(self):
        """{10.00, 10.00, 14.20 bad}: with the bad sample counted, A was
        (14.20 + 10.00 + 5.4) / 2 = 14.80, and a real 14.2 s extra pass
        passed. Every sign of an extra boot or a loose bracket excludes it,
        and two clean samples are not enough (review round 4)."""
        from riverqa import calibrate
        bad_flavours = ({"unplanned_fastboot": 1}, {"chain": [FAIL, ""]}, {"chain": [UNVERIFIED, ""]},
                        {"boot_count": [FAIL, ""]}, {"boot_count": [UNVERIFIED, ""]}, {"disconnect_gap": 0.9},
                        {"disconnect_gap": None}, {"status_read_s": 1.4})
        unfiltered = self.data(sysrq=[raw(10.0), raw(10.0), raw(14.2)], inj_sysrq=raw(15.4))
        calibrate.compute_paths(unfiltered)
        self.assertEqual(unfiltered["paths"]["sysrq"]["A"], 14.8)     # the counterexample, unfiltered
        for bad in bad_flavours:
            d = self.data(sysrq=[raw(10.0), raw(10.0), raw(14.2, **bad)], inj_sysrq=raw(15.4))
            calibrate.compute_paths(d)
            p = d["paths"]["sysrq"]
            self.assertIsNone(p["A"], bad)
            self.assertIn("only 2 clean normal sysrq reboots", p["why"])
            self.assertEqual([e["sample"] for e in p["excluded"]], [2], bad)
            self.assertFalse(calibrate.qualified(d))
            # With a third clean sample the bad one is still left out.
            d = self.data(sysrq=[raw(10.0), raw(10.0), raw(14.2, **bad), raw(10.01)], inj_sysrq=raw(15.4))
            calibrate.compute_paths(d)
            p = d["paths"]["sysrq"]
            self.assertEqual(p["normal_passes"], [10.0, 10.0, 10.01], bad)
            self.assertLess(p["A"], 14.2, bad)

    def test_unclean_injected_measurement_sets_no_allowance(self):
        from riverqa import calibrate
        for bad in ({"disconnect_gap": 0.9}, {"unplanned_fastboot": 1}, {"chain": [FAIL, ""]}):
            d = self.data(inj_sysrq=raw(14.46, **bad))
            calibrate.compute_paths(d)
            self.assertIsNone(d["paths"]["sysrq"]["A"], bad)
            self.assertIn("injected-pass measurement is not clean", d["paths"]["sysrq"]["why"])

    def test_too_few_clean_fastboot_reboots_means_no_flash_pass(self):
        from riverqa import calibrate
        d = self.data()
        d["raw"]["fastboot"] = [raw(10.07), raw(10.06), raw(10.08, disconnect_gap=0.7), raw(10.07, chain=FAIL)]
        calibrate.compute_paths(d)
        self.assertIsNone(d["paths"]["fastboot"]["A"])
        self.assertIsNone(d["paths"]["sysrq"]["A"])
        self.assertIn("flash-path pass", d["paths"]["sysrq"]["why"])

    def test_chain_record_counts_boots_that_did_not_pass(self):
        from riverqa import calibrate
        d = self.data(sysrq=[raw(10.06), raw(10.07, chain=UNVERIFIED)])
        calibrate.compute_paths(d)
        self.assertFalse(d["chain"]["sysrq"]["survives"])
        self.assertEqual(d["chain"]["sysrq"]["verdicts"], [PASS, UNVERIFIED])
        self.assertNotIn("proven", d["chain"]["sysrq"])

    def test_renewal_needs_a_clean_current_record(self):
        """R5-3: controls --boot-timing renews proofs only for a record the
        current rules would set A from."""
        from riverqa import calibrate
        good = self.data()
        calibrate.compute_paths(good)
        good["rules_version"] = calibrate.CALIBRATION_RULES
        self.assertEqual(calibrate.record_problems(good), [])
        # A legacy-shaped record: an A, but no rules stamp and no bracketed samples.
        legacy = self.data()
        for r in legacy["raw"]["fastboot"] + legacy["raw"]["sysrq"] + list(legacy["inject_measure"].values()):
            r.pop("disconnect_gap"), r.pop("status_read_s")
        legacy["paths"] = {p: dict(good["paths"][p]) for p in good["paths"]}
        probs = calibrate.record_problems(legacy)
        self.assertTrue(any("calibration rules" in x for x in probs), probs)
        self.assertTrue(any("sysrq: its samples give no A" in x for x in probs), probs)
        legacy["rules_version"] = calibrate.CALIBRATION_RULES
        self.assertTrue(calibrate.record_problems(legacy))
        # A stored A that the samples don't give.
        bent = self.data()
        calibrate.compute_paths(bent)
        bent["rules_version"] = calibrate.CALIBRATION_RULES
        bent["paths"]["sysrq"]["A"] = 14.8
        self.assertIn("stored A 14.8", " ".join(calibrate.record_problems(bent)))

    def test_prove_main_refuses_a_legacy_record(self):
        import io
        from contextlib import redirect_stderr
        from unittest import mock
        from riverqa import calibrate
        legacy = self.data(sysrq=[raw(10.06, disconnect_gap=None), raw(10.07, disconnect_gap=None),
                                  raw(10.08, disconnect_gap=None)])
        calibrate.compute_paths(legacy)
        legacy["paths"]["sysrq"]["A"] = 12.3
        with bench_home(), mock.patch.object(reboots, "load_timing", return_value=legacy), \
                mock.patch.object(calibrate, "Device", side_effect=AssertionError("touched the phone")), \
                mock.patch.object(calibrate, "prove", side_effect=AssertionError("renewed")), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(calibrate.prove_main(), 2)
        self.assertIn("does not qualify for renewed proofs", err.getvalue())

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
            def sh_rc(self, cmd, timeout=30):
                return 0, next(dumps), ""
        with mock.patch.object(calibrate.time, "sleep", lambda s: None):
            self.assertEqual(calibrate.boot_count_now(Dev()), 1)

    def test_boot_counts_use_the_checked_dropbox_read(self):
        """Review round 4: a failed dumpsys dropbox (exit 0 with no header,
        or an error) is never a dump with no SYSTEM_BOOT entries."""
        from riverqa import calibrate

        class Dev:
            def __init__(self, replies):
                self.replies = iter(replies)

            def sh_rc(self, cmd, timeout=30):
                return next(self.replies)
        bad = [(0, "Can't find service: dropbox\n", ""), (1, "", "error: closed")]
        # Unreadable to the deadline: UNVERIFIED, never a count of 0.
        clock = itertools.count(0, 50)
        with mock.patch.object(reboots.time, "sleep", lambda s: None), \
                mock.patch.object(reboots.time, "monotonic", lambda: next(clock)):
            st, why = reboots.read_boot_count(Dev(bad * 3), expected=2, settle_s=60)
        self.assertEqual(st, UNVERIFIED)
        self.assertIn("unreadable", why)
        # A failed read between two good ones doesn't count as settled.
        two = "Drop box contents: 2 entries\n\n" + "2026-11-22 18:44:04 SYSTEM_BOOT (text, 4 bytes)\n" * 2
        dev = Dev([(0, two, ""), bad[0], (0, two, ""), (0, two, ""), (0, two, "")])
        with mock.patch.object(reboots.time, "sleep", lambda s: None):
            self.assertEqual(reboots.read_boot_count(dev, expected=2)[0], PASS)
        self.assertEqual(len(list(dev.replies)), 0)    # needed 3 good reads in a row after the bad one
        clock = itertools.count(0, 50)
        with mock.patch.object(calibrate.time, "sleep", lambda s: None), \
                mock.patch.object(calibrate.time, "monotonic", lambda: next(clock)):
            self.assertIsNone(calibrate.boot_count_now(Dev(bad * 3)))
        # R5-14: a good read, then failures to the deadline: unverified, not the stale count.
        clock = itertools.count(0, 50)     # end at 60; the good read at 50, the failed one at 100
        with mock.patch.object(calibrate.time, "sleep", lambda s: None), \
                mock.patch.object(calibrate.time, "monotonic", lambda: next(clock)):
            self.assertIsNone(calibrate.boot_count_now(Dev([(0, DROPBOX1, "")] + bad * 3)))

    def test_controls_boot_timing_command(self):
        from riverqa import calibrate, qa
        with mock.patch.object(calibrate, "prove_main", return_value=0) as pm:
            self.assertEqual(qa.main(["controls", "--boot-timing"]), 0)
        pm.assert_called_once()


def unstamped(bootloader_pass, **kw):
    """A sample as code before R6-2 stored it: no per-sample rules stamp."""
    r = raw(bootloader_pass, **kw)
    r.pop("rules_version")
    return r


class CalibrationProvenance(unittest.TestCase):
    """R6-2: --reuse-normals and controls --boot-timing trust only samples
    stamped with the current calibration rules (full-precision brackets).

    Sol's case: before R5-8 a disconnect gap of 0.5004 s was stored rounded
    as 0.500 s, which passes the 0.5 s bound. Reused, the bad 14.2 s sample
    counts as clean and A comes out 14.8, so a real 14.2 s extra pass
    passes."""

    BUILD = "1795436282"

    def old_record(self, stamped=False):
        from riverqa import calibrate
        mk = raw if stamped else unstamped
        return {"measured_at": "2026-11-20T00:00:00+00:00", "rules_version": calibrate.CALIBRATION_RULES,
                "build_date_utc": self.BUILD,
                "raw": {"fastboot": [mk(10.072), mk(10.065), mk(10.064), mk(10.08)],
                        "sysrq": [mk(10.0), mk(10.0), mk(14.2, disconnect_gap=0.500)]},
                "inject_measure": {}, "paths": {}, "chain": {}, "controls": {}}

    def run_calibration(self, old):
        """calibrate.run(4, reuse=True) with the phone faked at the level of
        one(): fresh normals are ~10.07 s, injected passes 14.465 / 15.4 s.
        Returns (saved boot-timing.json, normal reboots measured)."""
        import io
        from contextlib import redirect_stderr
        from riverqa import calibrate, controls
        fresh = {"fastboot": 10.07, "sysrq": 10.06}
        normals = []

        def one(dev, w, path, timing, *, inject=False, count_before=None, **kw):
            if inject:
                return unstamped(14.465 if path == "fastboot" else 15.4)
            normals.append(path)
            return unstamped(fresh[path] + 0.001 * len(normals))

        class Dev:
            serial = "ZY225DPT7F"

            def guard(self):
                pass

            def root(self):
                pass

            def getprop(self, name):
                return CalibrationProvenance.BUILD if name == "ro.build.date.utc" else "22-test"
        reboots.save_timing(old)
        with mock.patch.object(calibrate, "Device", lambda *a: Dev()), \
                mock.patch.object(calibrate, "recover_fb_mode", return_value=None), \
                mock.patch.object(calibrate, "UsbWatcher"), \
                mock.patch.object(calibrate, "boot_count_now", return_value=1), \
                mock.patch.object(calibrate, "one", side_effect=one), \
                mock.patch.object(calibrate, "prove"), \
                mock.patch.object(controls, "record_step1"), redirect_stderr(io.StringIO()):
            calibrate.run(4, True)
        return reboots.load_timing(), normals

    def test_unstamped_normals_are_not_reused(self):
        with bench_home():
            saved, normals = self.run_calibration(self.old_record())
        a = saved["paths"]["sysrq"]["A"]
        self.assertIsNotNone(a, saved["paths"]["sysrq"].get("why"))
        self.assertLess(a, 14.2, "the 0.500 s-gap sample from an unstamped record set A")
        self.assertEqual(len(normals), 8, "fresh normals were not measured")
        self.assertFalse(saved.get("reused_normals_from"))

    def test_empty_build_date_is_never_reused(self):
        """R7-10 (Gemini): an empty build_date_utc on either side, even on
        both, fails closed (at 62f73de two empty dates matched)."""
        from riverqa import calibrate
        old = self.old_record(stamped=True)
        self.assertEqual(calibrate.reuse_problems(old, self.BUILD), [])
        for stored, now in (("", ""), (None, ""), ("", self.BUILD), (self.BUILD, "")):
            with self.subTest(stored=stored, now=now):
                why = calibrate.reuse_problems(dict(old, build_date_utc=stored), now)
                self.assertTrue(any("build date unknown" in w for w in why), why)

    def test_proofs_are_judged_with_the_recorded_a(self):
        """R7-6: prove() judges its fresh reboots under the current rules,
        so allowance_for gives them the recorded A (no stored samples)."""
        from riverqa import calibrate
        seen = []

        def one(dev, w, path, timing, **kw):
            seen.append(reboots.allowance_for(path, timing))
            return dict(raw(10.0), uptime_check=[PASS, ""])
        data = {"paths": {p: {"A": 12.0} for p in reboots.GATED_PATHS}, "controls": {}}
        with bench_home(), mock.patch.object(calibrate, "one", side_effect=one), \
                mock.patch.object(calibrate, "prove_adb_chain"):
            calibrate.prove(None, None, data)
        self.assertEqual(seen, [12.0] * 6)

    def test_stamped_normals_are_reused(self):
        with bench_home():
            old = self.old_record(stamped=True)
            old["raw"]["sysrq"][2] = raw(10.01)
            saved, normals = self.run_calibration(old)
        self.assertEqual(normals, [])
        self.assertEqual(saved["reused_normals_from"], old["measured_at"])
        self.assertIsNotNone(saved["paths"]["sysrq"]["A"])

    def test_renewal_refuses_unstamped_samples(self):
        """record_problems: a record that claims the current rules, with an
        A its samples reproduce under the old rules, but samples that carry
        no stamp (the 0.500 s gap among them), is not renewed."""
        from riverqa import calibrate
        rec = self.old_record()
        rec["raw"]["sysrq"].append(unstamped(10.01))
        rec["inject_measure"] = {"fastboot": unstamped(14.465), "sysrq": unstamped(15.4)}
        legacy = {"raw": rec["raw"], "inject_measure": rec["inject_measure"], "paths": {}, "chain": {}}
        with mock.patch.object(calibrate, "sample_problems", lambda r, normal: []):
            calibrate.compute_paths(legacy)     # what an unstamp-blind rule makes of it
        rec["paths"] = legacy["paths"]
        self.assertIsNotNone(rec["paths"]["sysrq"]["A"])
        probs = calibrate.record_problems(rec)
        self.assertTrue(probs, "a record with unstamped samples qualified for renewal")
        self.assertTrue(any("stamp" in x for x in probs), probs)


class CheckedPstoreRead(unittest.TestCase):
    """The boot judge reads pstore through the checked binary read (R2-4)."""

    def judge(self, files, timing=TIMING, path="sysrq"):
        from fakes import FakeShell
        dev = FakeShell({}, {r"console-ramoops": files})
        rec = record(path)
        reboots.judge_signals(dev, rec, timing)
        return rec

    def test_missing_console_ramoops_is_always_a_read_error(self):
        """R5-12 replaces review round 4's Gemini 2 exception: no
        console-ramoops is a read error on every path, whatever step 1
        recorded."""
        dead = dict(TIMING, chain={"sysrq": {"survives": False}, "fastboot": {"survives": True}})
        for path in ("sysrq", "fastboot", "adb"):
            self.assertTrue(self.judge((0, []), dead, path=path).pstore_read_error, path)
        self.assertTrue(self.judge((5, []), dead).pstore_read_error)

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

        boot_id_read = (0, "aaaaaaaa-bbbb\n")

        def read(self, cmd, timeout=30):
            assert cmd == "cat /proc/sys/kernel/random/boot_id", f"no live readback under A2.1: {cmd}"
            return Read(*self.boot_id_read)

        def kmsg(self, text):
            self.markers.append(text)
            return self.ok.pop(0)

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

    def test_failed_or_empty_boot_id_read_writes_no_marker(self):
        """Review round 4 (Gemini 3): a checked boot_id read; nothing is
        written if it failed or came back empty."""
        for reply in ((1, "aaaaaaaa-bbbb\n"), (0, "\n"), (0, "")):
            dev = self.Dev()
            dev.boot_id_read = reply
            self.assertEqual(reboots.leave_marker(dev), (None, None), reply)
            self.assertEqual(dev.markers, [])

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

        def fb_mode_set(self):
            self.calls.append(("fb_mode", True))
            self.fb = True

        def fb_mode_clear(self):
            self.calls.append(("fb_mode", False))
            self.fb = False

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

        # fbmode.recover's slot check (R7-1): one slot, always booted.
        def getvar(self, name):
            return "_a"

        def guard(self):
            pass

        def getprop(self, name):
            return "_a"

        # verify_boot and settle (R8-1): the boot status, both build IDs.
        def root(self, seconds=60):
            pass

        def sh(self, cmd, timeout=30):
            assert "build_id" in cmd, cmd
            return ("ro.vendor.river.build_id" if "vendor" in cmd else "ro.river.build_id") + "=B1\n"

        def boot_status(self):
            import time
            from riverqa.device import BootStatus
            t = time.monotonic()
            return BootStatus("bid", True, 0.0, t, t, time.time())

        def boot_id(self):
            return "bid"

    def reboot(self, path, stray=False, dev=None, **kw):
        w = self.W()
        dev = dev or self.Dev(w, stray)
        dev.w = w
        with bench_home(), mock.patch.dict("os.environ", {"RIVERQA_CONTROL": "x"} if kw else {}):
            rec = reboots.planned_reboot(dev, path, boot_timeout=10, watcher=w, **kw)
            self.assertIsNone(fbmode.pending(), "the fb_mode obligation must be resolved")
            return rec, dev

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
        # R8-1: the control starts from Android, where the identity the
        # obligation records is read, then goes to the bootloader to arm.
        self.assertEqual(dev.calls, [("reboot_adb", "bootloader"), ("fb_mode", True), ("continue",), ("sysrq",),
                                     ("fb_mode", False), ("reboot_fastboot", None)])
        self.assertEqual(rec.planned_fastboot, 1)
        # The clock starts at the sysrq disconnect (event 5, after the two
        # of the arm's adb reboot bootloader), not the final reboot's.
        self.assertAlmostEqual(rec.elapsed, 100.0 - 4.95)

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
        dev.state = lambda: "device"          # back in Android, with fb_mode still set
        with self.assertRaises(TimeoutError), \
                mock.patch.object(reboots, "leave_marker", return_value=(None, None)):
            self.reboot("sysrq", dev=dev, inject_extra_pass=True)
        self.assertFalse(dev.fb)
        self.assertEqual(dev.calls[-3:], [("reboot_adb", "bootloader"), ("fb_mode", False),
                                          ("reboot_fastboot", None)])

    def test_enumeration_right_after_the_disconnect_counts(self):
        rec, _ = self.reboot("fastboot", stray=True)
        self.assertEqual(rec.unplanned_fastboot, 1)


if __name__ == "__main__":
    unittest.main()
