import json
import subprocess
import unittest

import _path  # noqa: F401
from riverqa import recovery, slots


def base(kg="b"):
    st = slots.first_run(kg, "f" * 64, "t0")
    return st


class Transitions(unittest.TestCase):
    def test_first_run_is_provisional(self):
        st = base()
        self.assertEqual(st["known_good_slot"], "b")
        self.assertTrue(st["known_good"]["provisional"])
        with self.assertRaises(slots.SlotError):
            slots.first_run("b", "", "t")

    def test_preflight_rules(self):
        self.assertEqual(slots.preflight(None, "base", "baseline", "b"), [])
        self.assertTrue(slots.preflight(None, "x", "batch", "b"))
        st = base()
        self.assertTrue(any("running slot" in e for e in slots.preflight(st, "base", "baseline", "a")))
        # Batches need a closed baseline.
        self.assertTrue(any("baseline" in e for e in slots.preflight(st, "x", "batch", "b")))
        self.assertTrue(slots.preflight(st, "x", "nonsense", "b"))

    def test_target_is_never_known_good(self):
        st = base("a")
        p = slots.reserve(st, "base", "baseline", "h" * 64, "t1")
        self.assertEqual(p["target"], "b")
        self.assertEqual(p["fallback"], "a")
        with self.assertRaises(slots.SlotError):
            slots.reserve(st, "other", "batch", "h" * 64, "t1")

    def test_unresolved_pair_blocks_other_batch_and_own_retry(self):
        st = base()
        slots.reserve(st, "base", "baseline", "h" * 64, "t1")
        self.assertTrue(any("unresolved pair" in e for e in slots.preflight(st, "other", "batch", "b")))
        self.assertTrue(any("own pair" in e for e in slots.preflight(st, "base", "baseline", "b")))

    def test_rollback_releases_and_retry_reuses_target(self):
        st = base()
        p = slots.reserve(st, "base", "baseline", "h" * 64, "t1")
        slots.mark_written(st, "att1", "t2")
        with self.assertRaises(slots.SlotError):
            slots.rolled_back(st, "a", "wrong slot", "t3")
        slots.rolled_back(st, "b", "verified", "t3")
        self.assertIsNone(st["pair"])
        self.assertEqual(slots.preflight(st, "base", "baseline", "b"), [])
        p2 = slots.reserve(st, "base", "baseline", "h" * 64, "t4")
        self.assertEqual(p2["target"], p["target"])

    def test_baseline_close_then_batch(self):
        st = base("b")
        slots.reserve(st, "base", "baseline", "h" * 64, "t1")
        slots.close_baseline(st, "base", "id-1", "c" * 64, "t2")
        self.assertEqual(st["known_good_slot"], "a")
        self.assertFalse(st["known_good"]["provisional"])
        self.assertIsNotNone(st["baseline_closed"])
        self.assertEqual(slots.preflight(st, "b1", "batch", "a"), [])
        p = slots.reserve(st, "b1", "batch", "c" * 64, "t3")
        self.assertEqual(p["target"], "b")

    def test_promote_rules(self):
        st = base("b")
        slots.reserve(st, "cal", "calibration", "h" * 64, "t1")
        with self.assertRaises(slots.SlotError):
            slots.promote(st, "cal", "id", "c" * 64, "t2")
        st = base("b")
        slots.reserve(st, "b1", "batch", "h" * 64, "t1")
        with self.assertRaises(slots.SlotError):
            slots.promote(st, "b1", "", "c" * 64, "t2")
        slots.promote(st, "b1", "id", "c" * 64, "t2")
        self.assertEqual(st["known_good_slot"], "a")


def stub_runner(merged, tags, fail=None):
    def run(cmd):
        if fail and cmd[0] == fail:
            return subprocess.CompletedProcess(cmd, 1, "", "network down")
        if cmd[0] == "gh":
            n = int(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"state": "MERGED" if merged[n] else "OPEN",
                                                                   "url": f"u{n}"}), "")
        repo = cmd[3]
        return subprocess.CompletedProcess(cmd, 0, "abc\trefs/tags/kg-1\n" if tags[repo] else "", "")
    return run


BATCH = {"prs": [{"repo": "zrm625/a", "number": 0}, {"repo": "zrm625/b", "number": 1}], "tag": "kg-1",
         "tag_repos": ["ra", "rb"]}


class Recovery(unittest.TestCase):
    def decide(self, merged, tags, kind="batch", fail=None):
        look = recovery.lookup(BATCH, stub_runner(merged, tags, fail))
        return slots.decide_recovery({"kind": kind, "batch": "b"}, look)[0]

    def test_all_merged_and_tagged_promotes(self):
        # The "tag pushed, host killed before slot-state" control.
        self.assertEqual(self.decide([True, True], {"ra": True, "rb": True}), slots.PROMOTE)

    def test_none_rolls_back(self):
        self.assertEqual(self.decide([False, False], {"ra": False, "rb": False}), slots.ROLLBACK)

    def test_partial_halts(self):
        self.assertEqual(self.decide([True, False], {"ra": False, "rb": False}), slots.HALT)
        self.assertEqual(self.decide([True, True], {"ra": True, "rb": False}), slots.HALT)

    def test_lookup_failure_halts(self):
        self.assertEqual(self.decide([False, False], {"ra": False, "rb": False}, fail="gh"), slots.HALT)
        self.assertEqual(self.decide([False, False], {"ra": False, "rb": False}, fail="git"), slots.HALT)

    def test_pairs_without_prs_roll_back(self):
        for kind in ("baseline", "calibration", "experiment"):
            self.assertEqual(slots.decide_recovery({"kind": kind}, None)[0], slots.ROLLBACK)

    def test_no_prs_recorded_halts(self):
        self.assertEqual(slots.decide_recovery({"kind": "batch"}, {"prs": [], "tags": []})[0], slots.HALT)


if __name__ == "__main__":
    unittest.main()
