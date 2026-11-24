"""The merge path end to end with fakes (R10.2-R10.8): batch PRs,
merge_started, merge with reconciliation, landed check, tags, promotion,
known-good.json (KW, I7), change PRs and finish."""
import json
import unittest
from unittest import mock

import _path  # noqa: F401
from batchfakes import FakeGH, FakeTools, mk_ctx, write_json
from benchhome import bench_home
from riverbatch import merge, recover, state as S, tick

P = "device/motorola/river"
BASE = {p: f"{i:x}" * 40 for i, p in enumerate(sorted(merge.slugs()), start=1)}
BASE = {p: (v * 40)[:40] for p, v in BASE.items()}
TESTED, TT, M = "a" * 40, "c" * 40, "9" * 40
VERS = {"harness": "h", "runner_build": "rb", "runner_tools": "rt"}


class FakeGit:
    def __init__(self):
        self.tags, self.calls = {}, []

    def try_(self, path, *args, env=None):
        self.calls.append((path, args))
        if args[:2] == ("symbolic-ref", "--short"):
            return 0, "default\n" if path == ".repo/manifests" else "river-22.2\n"
        if args[:3] == ("rev-parse", "-q", "--verify"):
            return (0, self.tags[(path, args[3])]) if (path, args[3]) in self.tags else (1, "")
        return 0, ""

    def out(self, path, *args, env=None):
        self.calls.append((path, args))
        if "tag" in args:
            i = args.index("tag")
            tag, want = args[i + 2], args[-1]
            self.tags[(path, f"refs/tags/{tag}^{{}}")] = want
        return ""

    def raw(self, path, *args):
        return b""


def world(h):
    sl = merge.slugs()
    gh = FakeGH()
    for p, s in sl.items():
        gh.tips[(s, "refs/heads/river-22.2")] = BASE[p]
    R = sl[P]
    gh.prs[(R, 6)] = {"state": "OPEN", "headRefName": "perch", "headRefOid": TESTED, "baseRefName": "river-22.2",
                      "isCrossRepository": False}
    gh.commits[(R, M)] = {"parents": [BASE[P], TESTED], "tree": TT}

    def effect(repo, n, sha):
        gh.prs[(repo, n)].update(state="MERGED", mergeCommit={"oid": M})
        gh.tips[(repo, "refs/heads/river-22.2")] = M
    gh.merge_effect = effect
    git = FakeGit()

    def remote_git(ctx, path, *args):
        s = sl[path]
        if args[0] == "push":
            ref = args[-1]
            if ref.startswith("refs/tags/"):
                gh.tips[(s, ref + "^{}")] = git.tags[(path, ref + "^{}")]
            elif ref.startswith(":"):
                gh.tips.pop((s, ref[1:]), None)
            else:
                sha, dst = ref.split(":")
                gh.tips[(s, dst)] = sha
        return ""

    cs = {p: (TESTED if p == P else BASE[p]) for p in sl}
    write_json(h / "batches/b0001/batch.json", {"batch": "b0001", "kind": "batch", "experiment": False,
                                                "camera_set_change": None, "commit_set": cs,
                                                "cameras": {"set": [0, 1, 2]}, "flash_attempt": "fa-1",
                                                "bench_runs": ["r1"]})
    write_json(h / "slot-state.json", {"known_good_slot": "a", "known_good": {"batch": "kg"},
                                       "pair": {"batch": "b0001", "kind": "batch", "target": "b",
                                                "attempts": ["fa-1"]}, "history": [{"event": "reserve",
                                                                                     "batch": "b0001"}]})
    write_json(h / "qa/known-good.json", {"latest": {"batch": "kg", "cameras": [0, 1, 2]},
                                          "ram_reference": {"median_mb": 900.0, "seed": None}})
    with S.transaction() as st:
        st["entries"] = [{"name": "perch", "status": "in-batch", "batch": "b0001", "rev": 2, "position": 1,
                          "prs": [{"key": f"{R}#6", "path": P, "head": TESTED}], "flags": {}, "kind": "change",
                          "identity": f"{R}#6", "decisions": []}]
        st["running"] = {"id": "b0001", "purpose": "batch", "phase": "decided", "members": [{"name": "perch"}],
                         "touched": [P], "base": dict(BASE),
                         "composed": {p: {"sha": cs[p], "tree": TT if p == P else "d" * 40} for p in sl
                                      if p != ".repo/manifests"},
                         "content_key": "k", "attempt_id": "fa-1", "versions": dict(VERS, image_id="i"),
                         "merge": {"prs": {}, "merged": {}, "tag": None}, "outcome": {"class": "pass"},
                         "qa": {"run_id": "qa-1"}}

    def merge_started(args):
        b = json.loads((h / "batches/b0001/batch.json").read_text())
        exp = {p: {"repo": sl[p], "number": None} for p in b["no_pr"]}
        exp.update({next(p for p, s in sl.items() if s == x["repo"]): {"repo": x["repo"], "number": x["number"]}
                    for x in b["prs"]})
        b["merge_started"] = {"at": "t", "expected_prs": exp}
        (h / "batches/b0001/batch.json").write_text(json.dumps(b))
        return 0, ""

    def promote(args):
        ss = json.loads((h / "slot-state.json").read_text())
        ss["known_good"] = {"batch": "b0001"}
        ss["known_good_slot"] = "b"
        ss["pair"] = None
        ss["history"].append({"event": "promote", "batch": "b0001"})
        (h / "slot-state.json").write_text(json.dumps(ss))
        return 0, ""

    def flash(args):
        return {"--merge-started": merge_started, "--promote": promote}[args[0]](args)
    tools = FakeTools({"flash": flash})
    return gh, git, tools, remote_git


