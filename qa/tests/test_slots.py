import subprocess
import unittest

import _path  # noqa: F401
from ghworld import GHWorld
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
        self.assertEqual(slots.preflight(None, "base", "baseline"), [])
        self.assertTrue(slots.preflight(None, "x", "batch"))
        st = base()
        # R8-3: preflight is host-only; the running slot is checked on the
        # phone by verify_boot, after it.
        self.assertEqual(slots.preflight(st, "base", "baseline"), [])
        # Batches need a closed baseline.
        self.assertTrue(any("baseline" in e for e in slots.preflight(st, "x", "batch")))
        self.assertTrue(slots.preflight(st, "x", "nonsense"))

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
        self.assertTrue(any("unresolved pair" in e for e in slots.preflight(st, "other", "batch")))
        self.assertTrue(any("own pair" in e for e in slots.preflight(st, "base", "baseline")))

    def test_rollback_releases_and_retry_reuses_target(self):
        st = base()
        p = slots.reserve(st, "base", "baseline", "h" * 64, "t1")
        slots.mark_written(st, "att1", "t2")
        with self.assertRaises(slots.SlotError):
            slots.rolled_back(st, "a", "wrong slot", "t3")
        slots.rolled_back(st, "b", "verified", "t3")
        self.assertIsNone(st["pair"])
        self.assertEqual(slots.preflight(st, "base", "baseline"), [])
        p2 = slots.reserve(st, "base", "baseline", "h" * 64, "t4")
        self.assertEqual(p2["target"], p["target"])

    def test_baseline_close_then_batch(self):
        st = base("b")
        slots.reserve(st, "base", "baseline", "h" * 64, "t1")
        slots.close_baseline(st, "base", "id-1", "c" * 64, "t2", seed={"ram_reference": {}})
        self.assertEqual(st["known_good_slot"], "a")
        # The seed rides in the same write; until it is written out, no batch.
        self.assertTrue(any("checkpoint" in e for e in slots.preflight(st, "b1", "batch")))
        slots.seed_written(st, "t2")
        self.assertFalse(st["known_good"]["provisional"])
        self.assertIsNotNone(st["baseline_closed"])
        self.assertEqual(slots.preflight(st, "b1", "batch"), [])
        p = slots.reserve(st, "b1", "batch", "c" * 64, "t3")
        self.assertEqual(p["target"], "b")

    def test_unstamped_baseline_closes_on_dates_slot_and_hash(self):
        st = base("b")
        slots.reserve(st, "base", "baseline", "h" * 64, "t1")
        with self.assertRaises(slots.SlotError):
            slots.close_baseline(st, "base", "unstamped", "c" * 64, "t2", seed={"x": 1})
        with self.assertRaises(slots.SlotError):   # identity names the wrong slot
            slots.close_baseline(st, "base", None, "c" * 64, "t2", seed={"x": 1},
                                 identity={"dates": {"build": "1", "vendor": "2", "bootimage": "3"}, "slot": "b"})
        slots.close_baseline(st, "base", "unstamped", "c" * 64, "t2", seed={"x": 1},
                             identity={"dates": {"build": "1", "vendor": "2", "bootimage": "3"}, "slot": "a"})
        self.assertIsNone(st["known_good"]["build_id"])
        self.assertEqual(st["known_good"]["boot_sha256"], "c" * 64)
        self.assertEqual(slots.checkpoint_pending(st), {"x": 1})

    def test_promote_rules(self):
        st = base("b")
        slots.reserve(st, "cal", "calibration", "h" * 64, "t1")
        with self.assertRaises(slots.SlotError):
            slots.promote(st, "cal", "id", "c" * 64, "t2")
        st = base("b")
        slots.reserve(st, "b1", "batch", "h" * 64, "t1")
        with self.assertRaises(slots.SlotError):
            slots.promote(st, "b1", "", "c" * 64, "t2")
        with self.assertRaises(slots.SlotError):
            slots.promote(st, "b1", "unstamped", "c" * 64, "t2")
        slots.promote(st, "b1", "id", "c" * 64, "t2")
        self.assertEqual(st["known_good_slot"], "a")
        st = base("b")
        slots.reserve(st, "base", "baseline", "h" * 64, "t1")
        with self.assertRaises(slots.SlotError):   # baselines close only through close_baseline
            slots.promote(st, "base", "id", "c" * 64, "t2")


