import unittest

import _path  # noqa: F401
from riverqa import regression as rg
from riverqa.results import FAIL, PASS, UNVERIFIED


def row(ram_mb, boot=45.0, sys_kb=1_900_000, ven_kb=370_000):
    return {"used_ram_kb": str(int(ram_mb * 1024)), "host_boot_seconds": str(boot),
            "system_used_kb": str(sys_kb), "vendor_used_kb": str(ven_kb)}


def kg(ref=1200.0, seed=None, sys_kb=1_900_000, ven_kb=370_000):
    return {"latest": {"system_used_kb": sys_kb, "vendor_used_kb": ven_kb, "host_boot_seconds": 48.0},
            "ram_reference": {"median_mb": ref, "seed": seed}}


class Rules(unittest.TestCase):
    def test_no_reference(self):
        v = rg.evaluate([row(1200)], None)
        self.assertEqual(v.state, UNVERIFIED)

    def test_boot_limit(self):
        self.assertEqual(rg.evaluate([row(1200, boot=55.0)], kg()).state, PASS)
        self.assertEqual(rg.evaluate([row(1200, boot=55.1)], kg()).state, FAIL)

    def test_single_boot_within_10(self):
        self.assertEqual(rg.evaluate([row(1209)], kg()).state, PASS)

    def test_single_boot_over_10_needs_median(self):
        v = rg.evaluate([row(1211)], kg())
        self.assertEqual(v.state, UNVERIFIED)
        self.assertTrue(v.details["three_boots_required"])

    def test_sysrq_row_does_not_count_when_one_boot_is_enough(self):
        """Every batch has a mandatory sysrq bench boot (A2), but the charter
        judges RAM on one boot unless a trigger fires (review round 4)."""
        # Boot 1 within 10 MB: no trigger. A bad sysrq row (or a good third
        # row) changes nothing; the verdict is boot 1 alone.
        for rows in ([row(1209), row(1300)], [row(1209), row(1300), row(1300)],
                     [row(1209), row(1100), row(1100)]):
            v = rg.evaluate(rows, kg())
            self.assertEqual(v.state, PASS, rows)
            self.assertFalse(v.details["three_boots_required"])
            self.assertIsNone(v.details["median_mb"])
            self.assertIn("RAM boot 1", v.summary)
        # Boot 1 over reference + 10 fires the trigger: now the median counts.
        v = rg.evaluate([row(1211), row(1100), row(1100)], kg())
        self.assertTrue(v.details["three_boots_required"])
        self.assertEqual(v.details["median_mb"], 1100.0)

    def test_three_boot_rule_uses_the_median_of_mixed_paths(self):
        rows = [dict(row(1211), reboot_path="adb", run_id="r", boot="1"),
                dict(row(1204), reboot_path="sysrq", run_id="r", boot="2"),
                dict(row(1203), reboot_path="sysrq", run_id="r2", boot="1")]
        v = rg.evaluate(rows, kg())
        self.assertEqual(v.state, PASS)
        self.assertEqual(v.details["ram_rows"], ["r#1", "r#2", "r2#1"])

    def test_median_rule(self):
        self.assertEqual(rg.evaluate([row(1211), row(1204), row(1203)], kg()).state, PASS)
        self.assertEqual(rg.evaluate([row(1211), row(1206), row(1207)], kg()).state, FAIL)

    def test_at_or_under_1050_always_three(self):
        v = rg.evaluate([row(1040)], kg(ref=1045))
        self.assertEqual(v.state, UNVERIFIED)
        self.assertEqual(rg.evaluate([row(1040), row(1041), row(1042)], kg(ref=1045)).state, PASS)

    def test_seed_always_three(self):
        self.assertEqual(rg.evaluate([row(1190)], kg(seed="calibration")).state, UNVERIFIED)
        self.assertEqual(rg.evaluate([row(1190)] * 3, kg(seed="calibration")).state, PASS)

    def test_size_growth(self):
        big = dict(sys_kb=1_900_001)
        # Grew without a median: 3 boots required, so UNVERIFIED.
        self.assertEqual(rg.evaluate([row(1190, **big)], kg()).state, UNVERIFIED)
        # Grew, median not below reference: FAIL.
        self.assertEqual(rg.evaluate([row(1200, **big), row(1201), row(1202)], kg()).state, FAIL)
        # Grew, median below reference: allowed.
        self.assertEqual(rg.evaluate([row(1190, **big), row(1191), row(1192)], kg()).state, PASS)
        self.assertEqual(rg.evaluate([row(1190, ven_kb=370_001), row(1191), row(1192)], kg()).state, PASS)

    def test_needs_three_triggers(self):
        need, why = rg.needs_three_boots(1100, None, False)
        self.assertTrue(need)
        need, why = rg.needs_three_boots(1100, kg(ref=1100), False)
        self.assertFalse(need)
        self.assertTrue(rg.needs_three_boots(1100, kg(ref=1100), True)[0])


