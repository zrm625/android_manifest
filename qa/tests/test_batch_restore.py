"""The tree restore (R7.8) from the lease alone (Gemini GB2, Opus OB1),
through its real callers: the recovery entry point with no running (an
abort whose restore had failed) and R11.2's abort. TC is the contract fake:
like the real tc.check, a fork missing from ``expected`` fails."""
import unittest

import _path  # noqa: F401
from batchfakes import FakeTree, mk_ctx, write_json
from benchhome import bench_home
from riverbatch import ctx as C, merge, phases, recover, state as S

BASE = {p: f"{i:x}" * 40 for i, p in enumerate(sorted(merge.slugs()), start=1)}
LEASED = {"device/motorola/river": "a" * 40, "vendor/lineage": "c" * 40}


def tree_on_batch(bid):
    t = FakeTree(BASE)
    for p, sha in LEASED.items():
        t.branches[p][f"batch/{bid}"] = sha
        t.head[p] = f"batch/{bid}"
    return t


def lease(bid):
    return phases.new_lease(bid, LEASED, BASE)


class LeaseOnly(unittest.TestCase):
    def test_lease_without_running_is_restored_and_cleared(self):
        with bench_home():
            tree = tree_on_batch("b0004")
            with S.transaction() as st:
                st["lease"] = lease("b0004")
            recover.entry_point(mk_ctx(tree=tree), setup=False)
            self.assertIsNone(S.read()["lease"])
            for p in LEASED:
                self.assertEqual(tree.head[p], "river-22.2")
            self.assertEqual(tree.sha(".repo/manifests"), BASE[".repo/manifests"])

    def test_a_disturbed_fork_keeps_the_lease_and_pauses(self):
        with bench_home():
            tree = tree_on_batch("b0004")
            tree.dirty.add("vendor/lineage")
            with S.transaction() as st:
                st["lease"] = lease("b0004")
            with self.assertRaises(C.Pause) as cm:
                recover.entry_point(mk_ctx(tree=tree), setup=False)
            self.assertIn("tree-disturbed", cm.exception.message)
            self.assertIsNotNone(S.read()["lease"])

    def test_manifest_moved_under_the_lease_is_caught(self):
        with bench_home():
            tree = tree_on_batch("b0004")
            tree.branches[".repo/manifests"]["default"] = "f" * 40
            with S.transaction() as st:
                st["lease"] = lease("b0004")
            with self.assertRaises(C.Pause):
                recover.entry_point(mk_ctx(tree=tree), setup=False)
            self.assertIsNotNone(S.read()["lease"])

    def test_a_lease_without_restore_to_is_never_guessed(self):
        with bench_home():
            tree = tree_on_batch("b0004")
            old = lease("b0004")
            old.pop("restore_to")
            with S.transaction() as st:
                st["lease"] = old
            with self.assertRaises(C.Pause):
                recover.entry_point(mk_ctx(tree=tree), setup=False)
            self.assertEqual(tree.head["vendor/lineage"], "batch/b0004")    # nothing moved


class AbortRestores(unittest.TestCase):
    def test_abort_restores_every_fork_and_the_manifest(self):
        with bench_home() as h:
            write_json(h / "slot-state.json", {"known_good_slot": "a", "known_good": {"batch": "kg"}})
            tree = tree_on_batch("b0005")
            with S.transaction() as st:
                st["lease"] = lease("b0005")
                st["running"] = {"id": "b0005", "purpose": "batch", "phase": "building", "members": [],
                                 "touched": sorted(LEASED), "base": dict(BASE), "outcome": None}
            recover.abort(mk_ctx(tree=tree))
            st = S.read()
            self.assertIsNone(st["lease"])
            self.assertIsNone(st["running"])
            self.assertIsNone(st["pause"])


if __name__ == "__main__":
    unittest.main()