# The tested commit set: repos a and b have PRs, c has none.
COMMITS = {"a": "a" * 40, "b": "b" * 40, "c": "c" * 40}
SLUGS = {k: f"zrm625/{k}" for k in COMMITS}
TREE, TREE2 = "1" * 40, "2" * 40


MERGE = {"m0": "5" * 40, "m1": "6" * 40}
BASES = {"a": "7" * 40, "b": "8" * 40}


def stub_runner(merged, tags, fail=None, tag_commit=None, annotated=True, heads=None, base=None, trees=None,
                cross=None):
    """A recorded GitHub (ghworld.GHWorld). PR n of repo "ab"[n] lands as
    merge commit MERGE["m<n>"] = [BASES[repo], head] on river-22.2; an
    unmerged PR's river-22.2 is still at BASES[repo]; c (no PR) is at its
    tested commit. PR heads are the tested commits unless ``heads`` says
    otherwise; PR bases are river-22.2 unless ``base`` says otherwise. A tag
    in repo zrm625/<x> points at tag_commit[x] or, by default, its PR's
    merge commit (or, for c, the tested commit). Every commit's tree is
    TREE unless ``trees`` maps a commit (or "m0"/"m1") to another tree."""
    w = GHWorld(annotated=annotated)
    trees = {MERGE.get(k, k): v for k, v in (trees or {}).items()}
    for repo in "abc":
        slug = f"zrm625/{repo}"
        w.repo(slug, tip=COMMITS[repo] if repo == "c" else BASES[repo])
        w.commits[(slug, COMMITS[repo])] = {"tree": trees.get(COMMITS[repo], TREE), "parents": ["0" * 40]}
        if repo in BASES:
            w.commits[(slug, BASES[repo])] = {"tree": "4" * 40, "parents": ["0" * 40]}
    for n, repo in enumerate("ab"):
        slug = f"zrm625/{repo}"
        w.pr(slug, n, (heads or {}).get(repo, COMMITS[repo]), base=(base or {}).get(repo, "river-22.2"),
             cross=(cross or {}).get(repo, False))
        if merged[n]:
            m = MERGE[f"m{n}"]
            w.merge(slug, n, BASES[repo], m, trees.get(m, TREE))
    for repo in "abc":
        if tags[repo]:
            at = (tag_commit or {}).get(repo, COMMITS["c"] if repo == "c" else MERGE["m" + str("ab".index(repo))])
            w.repos[f"zrm625/{repo}"]["tags"]["kg-1"] = at
            w.commits.setdefault((f"zrm625/{repo}", at), {"tree": trees.get(at, TREE), "parents": ["0" * 40]})
    if fail:
        w.fail = lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "network down") if cmd[0] == fail else None
    return w


EXPECTED = {"a": {"repo": "zrm625/a", "number": 0}, "b": {"repo": "zrm625/b", "number": 1},
            "c": {"repo": "zrm625/c", "number": None}}
UNSTARTED = {"commit_set": COMMITS,
             "prs": [{"repo": "zrm625/a", "number": 0}, {"repo": "zrm625/b", "number": 1}], "no_pr": ["c"],
             "tag": "kg-1",
             "tag_repos": ["https://github.com/zrm625/a", "https://github.com/zrm625/b",
                           "https://github.com/zrm625/c"]}
BATCH = dict(UNSTARTED, merge_started={"at": "t", "expected_prs": EXPECTED})
ALL = {"a": True, "b": True, "c": True}
NONE = {"a": False, "b": False, "c": False}


