"""A settle of a partly merged batch, then the cleanup it schedules, through
the real callers (settle.settle, then recover.cleanup_items) with the real
eligibility check (Sol M4, M5, I9; Gemini GM4, GM6; Opus OM3).

The batch b0001 changed device/motorola/river and vendor/lineage. Its river
batch PR #7 merged; its vendor/lineage batch PR #8 did not (the Perch case
of I9). A second member, gnss, has nothing landed."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
from batchfakes import FakeGH, mk_ctx, write_json
from benchhome import bench_home
from riverbatch import ctx as C
from riverbatch import merge, records, recover, settle
from riverbatch import state as S
from test_batch_records import make_record

RIVER, VENDOR = "zrm625/android_device_motorola_river", "zrm625/android_vendor_lineage"
PR_, PV = "device/motorola/river", "vendor/lineage"
T_R, T_V = "a" * 40, "c" * 40                 # tested (composed) heads
M_R = "9" * 40                                # river's merge commit
H_R, H_V = "1" * 40, "2" * 40                 # perch's approved change-PR heads
H_G = "3" * 40                                # gnss's
KG = {p: "k" * 40 for p in merge.slugs()}


def world(h, cameras_changed=False):
    sl = merge.slugs()
    gh = FakeGH()
    gh.raw = {}
    for p, s in sl.items():
        gh.tips[(s, "refs/heads/river-22.2")] = M_R if p == PR_ else KG[p]
        gh.commits[(s, KG[p])] = {"parents": [], "tree": "1" * 40}
    gh.commits[(RIVER, M_R)] = {"parents": [KG[PR_], T_R], "tree": "2" * 40}
    gh.prs[(RIVER, 7)] = {"state": "MERGED", "headRefName": "batch/b0001", "headRefOid": T_R,
                          "baseRefName": "river-22.2", "isCrossRepository": False, "mergeCommit": {"oid": M_R}}
    gh.prs[(VENDOR, 8)] = {"state": "OPEN", "headRefName": "batch/b0001", "headRefOid": T_V,
                           "baseRefName": "river-22.2", "isCrossRepository": False, "mergeCommit": None}
    gh.tips[(RIVER, "refs/heads/batch/b0001")] = T_R
    gh.tips[(VENDOR, "refs/heads/batch/b0001")] = T_V
    for repo, head, landed in ((RIVER, H_R, True), (VENDOR, H_V, False), ("zrm625/android_build", H_G, False)):
        gh.raw[("gh", "api", f"repos/{repo}/compare/river-22.2...{head}", "--jq", ".status")] = \
            "behind" if landed else "ahead"
    # the open batch PR's tested head is not in river-22.2 (Sol SM1 asks)
    gh.raw[("gh", "api", f"repos/{VENDOR}/compare/river-22.2...{T_V}", "--jq", ".status")] = "ahead"
    if cameras_changed:
        gh.files[(RIVER, "qa/cameras", M_R)] = ("blob", "0 2\n")   # river-22.2's camera set moved
    write_json(h / "batches/b0001/batch.json", {
        "batch": "b0001", "kind": "batch", "commit_set": dict(KG, **{PR_: T_R, PV: T_V}),
        "tag": "known-good-b0001",
        "merge_started": {"at": "t", "expected_prs": {p: ({"repo": sl[p], "number": {PR_: 7, PV: 8}[p]}
                                                          if p in (PR_, PV) else {"repo": sl[p], "number": None})
                                                      for p in sl}}})
    write_json(h / "slot-state.json", {"known_good": {"batch": "base"}, "history": []})
    write_json(h / "qa/known-good.json", {"latest": {"batch": "base", "commit_set": KG, "cameras": [0, 1, 2]}})
    gnss_sha, rec = store_gnss()
    for k, head in records.approved_heads(rec).items():            # gnss's PRs: open at the approved heads
        repo, n = records.split_key(k)
        gh.prs[(repo, n)] = {"state": "OPEN", "headRefName": rec["branch"], "headRefOid": head,
                             "baseRefName": "river-22.2", "isCrossRepository": False}
    with S.transaction() as st:
        S.set_halt(st, f"{VENDOR}#8: neither the base nor the tested merge")
        st["entries"] = [
            {"name": "perch", "status": "in-batch", "batch": "b0001", "rev": 2, "position": 1, "decisions": [],
             "prs": [{"key": f"{RIVER}#6", "path": PR_, "head": H_R}, {"key": f"{VENDOR}#5", "path": PV, "head": H_V}],
             "record_sha256": "x", "identity": f"{RIVER}#6+{VENDOR}#5", "flags": {}},
            {"name": "gnss", "status": "in-batch", "batch": "b0001", "rev": 2, "position": 2, "decisions": [],
             "prs": [{"key": "zrm625/android_build#4", "path": "build/make", "head": H_G}],
             "record_sha256": gnss_sha, "identity": "zrm625/android_build#4", "flags": {}}]
        st["running"] = {"id": "b0001", "purpose": "batch", "members": [{"name": "perch"}, {"name": "gnss"}],
                         "touched": [PR_, PV], "base": dict(KG),
                         "composed": {PR_: {"sha": T_R, "tree": "2" * 40}, PV: {"sha": T_V, "tree": "3" * 40}},
                         "merge": {"prs": {PR_: 7, PV: 8}, "merged": {PR_: M_R}}}
    return gh


def store_gnss() -> tuple[str, dict]:
    with tempfile.TemporaryDirectory() as t:
        rec = make_record(Path(t), name="gnss")
        return records.store_bundle(rec, (Path(t) / "record.json").read_bytes(), Path(t)), rec


def locks():
    return mock.patch.object(C.Locks, "__enter__", lambda self: self), \
        mock.patch.object(C.Locks, "__exit__", lambda self, *a: None)


class PartialSettle(unittest.TestCase):
    def settle(self, gh):
        l1, l2 = locks()
        with l1, l2:
            return settle.settle(mk_ctx(gh=gh), "b0001")

    def cleanup(self, gh):
        deleted = []

        def rgit(ctx, path, *a):
            deleted.append((path, a))
            gh.tips.pop((merge.slugs()[path], a[-1][1:]), None)
        with mock.patch.object(merge, "remote_git", rgit):
            recover.cleanup_items(mk_ctx(gh=gh))
        return deleted

    def test_partial_settle_keeps_the_missing_pr_for_zaks_repair(self):
        with bench_home() as h:
            gh = world(h)
            out = self.settle(gh)
            st = S.read()
            self.assertTrue(out["rebaseline"])
            self.assertEqual({e["name"]: e["status"] for e in st["entries"]},
                             {"perch": "needs-reapproval", "gnss": "queued"})
            (item,) = st["cleanup"]
            self.assertEqual(item["tested"], {PR_: T_R, PV: T_V})                         # Sol M5
            self.assertEqual(item["expected_merged"], {PR_: {"repo": RIVER, "number": 7}})
            self.assertTrue(item["hold"])
            # The next ticks' entry point: held, nothing closed or deleted.
            self.assertEqual(self.cleanup(gh), [])
            self.assertEqual(gh.prs[(VENDOR, 8)]["state"], "OPEN")
            self.assertIn((VENDOR, "refs/heads/batch/b0001"), gh.tips)
            self.assertIsNone(S.read()["pause"])

    def test_after_zaks_repair_and_the_rebaseline_cleanup_finishes(self):
        with bench_home() as h:
            gh = world(h)
            self.settle(gh)
            gh.prs[(VENDOR, 8)].update(state="MERGED", mergeCommit={"oid": "8" * 40})   # Zak's repair
            with S.transaction() as st:
                st["rebaseline"] = None                                                # the re-baseline closed
            deleted = self.cleanup(gh)
            self.assertEqual(S.read()["cleanup"], [])
            self.assertIsNone(S.read()["pause"])                   # neither merge is "without the runner"
            self.assertEqual(sorted(p for p, _ in deleted), [PR_, PV])

    def test_after_a_revert_the_open_pr_is_closed_at_its_tested_head(self):
        with bench_home() as h:
            gh = world(h)
            self.settle(gh)
            with S.transaction() as st:
                st["rebaseline"] = None
            self.cleanup(gh)
            self.assertEqual(gh.prs[(VENDOR, 8)]["state"], "CLOSED")
            self.assertEqual(S.read()["cleanup"], [])

    def test_a_changed_base_camera_set_still_settles(self):
        """Gemini GM6: the fresh eligibility check meets river-22.2's new
        camera set; the member waits for Zak and the settle still owes the
        re-baseline (no deadlock with stage --kind baseline)."""
        with bench_home() as h:
            gh = world(h, cameras_changed=True)
            out = self.settle(gh)
            st = S.read()
            self.assertTrue(out["rebaseline"])
            self.assertEqual(st["rebaseline"]["settle"], "b0001")
            gnss = S.entry(st, "gnss")
            self.assertEqual(gnss["status"], "waiting-zak")
            self.assertIn("camera", gnss["reason"])
            self.assertIsNone(st["running"])
            self.assertIsNone(st["halt"])


if __name__ == "__main__":
    unittest.main()
