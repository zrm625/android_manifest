"""Merge reconciliation and the merge path (R10.4, R10.4a, I4, I5), KW's
commit set (I7), change PRs (R10.7) and cleanup discovery (I12)."""
import unittest
from unittest import mock

import _path  # noqa: F401
from batchfakes import FakeGH, mk_ctx, write_json
from benchhome import bench_home
from riverbatch import ctx as C, merge, recover, state as S

R = "zrm625/android_device_motorola_river"
P = "device/motorola/river"
BASE, TESTED, M, OTHER = "b" * 40, "t" * 40, "9" * 40, "e" * 40
TREE = "c" * 40


def run_dict(**kw):
    return {"id": "b0001", "purpose": "batch", "base": {P: BASE}, "touched": [P],
            "composed": {P: {"sha": TESTED, "tree": TREE}}, "members": [{"name": "perch", "id": "e1"}],
            "merge": {"prs": {P: 7}, "merged": {}, "tag": None}, "attempt_id": "fa-1",
            "versions": {"harness": "h", "runner_build": "rb", "runner_tools": "rt"}, **kw}


def gh_with(state="OPEN", tip=BASE, merge_commit=None, parents=(BASE, TESTED), tree=TREE, head=TESTED):
    gh = FakeGH()
    gh.prs[(R, 7)] = {"state": state, "headRefOid": head, "baseRefName": "river-22.2", "isCrossRepository": False,
                      "mergeCommit": {"oid": merge_commit} if merge_commit else None, "headRefName": "batch/b0001"}
    gh.tips[(R, "refs/heads/river-22.2")] = tip
    gh.commits[(R, M)] = {"parents": list(parents), "tree": tree}
    gh.commits[(R, OTHER)] = {"parents": [BASE], "tree": "f" * 40}
    return gh


def setup_batch(h, merge_started=True):
    b = {"batch": "b0001", "kind": "batch", "experiment": False, "commit_set": {P: TESTED},
         "prs": [{"repo": R, "number": 7}], "no_pr": [], "tag": "known-good-b0001"}
    if merge_started:
        b["merge_started"] = {"at": "t", "expected_prs": {P: {"repo": R, "number": 7}}}
    write_json(h / "batches/b0001/batch.json", b)
    with S.transaction() as st:
        st["running"] = run_dict()


class Classify(unittest.TestCase):
    def obs(self, gh):
        ctx = mk_ctx(gh=gh)
        return merge._with_commit(ctx, R, merge.observe(ctx, R, 7), BASE)

    def test_pending_and_landed(self):
        self.assertEqual(merge.classify(self.obs(gh_with()), BASE, TESTED, TREE), merge.PENDING)
        o = self.obs(gh_with("MERGED", M, M))
        self.assertEqual(merge.classify(o, BASE, TESTED, TREE), merge.LANDED)

    def test_i4_open_after_landed_tip_is_not_landed(self):
        o = self.obs(gh_with("OPEN", M))
        self.assertEqual(merge.classify(o, BASE, TESTED, TREE), "lag")

    def test_landed_needs_every_field(self):
        for kw in ({"merge_commit": OTHER}, {"tree": "d" * 40}, {"parents": (TESTED, BASE)}, {"head": OTHER}):
            args = dict(state="MERGED", tip=M, merge_commit=M) | kw
            self.assertEqual(merge.classify(self.obs(gh_with(**args)), BASE, TESTED, TREE), "other", kw)