class Recovery(unittest.TestCase):
    def decide(self, merged, tags, kind="batch", fail=None, batch=None, **kw):
        batch = batch or BATCH
        look = recovery.lookup(batch, stub_runner(merged, tags, fail, **kw), SLUGS).data
        return slots.decide_recovery({"kind": kind, "batch": "b"}, look, batch, SLUGS)[0]

    def test_all_merged_and_tagged_promotes(self):
        # The "tag pushed, host killed before slot-state" control.
        self.assertEqual(self.decide([True, True], ALL), slots.PROMOTE)
        self.assertEqual(self.decide([True, True], ALL, annotated=False), slots.PROMOTE)

    def test_tag_must_point_at_the_merge_commit(self):
        self.assertEqual(self.decide([True, True], ALL, tag_commit={"b": "other"}), slots.HALT)

    def test_repo_without_a_pr_needs_the_tag_at_the_tested_commit(self):
        self.assertEqual(self.decide([True, True], ALL, tag_commit={"c": "d" * 40}), slots.HALT)

    def test_pr_head_must_be_the_tested_commit(self):
        self.assertEqual(self.decide([True, True], ALL, heads={"a": "e" * 40}), slots.HALT)

    def test_every_repo_of_the_commit_set_must_be_covered(self):
        short = dict(BATCH, tag_repos=BATCH["tag_repos"][:2])          # c missing from tag_repos
        self.assertEqual(self.decide([True, True], ALL, batch=short), slots.HALT)
        extra = dict(BATCH, commit_set=dict(COMMITS, d="d" * 40))      # d is tested but never tagged
        self.assertEqual(self.decide([True, True], ALL, batch=extra), slots.HALT)

    def test_stale_pr_record_halts(self):
        stale = dict(BATCH, commit_set={"a": "a" * 40, "c": "c" * 40},
                     tag_repos=["https://github.com/zrm625/a", "https://github.com/zrm625/c"])
        # b's PR is not part of the tested commit set.
        self.assertEqual(self.decide([True, True], ALL, batch=stale), slots.HALT)

    def test_none_rolls_back(self):
        self.assertEqual(self.decide([False, False], NONE, batch=UNSTARTED), slots.ROLLBACK)
        self.assertEqual(self.decide([False, False], NONE), slots.HALT)      # merge_started: mid-merge

    def test_partial_halts(self):
        self.assertEqual(self.decide([True, False], NONE), slots.HALT)
        self.assertEqual(self.decide([True, True], dict(ALL, b=False)), slots.HALT)

    def test_lookup_failure_halts(self):
        self.assertEqual(self.decide([False, False], NONE, fail="gh"), slots.HALT)
        self.assertEqual(self.decide([False, False], NONE, fail="git"), slots.HALT)

    # --- review round 3: the frozen PR set, the base branch and the tree ---

    def test_no_frozen_pr_set_never_promotes(self):
        self.assertEqual(self.decide([True, True], ALL, batch=UNSTARTED), slots.HALT)
        old = dict(UNSTARTED, merge_started={"at": "t"})                  # recorded without the set
        self.assertEqual(self.decide([True, True], ALL, batch=old), slots.HALT)

    def test_repo_cannot_drop_from_pr_to_no_pr(self):
        # After merge_started, the runner rewrites b as "no PR" and drops its
        # PR: recovery still looks b's PR up from the frozen set, and the
        # changed record halts.
        dropped = dict(BATCH, prs=[{"repo": "zrm625/a", "number": 0}], no_pr=["b", "c"])
        self.assertEqual(self.decide([True, True], ALL, batch=dropped), slots.HALT)
        look = recovery.lookup(dropped, stub_runner([True, True], ALL), SLUGS).data
        self.assertEqual(sorted(p["repo"] for p in look["prs"]), ["zrm625/a", "zrm625/b"])
        errs = slots.commit_set_proof(dropped, look, SLUGS)
        self.assertTrue(any("changed after merge_started" in e for e in errs), errs)

    def test_pr_must_merge_into_river_22_2(self):
        self.assertEqual(self.decide([True, True], ALL, base={"a": "main"}), slots.HALT)

    def test_tree_lookup_must_return_a_sha(self):
        """Review round 4 (Gemini 5): exit 0 with something that isn't a tree
        hash is a failed lookup, never a tree."""
        for out in ("T\n", "", "<html>rate limited</html>\n", "1" * 39 + "\n", "1" * 40 + "x\n"):
            r = lambda cmd, out=out: subprocess.CompletedProcess(cmd, 0, out, "")
            with self.assertRaises(recovery._Failed) as cm:
                recovery.tree_of(r, "zrm625/a", "a" * 40)
            self.assertEqual(cm.exception.outcome, recovery.MALFORMED, out)
        ok = lambda cmd: subprocess.CompletedProcess(cmd, 0, "1" * 40 + "\n", "")
        self.assertEqual(recovery.tree_of(ok, "zrm625/a", "a" * 40), "1" * 40)
        look = recovery.lookup(BATCH, stub_runner([True, True], ALL, trees={"m1": "T"}), SLUGS)
        self.assertEqual((look.outcome, look.data), (recovery.MALFORMED, None))

    def test_tagged_tree_must_be_the_tested_tree(self):
        # b's PR was merged into a river-22.2 that had moved on: the merge
        # commit's tree holds content that was never tested.
        self.assertEqual(self.decide([True, True], ALL, trees={"m1": TREE2}), slots.HALT)
        look = recovery.lookup(BATCH, stub_runner([True, True], ALL, trees={"m1": TREE2}), SLUGS).data
        errs = slots.commit_set_proof(BATCH, look, SLUGS)
        self.assertTrue(any("tested tree" in e for e in errs), errs)

    def test_tree_lookup_failure_halts(self):
        def run(cmd, inner=stub_runner([True, True], ALL)):
            if cmd[:2] == ["gh", "api"]:
                return subprocess.CompletedProcess(cmd, 1, "", "rate limited")
            return inner(cmd)
        self.assertEqual(recovery.lookup(BATCH, run, SLUGS).outcome, recovery.TRANSPORT)

    def test_expected_pr_set(self):
        exp, errs = slots.expected_pr_set(UNSTARTED, SLUGS)
        self.assertEqual(exp, EXPECTED, errs)
        for bad in (dict(UNSTARTED, no_pr=[]),                                  # c has neither
                    dict(UNSTARTED, no_pr=["c", "a"]),                          # a has both
                    dict(UNSTARTED, prs=UNSTARTED["prs"] + [{"repo": "zrm625/z", "number": 9}]),
                    dict(UNSTARTED, prs=[{"repo": "zrm625/a", "number": "0"}, UNSTARTED["prs"][1]])):
            self.assertIsNone(slots.expected_pr_set(bad, SLUGS)[0], bad)
        self.assertIsNone(slots.expected_pr_set(UNSTARTED, dict(SLUGS, a="lineage/a"))[0])

    def test_pairs_without_prs_roll_back(self):
        for kind in ("baseline", "calibration", "experiment"):
            self.assertEqual(slots.decide_recovery({"kind": kind}, None)[0], slots.ROLLBACK)