class KnownGood(unittest.TestCase):
    def test_baseline_seed(self):
        k = rg.baseline_record(None, "b1", {}, ["r1"], [row(1230), row(1220), row(1240)], [0, 1, 2])
        self.assertEqual(k["ram_reference"]["median_mb"], 1230.0)
        self.assertEqual(k["ram_reference"]["seed"], "calibration")
        with self.assertRaises(ValueError):
            rg.baseline_record(None, "b1", {}, ["r1"], [row(1230)], [0, 1, 2])

    def test_seed_retired_never_raises(self):
        seed = rg.baseline_record(None, "b1", {}, ["r1"], [row(1230)] * 3, [0, 1, 2])
        higher = rg.update_on_tag(seed, "kg-1", "b2", {}, ["r2"], [row(1240)] * 3, [0, 1, 2])
        self.assertEqual(higher["ram_reference"]["median_mb"], 1230.0)
        self.assertEqual(higher["ram_reference"]["batch"], "b1")  # keeps the seed's provenance
        self.assertIsNone(higher["ram_reference"]["seed"])
        lower = rg.update_on_tag(seed, "kg-1", "b2", {}, ["r2"], [row(1200)] * 3, [0, 1, 2])
        self.assertEqual(lower["ram_reference"]["median_mb"], 1200.0)
        self.assertEqual(lower["ram_reference"]["tag"], "kg-1")

    def test_running_minimum(self):
        # At or below 1,050 MB every batch is judged on three boots.
        k = {"latest": {}, "ram_reference": {"median_mb": 1000.0, "seed": None, "tag": "a"}}
        k2 = rg.update_on_tag(k, "b", "b", {}, ["r"], [row(999)] * 3, [0])
        self.assertEqual(k2["ram_reference"]["median_mb"], 999.0)
        k3 = rg.update_on_tag(k2, "c", "c", {}, ["r"], [row(1005)] * 3, [0])
        self.assertEqual(k3["ram_reference"]["tag"], "b")
        k4 = rg.update_on_tag(k3, "d", "d", {}, ["r"], [row(900)], [0])  # one boot never changes it
        self.assertEqual(k4["ram_reference"]["tag"], "b")
        self.assertEqual(k4["latest"]["tag"], "d")  # latest always replaced



class ReferenceFollowsTheVerdict(unittest.TestCase):
    def test_sol_one_boot_verdict_never_moves_the_reference(self):
        """R5-4: reference 1,230 MB (not the seed), rows 1,200 / 1,150 /
        1,150. Boot 1 is within 10 MB and above 1,050 MB, sizes did not grow:
        the verdict judges boot 1 alone, so the 1,150 median it ignored must
        not become the reference."""
        k = kg(1230.0)
        rows = [row(1200), row(1150), row(1150)]
        v = rg.evaluate(rows, k)
        self.assertFalse(v.details["three_boots_required"])
        self.assertIsNone(v.details["median_mb"])
        after = rg.update_on_tag(k, "kg-2", "b2", {}, ["r"], rows, [0])
        self.assertEqual(after["ram_reference"]["median_mb"], 1230.0)

    def test_three_boot_verdict_moves_it(self):
        k = kg(1230.0)
        rows = [row(1250), row(1150), row(1150)]                 # boot 1 > ref + 10: three boots
        self.assertTrue(rg.evaluate(rows, k).details["three_boots_required"])
        self.assertEqual(rg.update_on_tag(k, "kg-2", "b2", {}, ["r"], rows, [0])["ram_reference"]["median_mb"],
                         1150.0)


if __name__ == "__main__":
    unittest.main()