class Reconcile(unittest.TestCase):
    def one(self, gh, flip=None):
        ctx = mk_ctx(gh=gh)
        if flip:
            ctx.sleep = lambda s: flip(gh)
        return merge.reconcile_one(ctx, run_dict(), P, R, 7)[0]

    def test_i4_lag_is_unknown_even_when_reads_agree(self):
        self.assertEqual(self.one(gh_with("OPEN", M)), merge.UNKNOWN)

    def test_i5_two_agreeing_other_observations_are_a_mismatch(self):
        self.assertEqual(self.one(gh_with("OPEN", OTHER)), merge.MISMATCH)          # tip moved elsewhere
        self.assertEqual(self.one(gh_with("CLOSED")), merge.MISMATCH)
        self.assertEqual(self.one(gh_with("MERGED", BASE, M)), merge.MISMATCH)      # MERGED, tip still the base

    def test_i5_disagreeing_reads_are_unknown(self):
        def flip(gh):
            gh.tips[(R, "refs/heads/river-22.2")] = M
        self.assertEqual(self.one(gh_with("CLOSED"), flip), merge.UNKNOWN)

    def test_failed_read_raises_unknown(self):
        gh = gh_with()
        gh.fail["pr_view"] = C.Unknown("x")
        with self.assertRaises(C.Unknown):
            self.one(gh)

    def test_reconcile_records_landed_and_halts_on_mismatch(self):
        with bench_home() as h:
            setup_batch(h)
            ctx = mk_ctx(gh=gh_with("MERGED", M, M))
            run = S.read()["running"]
            self.assertEqual(merge.reconcile(ctx, run), {P: merge.LANDED})
            self.assertEqual(S.read()["running"]["merge"]["merged"], {P: M})
        with bench_home() as h:
            setup_batch(h)
            with self.assertRaises(C.Halt):
                merge.reconcile(mk_ctx(gh=gh_with("OPEN", OTHER)), S.read()["running"])
        with bench_home() as h:
            setup_batch(h)
            with self.assertRaises(C.Unknown):
                merge.reconcile(mk_ctx(gh=gh_with("OPEN", M)), S.read()["running"])
            self.assertTrue(S.read()["running"]["reconcile_unknown_since"])      # I5: the 24 h clock


class ReconcileClock(unittest.TestCase):
    def test_a_failed_read_starts_the_i5_clock(self):
        # Sol M2, Gemini minor: a transport exception is an unknown too.
        with bench_home() as h:
            setup_batch(h)
            gh = gh_with()
            gh.fail["pr_view"] = C.Unknown("pr view", R, None, "timeout")
            with self.assertRaises(C.Unknown):
                merge.reconcile(mk_ctx(gh=gh), S.read()["running"])
            first = S.read()["running"]["reconcile_unknown_since"]
            self.assertTrue(first)
            gh.fail["pr_view"] = C.Unknown("pr view", R, None, "timeout")
            with self.assertRaises(C.Unknown):
                merge.reconcile(mk_ctx(gh=gh), run_dict())          # a stale copy never restarts the clock
            self.assertEqual(S.read()["running"]["reconcile_unknown_since"], first)

    def test_lag_that_clears_on_the_second_read_is_landed(self):
        def land(gh):
            gh.prs[(R, 7)].update(state="MERGED", mergeCommit={"oid": M})
        ctx = mk_ctx(gh=gh_with("OPEN", M))
        ctx.sleep = lambda s: land(ctx.gh)
        self.assertEqual(merge.reconcile_one(ctx, run_dict(), P, R, 7)[0], merge.LANDED)