class MergeStarted(unittest.TestCase):
    """Empty or unmerged records: merge_started tells "never started" from
    "interrupted mid-merge" (review round 2, C1)."""
    EMPTY = {"prs": [], "tags": []}

    def decide(self, look, **batch):
        return slots.decide_recovery({"kind": "batch"}, look, dict({"commit_set": COMMITS}, **batch), SLUGS)[0]

    def test_no_record_and_no_marker_rolls_back(self):
        self.assertEqual(self.decide(self.EMPTY), slots.ROLLBACK)
        self.assertEqual(self.decide(self.EMPTY, merge_started=None), slots.ROLLBACK)

    def test_no_record_with_marker_halts(self):
        self.assertEqual(self.decide(self.EMPTY, merge_started={"at": "t"}), slots.HALT)

    def test_nothing_merged_with_marker_halts(self):
        look = recovery.lookup(UNSTARTED, stub_runner([False, False], NONE), SLUGS).data
        self.assertEqual(slots.decide_recovery({"kind": "batch"}, look, UNSTARTED, SLUGS)[0], slots.ROLLBACK)
        self.assertEqual(slots.decide_recovery({"kind": "batch"}, look, BATCH, SLUGS)[0], slots.HALT)

    def test_partial_with_marker_halts(self):
        look = recovery.lookup(BATCH, stub_runner([True, False], NONE), SLUGS).data
        self.assertEqual(slots.decide_recovery({"kind": "batch"}, look, BATCH, SLUGS)[0], slots.HALT)

    def test_complete_with_marker_promotes(self):
        look = recovery.lookup(BATCH, stub_runner([True, True], ALL), SLUGS).data
        self.assertEqual(slots.decide_recovery({"kind": "batch"}, look, BATCH, SLUGS)[0], slots.PROMOTE)


if __name__ == "__main__":
    unittest.main()