ROWS = [{"run_id": "r1", "host_boot_seconds": "31", "system_used_kb": "10", "vendor_used_kb": "5",
         "used_ram_kb": str(880 * 1024)}]


class MergePath(unittest.TestCase):
    def test_clean_batch_merges_tags_promotes_and_finishes(self):
        with bench_home() as h:
            gh, git, tools, rgit = world(h)
            ctx = mk_ctx(gh=gh, tools=tools)
            ctx.git = git
            with mock.patch.object(merge, "remote_git", rgit), mock.patch.object(tick, "remote_git", rgit), \
                    mock.patch.object(tick.version, "current", return_value=dict(VERS)), \
                    mock.patch.object(recover.version, "current", return_value=dict(VERS)), \
                    mock.patch("riverqa.bench.attempt_rows", return_value=ROWS):
                tick.merge_path(ctx)
            st = S.read()
            R = merge.slugs()[P]
            self.assertIsNone(st["running"])
            self.assertEqual(st["entries"][0]["status"], "merged")
            self.assertEqual(st["cleanup"], [])
            batch_pr = next(n for (r, n), p in gh.prs.items() if p.get("headRefName") == "batch/b0001")
            self.assertIn(("merge_pr", R, batch_pr, TESTED), gh.calls)
            self.assertEqual(gh.prs[(R, 6)]["state"], "CLOSED")                       # the change PR
            kg = json.loads((h / "qa/known-good.json").read_text())
            self.assertEqual(kg["latest"]["batch"], "b0001")
            self.assertEqual(kg["latest"]["commit_set"][P], M)                        # I7: the merge commit
            self.assertEqual(kg["latest"]["commit_set"]["vendor/lineage"], BASE["vendor/lineage"])
            for p, s in merge.slugs().items():                                        # every repo tagged
                want = M if p == P else BASE[p]
                self.assertEqual(gh.tips[(s, "refs/tags/known-good-b0001^{}")], want, p)
            self.assertNotIn((R, "refs/heads/batch/b0001"), gh.tips)                 # branch deleted
            self.assertEqual(st["strikes"]["transport_prs"], 0)
            body = gh.bodies[(R, "batch/b0001")]                                      # Sol minor
            self.assertIn(f"https://github.com/{R}/pull/6 at `{TESTED}`", body)
            self.assertIn("council record", body)
            self.assertIn("boot 31 s, used RAM 880 MB", body)

    def test_moved_base_before_merge_started_is_a_void(self):
        with bench_home() as h:
            gh, git, tools, rgit = world(h)
            gh.tips[(merge.slugs()["vendor/lineage"], "refs/heads/river-22.2")] = "f" * 40
            ctx = mk_ctx(gh=gh, tools=tools)
            ctx.git = git
            from riverbatch import ctx as C
            with mock.patch.object(merge, "remote_git", rgit), mock.patch.object(tick, "remote_git", rgit), \
                    mock.patch.object(tick.version, "current", return_value=dict(VERS)):
                with self.assertRaises(C.Abort) as cm:
                    tick.merge_path(ctx)
            self.assertIn("stale-base", cm.exception.outcome["reason"])
            self.assertFalse(any(c[0] == "merge_pr" for c in gh.calls))
            self.assertEqual(len(S.read()["cleanup"]), 1)                              # the item to clean up

    def test_promote_lag_is_75_and_resumes_forward(self):
        with bench_home() as h:
            gh, git, tools, rgit = world(h)
            ctx = mk_ctx(gh=gh, tools=tools)
            ctx.git = git
            from riverbatch import ctx as C
            seq = {"n": 0}

            def flash(args):
                if args[0] == "--merge-started":
                    b = json.loads((h / "batches/b0001/batch.json").read_text())
                    sl = merge.slugs()
                    exp = {p: {"repo": sl[p], "number": None} for p in b["no_pr"]}
                    for x in b["prs"]:
                        exp[P] = {"repo": x["repo"], "number": x["number"]}
                    b["merge_started"] = {"at": "t", "expected_prs": exp}
                    (h / "batches/b0001/batch.json").write_text(json.dumps(b))
                    return 0, ""
                seq["n"] += 1
                return 75, ""
            tools.script["flash"] = flash
            with mock.patch.object(merge, "remote_git", rgit), mock.patch.object(tick, "remote_git", rgit), \
                    mock.patch.object(tick.version, "current", return_value=dict(VERS)), \
                    mock.patch.object(recover.version, "current", return_value=dict(VERS)):
                with self.assertRaises(C.Unknown):
                    tick.merge_path(ctx)
                st = S.read()
                self.assertEqual(st["running"]["phase"], "tagging")
                self.assertEqual(st["running"]["merge"]["merged"], {P: M})
                # the next tick: forward again, no second merge
                with self.assertRaises(C.Unknown):
                    recover.recover_running(ctx)
            self.assertEqual(sum(1 for c in gh.calls if c[0] == "merge_pr"), 1)
            self.assertEqual(seq["n"], 2)

    def patches(self, rgit):
        return [mock.patch.object(merge, "remote_git", rgit), mock.patch.object(tick, "remote_git", rgit),
                mock.patch.object(tick.version, "current", return_value=dict(VERS)),
                mock.patch.object(recover.version, "current", return_value=dict(VERS)),
                mock.patch("riverqa.bench.attempt_rows", return_value=ROWS)]

    def test_pair_lost_after_the_final_merge_halts_before_any_tag(self):
        """Sol M3: every merge recorded, the pair gone: no tag, a HALT."""
        from riverbatch import ctx as C
        with bench_home() as h:
            gh, git, tools, rgit = world(h)
            effect = gh.merge_effect

            def merge_and_lose_pair(repo, n, sha):
                effect(repo, n, sha)
                ss = json.loads((h / "slot-state.json").read_text())
                ss["pair"] = {"batch": "b0001", "kind": "batch", "target": "b", "attempts": ["fa-other"]}
                (h / "slot-state.json").write_text(json.dumps(ss))
            gh.merge_effect = merge_and_lose_pair
            ctx = mk_ctx(gh=gh, tools=tools)
            ctx.git = git
            ps = self.patches(rgit)
            for x in ps:
                x.start()
            try:
                with self.assertRaises(C.Halt) as cm:
                    tick.merge_path(ctx)
                self.assertIn("no longer reserves", cm.exception.message)
                # resumed with every merge already recorded: still no tag
                with self.assertRaises(C.Halt):
                    recover.recover_running(ctx)
            finally:
                mock.patch.stopall()
            self.assertEqual(S.read()["running"]["merge"]["merged"], {P: M})
            self.assertFalse(any(k[1].startswith("refs/tags/") for k in gh.tips))
            self.assertFalse(any(c[0] == "flash" and c[1][0] == "--promote" for c in tools.calls))

    def test_lookup_pause_has_its_strike_key_and_resume_clears_it(self):
        """Sol M7, Gemini GM5: promote's lookup pause names the strike;
        resume clears every strike, so stage and qualify are not refused
        forever afterwards."""
        from riverbatch import commands, ctx as C, qualify
        with bench_home() as h:
            gh, git, tools, rgit = world(h)
            promote = tools.script["flash"]
            fail = {"n": 2}

            def flash(args):
                if args[0] == "--promote" and fail["n"]:
                    fail["n"] -= 1
                    return 1, json.dumps({"lookup": "refused", "repo": "zrm625/x",
                                          "evidence": {"status": 404}})
                return promote(args)
            tools.script["flash"] = flash
            ctx = mk_ctx(gh=gh, tools=tools)
            ctx.git = git
            ps = self.patches(rgit)
            for x in ps:
                x.start()
            try:
                with self.assertRaises(C.Unknown):
                    tick.merge_path(ctx)
                with self.assertRaises(C.Pause) as cm:
                    recover.recover_running(ctx)
                tick.commit_stop(cm.exception)
                st = S.read()
                self.assertEqual((st["pause"]["step"], st["strikes"]["lookup"]), ("lookup", 2))
                commands.resume()                                    # Zak
                self.assertEqual(set(S.read()["strikes"].values()), {0})
                self.assertEqual(recover.recover_running(ctx), "finished")
            finally:
                mock.patch.stopall()
            qualify.refusals(ctx, "qualify")                         # not refused by a strike
            qualify.refusals(ctx, "stage")


if __name__ == "__main__":
    unittest.main()