class MergeAll(unittest.TestCase):
    def test_merged_after_the_pending_read_is_reconciled_not_halted(self):
        """Sol M2, Gemini GM3: reconciliation saw it pending, the final read
        sees MERGED: reconcile again, record it, no merge call, no HALT."""
        with bench_home() as h:
            setup_batch(h)
            write_json(h / "slot-state.json", {"pair": {"batch": "b0001", "attempts": ["fa-1"]}})
            gh = gh_with()
            orig = gh.pr_view
            seen = {"n": 0}

            def view(repo, n):
                seen["n"] += 1
                if seen["n"] == 2:                    # someone merged it right after the first reconcile
                    gh.prs[(R, 7)].update(state="MERGED", mergeCommit={"oid": M})
                    gh.tips[(R, "refs/heads/river-22.2")] = M
                return orig(repo, n)
            gh.pr_view = view
            merge.merge_all(mk_ctx(gh=gh), S.read()["running"])
            self.assertEqual(S.read()["running"]["merge"]["merged"], {P: M})
            self.assertFalse(any(c[0] == "merge_pr" for c in gh.calls))


    def test_unexpected_merged_goes_through_reconciliation(self):
        """I4: a PR found MERGED is recorded only through R10.4a; no merge call."""
        with bench_home() as h:
            setup_batch(h)
            gh = gh_with("MERGED", M, M)
            ctx = mk_ctx(gh=gh)
            merge.merge_all(ctx, S.read()["running"])
            self.assertFalse(any(c[0] == "merge_pr" for c in gh.calls))
            self.assertEqual(S.read()["running"]["merge"]["merged"], {P: M})

    def test_merge_then_reconcile(self):
        with bench_home() as h:
            setup_batch(h)
            write_json(h / "slot-state.json", {"pair": {"batch": "b0001", "attempts": ["fa-1"]}})
            gh = gh_with()

            def effect(repo, n, sha):
                gh.prs[(R, 7)].update(state="MERGED", mergeCommit={"oid": M})
                gh.tips[(R, "refs/heads/river-22.2")] = M
            gh.merge_effect = effect
            merge.merge_all(mk_ctx(gh=gh), S.read()["running"])
            self.assertEqual(S.read()["running"]["merge"]["merged"], {P: M})
            self.assertIn(("merge_pr", R, 7, TESTED), gh.calls)

    def test_unknown_merge_outcome_that_did_not_land_is_75(self):
        with bench_home() as h:
            setup_batch(h)
            write_json(h / "slot-state.json", {"pair": {"batch": "b0001", "attempts": ["fa-1"]}})
            gh = gh_with()
            gh.fail["merge_pr"] = C.Unknown("timeout")
            with self.assertRaises(C.Unknown):
                merge.merge_all(mk_ctx(gh=gh), S.read()["running"])

    def test_refused_merge_halts(self):
        with bench_home() as h:
            setup_batch(h)
            write_json(h / "slot-state.json", {"pair": {"batch": "b0001", "attempts": ["fa-1"]}})
            gh = gh_with()
            gh.fail["merge_pr"] = C.Refused("merge", R, 405, "not mergeable")
            with self.assertRaises(C.Halt):
                merge.merge_all(mk_ctx(gh=gh), S.read()["running"])

    def test_pair_gone_halts_before_merging(self):
        with bench_home() as h:
            setup_batch(h)
            gh = gh_with()
            with self.assertRaises(C.Halt):
                merge.merge_all(mk_ctx(gh=gh), S.read()["running"])
            self.assertFalse(any(c[0] == "merge_pr" for c in gh.calls))


class KW(unittest.TestCase):
    """I7: known-good.json's commit set is merge commits for PR repos and
    the tested commits for no_pr repos, never a batch-branch SHA."""

    def promote_state(self, h):
        write_json(h / "slot-state.json", {"known_good": {"batch": "b0001"},
                                           "history": [{"event": "promote", "batch": "b0001"},
                                                       {"event": "fb-mode-recovered"}]})
        write_json(h / "qa/known-good.json", {"latest": {"batch": "base"}, "ram_reference": {"median_mb": 900.0}})

    def test_commit_set_from_merge_commits(self):
        with bench_home() as h:
            setup_batch(h)
            b = h / "batches/b0001/batch.json"
            import json
            d = json.loads(b.read_text())
            d["commit_set"][".repo/manifests"] = "a" * 40
            d["merge_started"]["expected_prs"][".repo/manifests"] = {"repo": "zrm625/android_manifest",
                                                                     "number": None}
            b.write_text(json.dumps(d))
            self.promote_state(h)
            ctx = mk_ctx(gh=gh_with("MERGED", M, M))
            run = S.read()["running"]
            self.assertTrue(merge.kw_holds(ctx, run))
            self.assertIsNone(merge.kw_commit_set(ctx, run, local_only=True))   # not recorded locally yet
            self.assertEqual(merge.kw_commit_set(ctx, run, local_only=False),
                             {P: M, ".repo/manifests": "a" * 40})
            run["merge"]["merged"] = {P: M}
            self.assertEqual(merge.kw_commit_set(ctx, run, local_only=True)[P], M)

    def test_kw_needs_promote_as_last_slot_event(self):
        with bench_home() as h:
            setup_batch(h)
            self.promote_state(h)
            write_json(h / "slot-state.json", {"known_good": {"batch": "b0001"},
                                               "history": [{"event": "promote", "batch": "b0001"},
                                                           {"event": "reserve", "batch": "b0002"}]})
            self.assertFalse(merge.kw_holds(mk_ctx(), S.read()["running"]))

    def test_i7_transport_at_the_known_good_write_is_75_then_written(self):
        """Sol (I7): promoted, the merge commit not recorded locally and no
        promotion record; GitHub gives no answer at KW: 75, nothing written;
        the next forward pass writes it."""
        import json
        with bench_home() as h:
            setup_batch(h)
            self.promote_state(h)
            gh = gh_with("MERGED", M, M)
            gh.fail["pr_view"] = [C.Unknown("pr view", R, 502, "Bad Gateway")]      # once
            rows = [{"run_id": "r1", "host_boot_seconds": "30", "system_used_kb": "1", "vendor_used_kb": "1",
                     "used_ram_kb": str(880 * 1024)}]
            with mock.patch("riverqa.bench.attempt_rows", return_value=rows):
                with self.assertRaises(C.Unknown):
                    recover.recover_running(mk_ctx(gh=gh))
                self.assertEqual(json.loads((h / "qa/known-good.json").read_text())["latest"]["batch"], "base")
                self.assertEqual(S.read()["running"]["id"], "b0001")
                merge.write_known_good(mk_ctx(gh=gh), S.read()["running"])
            kg = json.loads((h / "qa/known-good.json").read_text())
            self.assertEqual((kg["latest"]["batch"], kg["latest"]["commit_set"][P]), ("b0001", M))

    def test_unreadable_merge_commit_is_unknown(self):
        with bench_home() as h:
            setup_batch(h)
            self.promote_state(h)
            gh = gh_with()
            with self.assertRaises(C.Unknown):
                merge.kw_commit_set(mk_ctx(gh=gh), S.read()["running"], local_only=False)


class ChangePRs(unittest.TestCase):
    def test_close_and_reopen_when_head_moves(self):
        with bench_home() as h:
            setup_batch(h)
            gh = FakeGH()
            gh.prs[(R, 6)] = {"state": "OPEN", "headRefOid": TESTED, "baseRefName": "river-22.2",
                              "isCrossRepository": False, "headRefName": "perch"}
            moved = {"n": 0}
            orig = gh.pr_view

            def view(repo, n):
                v = orig(repo, n)
                if gh.prs[(R, 6)]["state"] == "CLOSED":
                    moved["n"] += 1
                    v["headRefOid"] = "f" * 40
                return v
            gh.pr_view = view
            st = S.read()
            st["entries"] = [{"name": "perch", "id": "e1", "batch": "b0001",
                             "prs": [{"key": f"{R}#6", "path": P, "head": TESTED}]}]
            merge.change_prs(mk_ctx(gh=gh), st["running"], st)
            self.assertIn(("reopen_pr", R, 6), gh.calls)
            self.assertEqual(len(gh.comments_by[(R, 6)]), 2)
            self.assertIn("perch", S.read()["running"]["followups"])

    def change_world(self, h):
        setup_batch(h)
        gh = FakeGH()
        gh.prs[(R, 6)] = {"state": "OPEN", "headRefOid": TESTED, "baseRefName": "river-22.2",
                          "isCrossRepository": False, "headRefName": "perch"}
        st = S.read()
        st["entries"] = [{"name": "perch", "id": "e1", "batch": "b0001",
                             "prs": [{"key": f"{R}#6", "path": P, "head": TESTED}]}]
        return gh, st

    def test_interrupted_close_is_checked_again(self):
        """Sol M6: the runner died after closing; a push landed during the
        close. The next pass sees CLOSED with the intent and reopens."""
        with bench_home() as h:
            gh, st = self.change_world(h)

            def close_then_die(repo, n):
                gh.prs[(R, 6)].update(state="CLOSED", headRefOid="f" * 40)
                raise C.Unknown("pr close", R, None, "timed out")
            gh.close_pr = close_then_die
            with self.assertRaises(C.Unknown):
                merge.change_prs(mk_ctx(gh=gh), st["running"], st)
            self.assertEqual(S.read()["running"]["closing"], {f"{R}#6": TESTED})
            gh2 = FakeGH()
            gh2.prs, gh2.comments_by = gh.prs, gh.comments_by
            st = S.read()
            st["entries"] = [{"name": "perch", "id": "e1", "batch": "b0001",
                             "prs": [{"key": f"{R}#6", "path": P, "head": TESTED}]}]
            merge.change_prs(mk_ctx(gh=gh2), st["running"], st)
            self.assertIn(("reopen_pr", R, 6), gh2.calls)
            run = S.read()["running"]
            self.assertIn("perch", run["followups"])
            self.assertEqual(run["closing"], {})

    def test_closed_at_the_approved_head_clears_the_intent(self):
        with bench_home() as h:
            gh, st = self.change_world(h)

            def close_then_die(repo, n):
                gh.prs[(R, 6)]["state"] = "CLOSED"
                raise C.Unknown("pr close", R, None, "timed out")
            gh.close_pr = close_then_die
            with self.assertRaises(C.Unknown):
                merge.change_prs(mk_ctx(gh=gh), st["running"], st)
            gh2 = FakeGH()
            gh2.prs, gh2.comments_by = gh.prs, gh.comments_by
            st = S.read()
            st["entries"] = [{"name": "perch", "id": "e1", "batch": "b0001",
                             "prs": [{"key": f"{R}#6", "path": P, "head": TESTED}]}]
            merge.change_prs(mk_ctx(gh=gh2), st["running"], st)
            self.assertNotIn(("reopen_pr", R, 6), gh2.calls)
            self.assertEqual(S.read()["running"]["closing"], {})

    def test_comment_once(self):
        gh = FakeGH()
        gh.prs[(R, 6)] = {"state": "OPEN"}
        gh.comment_once(R, 6, "<!-- m -->", "hello")
        gh.comment_once(R, 6, "<!-- m -->", "hello")
        self.assertEqual(len(gh.comments_by[(R, 6)]), 1)


class Cleanup(unittest.TestCase):
    """I12: PRs are found by exact head branch in every touched repo, and
    checked before closing."""

    def item(self, h, **kw):
        with S.transaction() as st:
            st["cleanup"] = [dict(S.cleanup_item("b0001", {P: TESTED}, branch_repos={P: TESTED}), **kw)]

    def test_pr_only_item_still_checks_the_tested_head(self):
        """Sol M5, Gemini GM4: the branch is already gone (no branch_repos)
        but the PR is open; it is closed only at the tested head."""
        with bench_home() as h:
            self.item(h, branch_repos={}, pr_repos={P: {"repo": R, "number": 8}})
            gh = FakeGH()
            gh.prs[(R, 8)] = {"state": "OPEN", "headRefName": "batch/b0001", "headRefOid": OTHER,
                              "baseRefName": "river-22.2", "isCrossRepository": False}
            with self.assertRaises(C.Pause):
                recover.cleanup_items(mk_ctx(gh=gh))
            self.assertEqual(gh.prs[(R, 8)]["state"], "OPEN")
            gh.prs[(R, 8)]["headRefOid"] = TESTED
            with S.transaction() as st:
                st["pause"] = None
            recover.cleanup_items(mk_ctx(gh=gh))
            self.assertEqual(gh.prs[(R, 8)]["state"], "CLOSED")

    def test_no_tested_head_never_closes(self):
        with bench_home() as h:
            self.item(h, tested={}, branch_repos={}, pr_repos={P: {"repo": R, "number": 8}})
            gh = FakeGH()
            gh.prs[(R, 8)] = {"state": "OPEN", "headRefName": "batch/b0001", "headRefOid": TESTED,
                              "baseRefName": "river-22.2", "isCrossRepository": False}
            with self.assertRaises(C.Pause) as cm:
                recover.cleanup_items(mk_ctx(gh=gh))
            self.assertIn("no tested head", cm.exception.message)
            self.assertEqual(gh.prs[(R, 8)]["state"], "OPEN")

    def test_expected_merged_batch_pr_is_not_a_pause(self):
        """Opus OM3: the batch PR merged through the runner; its branch is
        deleted, nothing pauses."""
        with bench_home() as h:
            self.item(h, expected_merged={P: {"repo": R, "number": 8}})
            gh = FakeGH()
            gh.prs[(R, 8)] = {"state": "MERGED", "headRefName": "batch/b0001", "headRefOid": TESTED,
                              "baseRefName": "river-22.2", "isCrossRepository": False}
            gh.tips[(R, "refs/heads/batch/b0001")] = TESTED
            deleted = []
            with mock.patch.object(merge, "remote_git", lambda ctx, path, *a: deleted.append(a)):
                recover.cleanup_items(mk_ctx(gh=gh))
            self.assertEqual(S.read()["cleanup"], [])
            self.assertTrue(any(a[0] == "push" for a in deleted))

    def test_third_same_refusal_pauses_and_stops(self):
        with bench_home() as h:
            self.item(h)
            gh = FakeGH()
            gh.prs[(R, 8)] = {"state": "OPEN", "headRefName": "batch/b0001", "headRefOid": TESTED,
                              "baseRefName": "river-22.2", "isCrossRepository": False}
            gh.fail["close_pr"] = C.Refused("pr close", R, 403, "nope")
            recover.cleanup_items(mk_ctx(gh=gh))
            gh.fail["close_pr"] = C.Refused("pr close", R, 403, "nope")
            recover.cleanup_items(mk_ctx(gh=gh))
            gh.fail["close_pr"] = C.Refused("pr close", R, 403, "nope")
            with self.assertRaises(C.Pause):                        # Gemini minor: the tick stops here
                recover.cleanup_items(mk_ctx(gh=gh))
            self.assertIn("three ticks", S.read()["pause"]["reason"])

    def test_discovers_unsaved_pr_and_deletes_branch(self):
        with bench_home() as h:
            self.item(h)
            gh = FakeGH()
            gh.prs[(R, 8)] = {"state": "OPEN", "headRefName": "batch/b0001", "headRefOid": TESTED,
                              "baseRefName": "river-22.2", "isCrossRepository": False}
            gh.prs[(R, 9)] = {"state": "OPEN", "headRefName": "batch/b0001x", "headRefOid": TESTED,
                              "baseRefName": "river-22.2", "isCrossRepository": False}
            ctx = mk_ctx(gh=gh)
            ctx.git = mock.Mock()
            with mock.patch.object(merge, "remote_git", lambda *a: ""):
                recover.cleanup_items(ctx)
            self.assertEqual(gh.prs[(R, 8)]["state"], "CLOSED")
            self.assertEqual(gh.prs[(R, 9)]["state"], "OPEN")                # not an exact head match
            self.assertEqual(S.read()["cleanup"], [])

    def test_pr_at_another_head_is_left_and_pauses(self):
        with bench_home() as h:
            self.item(h)
            gh = FakeGH()
            gh.prs[(R, 8)] = {"state": "OPEN", "headRefName": "batch/b0001", "headRefOid": OTHER,
                              "baseRefName": "river-22.2", "isCrossRepository": False}
            with self.assertRaises(C.Pause):
                recover.cleanup_items(mk_ctx(gh=gh))
            self.assertEqual(gh.prs[(R, 8)]["state"], "OPEN")
            self.assertEqual(len(S.read()["cleanup"]), 1)

    def test_merged_batch_pr_pauses(self):
        with bench_home() as h:
            self.item(h)
            gh = FakeGH()
            gh.prs[(R, 8)] = {"state": "MERGED", "headRefName": "batch/b0001", "headRefOid": TESTED,
                              "baseRefName": "river-22.2", "isCrossRepository": False}
            with self.assertRaises(C.Pause):
                recover.cleanup_items(mk_ctx(gh=gh))

    def test_branch_at_another_sha_is_left(self):
        with bench_home() as h:
            self.item(h)
            gh = FakeGH()
            gh.tips[(R, "refs/heads/batch/b0001")] = OTHER
            with self.assertRaises(C.Pause):
                recover.cleanup_items(mk_ctx(gh=gh))
            self.assertEqual(len(S.read()["cleanup"]), 1)


if __name__ == "__main__":
    unittest.main()
