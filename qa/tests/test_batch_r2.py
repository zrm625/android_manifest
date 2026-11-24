"""Round-2 council findings on the batch runner (Sol SB*, SM*; Gemini GM2-*;
Opus minors), each through its real caller."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from riverbatch import hold, paths, ports
from riverqa import children


def _alive(pid: int) -> bool:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return text[text.rfind(")") + 2:].split()[0] != "Z"


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class SessionEscape(unittest.TestCase):
    """Sol SB2: a descendant in a process group of its own (GNU timeout)
    is still the child's; the holder ends it and the interlock holds until
    it is gone."""

    ESCAPE = ("import os, sys, time\n"
              "os.setpgid(0, 0)\n"
              "open(sys.argv[1], 'w').write(f'{os.getpid()} {os.getpgid(0)} {os.getsid(0)}')\n"
              "time.sleep(600)\n")

    def test_holder_deadline_ends_a_descendant_in_its_own_group(self):
        with bench_home() as h, tempfile.TemporaryDirectory() as t:
            pidf = Path(t) / "pid"
            script = Path(t) / "escape.py"
            script.write_text(self.ESCAPE)
            tools = ports.Tools()
            pid = None
            try:
                r = tools.run(["sh", "-c", f'"{sys.executable}" -I "{script}" "{pidf}" </dev/null >/dev/null 2>&1 & wait'], None,
                              timeout=3)
                self.assertTrue(r.timed_out)
                pid, pgid, sid = map(int, pidf.read_text().split())
                self.assertNotEqual(pgid, sid)                       # it did leave the tool's group
                for _ in range(50):
                    if not _alive(pid):
                        break
                    time.sleep(0.1)
                self.assertFalse(_alive(pid), "a descendant in its own group outlived the holder")
                self.assertEqual(children.live_children(h), [])
            finally:
                if pid:
                    _kill(pid)

    def test_record_stays_live_while_the_session_lives(self):
        """The leader and its group are gone; a process of its session
        is not: the record is live and blocks every other session."""
        with tempfile.TemporaryDirectory() as t:
            proc = Path(t) / "proc"
            (proc / "sys/kernel/random").mkdir(parents=True)
            (proc / "sys/kernel/random/boot_id").write_text("boot\n")
            (proc / "200").mkdir()
            # pid 200: state S, ppid 1, pgrp 200, session 100
            (proc / "200/stat").write_text("200 (timeout) S 1 200 100 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 77 0\n")
            home = Path(t) / "home"
            d = home / "runner" / "children"
            d.mkdir(parents=True)
            (d / "100.json").write_text(json.dumps({"cmd": ["phone-setup"], "pid": 100, "pgid": 100, "sid": 100,
                                                    "boot_id": "boot", "start_time": "5"}))
            self.assertEqual(len(children.blocking(caller_sid=999, home=home, proc=proc)), 1)
            self.assertTrue((d / "100.json").exists())
            (proc / "200/stat").write_text("200 (timeout) Z 1 200 100 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 77 0\n")
            self.assertEqual(children.blocking(caller_sid=999, home=home, proc=proc), [])

    def test_holder_keeps_the_record_while_the_session_survives(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "c"
            saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGALRM)}
            try:
                with mock.patch.object(hold, "GRACE", 0.2), \
                        mock.patch.object(hold, "group_alive", return_value=True), \
                        mock.patch.object(hold, "signal_session"):
                    rc = hold.main([str(d), "--", "true"])
            finally:
                for s, f in saved.items():
                    signal.signal(s, f)
            self.assertEqual(rc, 0)
            self.assertEqual(len(list(d.glob("*.json"))), 1, "the record went while the session was alive")

    def test_phone_setup_adb_stays_in_the_tools_group(self):
        """Through the real phone-setup, run by the real holder: a hung adb
        (a stub on PATH) runs in phone-setup's own group and session, and
        the holder's deadline ends it."""
        with bench_home() as h, tempfile.TemporaryDirectory() as t:
            m = Path(t) / "top/.repo/manifests"
            shutil.copytree(paths.SCRIPTS, m / "scripts", symlinks=True)
            shutil.copytree(paths.SCRIPTS.parent / "qa/lib", m / "qa/lib",
                            ignore=shutil.ignore_patterns("__pycache__"))
            bindir = Path(t) / "bin"
            bindir.mkdir()
            pidf = Path(t) / "adb.pid"
            (bindir / "adb").write_text(
                f"#!{sys.executable} -I\n"
                "import os, time\n"
                f"open({str(pidf)!r}, 'w').write(f'{{os.getpid()}} {{os.getpgid(0)}} {{os.getsid(0)}}')\n"
                "time.sleep(600)\n")
            (bindir / "adb").chmod(0o755)
            pid = None
            try:
                with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ.get('PATH', '')}"}):
                    r = ports.Tools().run([str(m / "scripts/phone-setup")], None, timeout=4)
                self.assertTrue(r.timed_out)
                pid, pgid, sid = map(int, pidf.read_text().split())
                self.assertEqual(pgid, sid, "adb runs in a process group of its own (timeout without --foreground)")
                for _ in range(50):
                    if not _alive(pid):
                        break
                    time.sleep(0.1)
                self.assertFalse(_alive(pid))
                self.assertEqual(children.live_children(h), [])
            finally:
                if pid:
                    _kill(pid)


if __name__ == "__main__":
    unittest.main()


# --- Sol SB1: immutable entry IDs ------------------------------------------------------------------

def _entry(name, pos, status="queued", rev=1, sha="r" * 64, head="1" * 40):
    return {"name": name, "id": f"e{pos}", "position": pos, "status": status, "rev": rev, "batch": None,
            "record_sha256": sha, "kind": "change", "branch": name, "identity": f"zrm625/x#{pos}",
            "prs": [{"key": f"zrm625/x#{pos}", "path": "device/motorola/river", "head": head}],
            "flags": {}, "decisions": [], "records_before": []}


class EntryIdentity(unittest.TestCase):
    def test_withdraw_and_reenqueue_between_composition_and_claim(self):
        """A is composed; then A is withdrawn and B enqueued under the same
        name, at the same rev, before the claim. The claim must not take B
        with A's composition: the run claims B only after composing B."""
        from riverbatch import compose, phases, tick
        from riverbatch import state as S
        from batchfakes import mk_ctx
        with bench_home():
            with S.transaction() as st:
                st["entries"] = [_entry("perch", 1, sha="a" * 64)]
            composed = []
            raced = {"done": False}

            def fake_compose(git, bid, members, base, cams):
                composed.append([(m["id"], m["record_sha256"]) for m in members])
                return compose.Composed(members=list(members), commits={"device/motorola/river": "c" * 40},
                                        trees={"device/motorola/river": "d" * 40})

            def race(git, bid, commits):
                if raced["done"]:
                    return
                raced["done"] = True
                from riverbatch import commands
                commands.withdraw("perch")
                with S.transaction() as st:
                    b = _entry("perch", S.next_position(st), sha="b" * 64)
                    st["entries"].append(b)
            pre = {"base": {"device/motorola/river": "1" * 40, ".repo/manifests": "2" * 40}, "kg": {},
                   "verified": True, "slugs": {}}
            with mock.patch.object(phases, "member_checks", return_value=[]), \
                    mock.patch.object(compose, "compose", fake_compose), \
                    mock.patch.object(compose, "write_refs", race), \
                    mock.patch.object(compose, "manifest_xml", return_value={}), \
                    mock.patch.object(tick.version, "current", return_value={}):
                run = tick.next_batch(mk_ctx(), pre)
            st = S.read()
            self.assertEqual(composed, [[("e1", "a" * 64)], [("e2", "b" * 64)]])
            self.assertEqual([(m["id"], m["record_sha256"]) for m in run["members"]], [("e2", "b" * 64)])
            self.assertEqual([(e["id"], e["status"]) for e in st["entries"]], [("e1", "withdrawn"),
                                                                                ("e2", "in-batch")])

    def test_outcome_never_applies_to_a_new_entry_of_the_same_name(self):
        """Members are matched by ID: a void of the run returns the entry it
        claimed, never another one under the name."""
        from riverbatch import outcomes as O
        from riverbatch import state as S
        st = S.empty()
        a = dict(_entry("perch", 1, status="in-batch", rev=2), batch="b0001")
        b = dict(_entry("perch", 2, status="in-batch", rev=2), batch="b0001", record_sha256="b" * 64)
        st["entries"] = [a, b]
        run = {"id": "b0001", "purpose": "batch", "members": [{"name": "perch", "id": "e1", "rev": 2,
                                                                "record_sha256": "r" * 64}]}
        O.apply(st, run, {"class": O.VOID, "reason": "x"})
        self.assertEqual([e["status"] for e in st["entries"]], ["queued", "in-batch"])

    def test_merge_rule_refuses_a_run_whose_member_record_changed(self):
        from riverbatch import merge, outcomes as O
        from riverbatch import state as S
        with bench_home():
            with S.transaction() as st:
                st["entries"] = [dict(_entry("perch", 1, status="in-batch", rev=2), batch="b0001",
                                      record_sha256="z" * 64)]
            run = {"id": "b0001", "purpose": "batch", "versions": {},
                   "members": [{"name": "perch", "id": "e1", "rev": 2, "record_sha256": "r" * 64}]}
            why = merge.merge_rule(run, {"kind": "batch", "experiment": False}, {"class": O.PASS}, {}, True)
            self.assertTrue(any("approval record" in w for w in why), why)

    def test_old_state_gets_ids_on_read(self):
        from riverbatch import state as S
        with bench_home():
            with S.transaction() as st:
                e = _entry("perch", 3, status="in-batch")
                del e["id"]
                e["batch"] = "b0001"
                st["entries"] = [e]
                st["running"] = {"id": "b0001", "members": [{"name": "perch", "rev": 1}]}
            st = S.read()
            self.assertEqual(st["entries"][0]["id"], "e3")
            self.assertEqual(st["running"]["members"][0]["id"], "e3")
            self.assertEqual(st["running"]["members"][0]["record_sha256"], "r" * 64)


# --- Sol SB3: one reply per provider ---------------------------------------------------------------

class OneReplyPerProvider(unittest.TestCase):
    def enqueue_with(self, mutate):
        from batchfakes import FakeGH, mk_ctx
        from riverbatch import commands, records
        from riverbatch import state as S
        from test_batch_commands import world
        from test_batch_records import make_record
        with bench_home() as h, tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d, delta=False)
            mutate(d, rec["rounds"][0])
            (d / "record.json").write_text(json.dumps(rec))
            gh = FakeGH()
            world(h, gh, rec)
            with self.assertRaises(S.Refused) as cm:
                commands.enqueue(mk_ctx(gh=gh), d / "record.json")
            self.assertEqual(S.read()["entries"], [])
            self.assertFalse((h / "runner/council").exists())
            return str(cm.exception)

    def test_one_file_for_every_provider_is_refused(self):
        def same_file(d, rnd):
            for p in ("gemini", "sol"):
                rnd["replies"][p] = dict(rnd["replies"]["opus"])
        why = self.enqueue_with(same_file)
        self.assertIn("its own file", why)

    def test_identical_content_under_other_names_is_refused(self):
        def same_text(d, rnd):
            text = (d / rnd["replies"]["opus"]["file"]).read_bytes()
            for p in ("gemini", "sol"):
                (d / rnd["replies"][p]["file"]).write_bytes(text)
                rnd["replies"][p]["sha256"] = rnd["replies"]["opus"]["sha256"]
        why = self.enqueue_with(same_text)
        self.assertIn("identical", why)


# --- Gemini GM2-1, GM2-2: qualify ------------------------------------------------------------------

class QualifyExits(unittest.TestCase):
    FULL = None

    def run_cli(self, h, abort=None, outcome=None, argv=("qualify", "--run-only")):
        """`batch qualify --run-only` through cli.main with part 2's phases
        faked; ``abort`` replaces recover.abort."""
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from batchfakes import mk_ctx, write_json
        from riverbatch import cli, kb, merge, phases, qualify, recover
        from riverbatch import ctx as C
        from riverbatch import outcomes as O
        from riverbatch import state as S
        full = {p: "b" * 40 for p in merge.slugs()}
        write_json(h / "slot-state.json", {"known_good_slot": "a", "known_good": {"batch": "kg"}, "pair": None})
        write_json(h / "qa/known-good.json", {"latest": {"batch": "kg", "cameras": [0, 1, 2]}})
        write_json(h / "runner/abtest.json", {"passed": True, "runner_build": "rb", "image_id": "img"})

        def compose_run(ctx, bid, purpose, members, base, kg, pin, verified):
            run = {"id": bid, "purpose": purpose, "touched": [], "base": dict(base), "members": [],
                   "composed": {p: {"sha": v} for p, v in base.items() if p != ".repo/manifests"},
                   "versions": {"image_id": pin}, "phase": "claimed", "outcome": None, "attempt_id": None}
            with S.transaction() as st:
                st["running"] = run
            return run

        def real_abort(ctx):
            with S.transaction() as st:
                st["running"] = None
        names = ["build", "stage_run", "restore_or_void", "flash", "bench", "setup_candidate"]
        ps = [mock.patch.object(phases, n) for n in names] + [
            mock.patch.object(phases, "test", return_value=outcome or {"class": O.PASS}),
            mock.patch.object(phases, "base_verified", return_value=(True, [])),
            mock.patch.object(qualify, "compose_run", compose_run), mock.patch.object(qualify, "commit_phase"),
            mock.patch.object(qualify, "local_base", return_value=full),
            mock.patch.object(qualify, "pin_image", return_value="img"),
            mock.patch.object(qualify.version, "runner_build", return_value="rb"),
            mock.patch.object(qualify.version, "runner_tools", return_value="rt"),
            mock.patch.object(qualify.version, "harness", return_value="h"),
            mock.patch.object(recover, "entry_point"), mock.patch.object(recover, "abort", abort or real_abort),
            mock.patch.object(recover, "setup_known_good"),
            mock.patch.object(recover, "setup_owed", return_value=(False, kb.Check(kb.OK))),
            mock.patch.object(qualify.kb, "check", return_value=kb.Check(kb.OK)),
            mock.patch.object(qualify, "owed_notes", return_value=[]),
            mock.patch.object(C.Locks, "__enter__", lambda self: self),
            mock.patch.object(C.Locks, "__exit__", lambda self, *a: None)]
        for x in ps:
            x.start()
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(list(argv), mk_ctx())
        finally:
            mock.patch.stopall()
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    def test_a_halt_in_the_rollback_exits_3_and_is_committed(self):
        from riverbatch import ctx as C
        from riverbatch import state as S

        def abort(ctx):
            raise C.Halt("flash --rollback halted in q0001")
        with bench_home() as h:
            code, line = self.run_cli(h, abort=abort)
            self.assertEqual(code, 3, line)
            self.assertIn("rollback halted", S.read()["halt"]["reason"])
            self.assertNotIn("qualified", line)

    def test_not_qualified_exits_1(self):
        from riverbatch import outcomes as O
        with bench_home() as h:
            code, line = self.run_cli(h, outcome={"class": O.FAIL, "checks": ["boot"]})
            self.assertEqual((code, line.get("qualified")), (1, False), line)
        with bench_home() as h:
            code, line = self.run_cli(h)
            self.assertEqual((code, line.get("qualified")), (0, True), line)

    def test_qualify_reexecs_after_fast_forwarding_the_manifest(self):
        """Gemini GM2-2: local_base fast-forwards .repo/manifests, then
        re-execs (once) instead of certifying the old code's run."""
        from batchfakes import FakeTree, mk_ctx
        from riverbatch import ctx as C
        from riverbatch import merge, qualify, tc
        full = {p: "b" * 40 for p in merge.slugs()}
        tree = FakeTree(full)
        tree.branches[tc.MANIFEST]["origin/river-22.2"] = "c" * 40
        merged = []
        orig = tree.out

        def out(path, *args, env=None):
            if args[:3] == ("merge-base", "--is-ancestor", "b" * 40):
                return ""
            if args[:3] == ("merge", "-q", "--ff-only"):
                merged.append(path)
                tree.branches[path][tree.head[path]] = "c" * 40
                return ""
            return orig(path, *args, env=env)
        tree.out = out
        ctx = mk_ctx(tree=tree)
        with mock.patch.object(qualify, "remote_git"), mock.patch.dict(os.environ, {"RIVER_BATCH_REEXEC": ""}):
            with self.assertRaises(C.ReExec):
                qualify.local_base(ctx, check_xml=False, kg=None)
        self.assertEqual(merged, [tc.MANIFEST])
        tree.branches[tc.MANIFEST]["default"] = "b" * 40
        with mock.patch.object(qualify, "remote_git"), mock.patch.dict(os.environ, {"RIVER_BATCH_REEXEC": "1"}):
            with self.assertRaises(qualify.Refused):
                qualify.local_base(ctx, check_xml=False, kg=None)


# --- Sol SM3, Gemini GM2-3: refusals and uncertain creates and merges -------------------------------

class PRPhaseRefusals(unittest.TestCase):
    """Through tick.run_batch with the merge path for real (test_batch_mergepath's world)."""

    def run_batch(self, h, gh_tweak):
        import test_batch_mergepath as mp
        from test_batch_flow import steps
        from batchfakes import mk_ctx
        from riverbatch import tick
        from riverbatch import state as S
        gh, git, tools, rgit = mp.world(h)
        gh_tweak(gh)
        flash = tools.script["flash"]

        def flash2(args):
            if args[0] == "--rollback":
                ss = json.loads((h / "slot-state.json").read_text())
                ss["pair"] = None
                (h / "slot-state.json").write_text(json.dumps(ss))
                return 0, ""
            return flash(args)
        tools.script["flash"] = flash2
        ctx = mk_ctx(gh=gh, tools=tools)
        ctx.git = git
        run = S.read()["running"]
        ps = steps() + [mock.patch.object(mp.merge, "remote_git", rgit), mock.patch.object(tick, "remote_git", rgit),
                        mock.patch.object(tick.version, "current", return_value=dict(mp.VERS)),
                        mock.patch.object(mp.recover.version, "current", return_value=dict(mp.VERS)),
                        mock.patch.object(tick, "qualification_problems", return_value=[]),
                        mock.patch("riverqa.bench.attempt_rows", return_value=mp.ROWS)]
        for x in ps:
            x.start()
        try:
            tick.run_batch(ctx, run, {"latest": {"cameras": [0, 1, 2]}})
        finally:
            mock.patch.stopall()
        return gh, tools, S.read()

    def test_refused_pr_create_voids_and_rolls_back_this_tick(self):
        from riverbatch import ctx as C
        with bench_home() as h:
            def tweak(gh):
                gh.fail["create_pr"] = C.Refused("pr create", "x", 422, "Validation Failed")
            gh, tools, st = self.run_batch(h, tweak)
            self.assertIn(("flash", ("--rollback",), "b0001"), tools.calls)
            self.assertIsNone(st["running"])
            self.assertEqual(st["entries"][0]["status"], "queued")
            self.assertIn("refused", st["entries"][0]["reason"])
            self.assertIsNone(st["last_error"])

    def test_pr_view_404_voids_and_rolls_back_this_tick(self):
        from riverbatch import ctx as C
        with bench_home() as h:
            def tweak(gh):
                gh.fail["pr_view"] = [C.Refused("pr view", "x", 404, "Could not resolve to a PullRequest")]
            gh, tools, st = self.run_batch(h, tweak)
            self.assertIn(("flash", ("--rollback",), "b0001"), tools.calls)
            self.assertIsNone(st["running"])
            self.assertEqual(st["entries"][0]["status"], "queued")
            self.assertIn("refused the read", st["entries"][0]["reason"])

    def test_any_refusal_before_merge_started_voids_and_rolls_back_this_tick(self):
        """A refused GitHub call no step handles (a PR list 404) is the
        batch's void, never a runner error."""
        from riverbatch import ctx as C
        with bench_home() as h:
            def tweak(gh):
                gh.fail["pr_list"] = C.Refused("pr list", "x", 404, "Not Found")
            gh, tools, st = self.run_batch(h, tweak)
            self.assertIn(("flash", ("--rollback",), "b0001"), tools.calls)
            self.assertIsNone(st["running"])
            self.assertEqual(st["entries"][0]["status"], "queued")
            self.assertIn("GitHub refused", st["entries"][0]["reason"])

    def test_lost_create_answer_finds_the_pr_it_made(self):
        """The create reached GitHub, its answer did not: the PR is looked
        up and the batch goes on to merge."""
        from riverbatch import ctx as C
        with bench_home() as h:
            def tweak(gh):
                orig = gh.create_pr

                def create(repo, head, title, body):
                    orig(repo, head, title, body)
                    raise C.Unknown("pr create", repo, None, "connection reset")
                gh.create_pr = create
            gh, tools, st = self.run_batch(h, tweak)
            self.assertIsNone(st["running"])
            self.assertEqual(st["entries"][0]["status"], "merged")
            self.assertNotIn(("flash", ("--rollback",), "b0001"), tools.calls)


class MergeLag(unittest.TestCase):
    def setup(self, h):
        from batchfakes import write_json
        from test_batch_merge import setup_batch
        setup_batch(h)
        write_json(h / "slot-state.json", {"pair": {"batch": "b0001", "attempts": ["fa-1"]}})

    def test_already_merged_refusal_is_reconciled(self):
        """The first merge landed but its answer was lost; GitHub refuses
        the second: reconciliation finds it landed; no HALT."""
        from riverbatch import ctx as C, merge
        from riverbatch import state as S
        from test_batch_merge import M, P, R, gh_with
        from batchfakes import mk_ctx
        with bench_home() as h:
            self.setup(h)
            gh = gh_with()
            calls = {"n": 0}

            def merge_pr(repo, n, sha):
                calls["n"] += 1
                gh.prs[(R, 7)].update(state="MERGED", mergeCommit={"oid": M})
                gh.tips[(R, "refs/heads/river-22.2")] = M
                raise C.Refused("merge", repo, 405, "Pull Request is not mergeable")
            gh.merge_pr = merge_pr
            merge.merge_all(mk_ctx(gh=gh), S.read()["running"])
            self.assertEqual(S.read()["running"]["merge"]["merged"], {P: M})

    def test_acknowledged_merge_with_lagging_reads_is_never_sent_again(self):
        from riverbatch import ctx as C, merge
        from riverbatch import state as S
        from test_batch_merge import M, P, R, gh_with
        from batchfakes import mk_ctx
        with bench_home() as h:
            self.setup(h)
            gh = gh_with()
            gh.merge_answer = M                       # answered merged; reads still show OPEN at the base
            ctx = mk_ctx(gh=gh)
            with self.assertRaises(C.Unknown):
                merge.merge_all(ctx, S.read()["running"])
            self.assertEqual(S.read()["running"]["merge"]["acked"], {P: M})
            with self.assertRaises(C.Unknown):        # next tick: still lagging; no second merge
                merge.merge_all(ctx, S.read()["running"])
            self.assertEqual(sum(1 for c in gh.calls if c[0] == "merge_pr"), 1)
            self.assertIsNotNone(S.read()["running"]["reconcile_unknown_since"])
            gh.prs[(R, 7)].update(state="MERGED", mergeCommit={"oid": M})
            gh.tips[(R, "refs/heads/river-22.2")] = M
            merge.merge_all(ctx, S.read()["running"])
            self.assertEqual(S.read()["running"]["merge"]["merged"], {P: M})
            self.assertEqual(sum(1 for c in gh.calls if c[0] == "merge_pr"), 1)

    def test_refusal_after_an_unanswered_merge_is_unknown_not_halt(self):
        from riverbatch import ctx as C, merge
        from riverbatch import state as S
        from test_batch_merge import gh_with
        from batchfakes import mk_ctx
        with bench_home() as h:
            self.setup(h)
            gh = gh_with()
            gh.fail["merge_pr"] = [C.Unknown("merge", "x", None, "timeout"),
                                   C.Refused("merge", "x", 405, "Base branch was modified")]
            ctx = mk_ctx(gh=gh)
            with self.assertRaises(C.Unknown):
                merge.merge_all(ctx, S.read()["running"])
            with self.assertRaises(C.Unknown):
                merge.merge_all(ctx, S.read()["running"])

    def test_answered_sha_that_is_not_the_landed_tip_halts(self):
        from riverbatch import ctx as C, merge
        from riverbatch import state as S
        from test_batch_merge import M, OTHER, R, gh_with
        from batchfakes import mk_ctx
        with bench_home() as h:
            self.setup(h)
            gh = gh_with()

            def effect(repo, n, sha):
                gh.prs[(R, 7)].update(state="MERGED", mergeCommit={"oid": M})
                gh.tips[(R, "refs/heads/river-22.2")] = M
            gh.merge_effect = effect
            gh.merge_answer = OTHER
            with self.assertRaises(C.Halt):
                merge.merge_all(mk_ctx(gh=gh), S.read()["running"])


class ForwardRefusals(unittest.TestCase):
    def test_delete_branch_with_a_stale_lease_reads_again(self):
        from batchfakes import FakeGH, mk_ctx
        from riverbatch import ctx as C, merge
        gh = FakeGH()
        R = merge.slugs()["device/motorola/river"]
        gh.tips[(R, "refs/heads/batch/b0001")] = "a" * 40

        def push(ctx, path, *args):
            gh.tips.pop((R, "refs/heads/batch/b0001"))       # deleted meanwhile
            raise C.Refused("git push", path, None, "! [rejected] (stale info)")
        with mock.patch.object(merge, "remote_git", push):
            self.assertEqual(merge.delete_branch(mk_ctx(gh=gh), "device/motorola/river", R, "b0001", "a" * 40),
                             "absent")

    def test_refused_tag_push_reads_again(self):
        import test_batch_mergepath as mp
        from batchfakes import mk_ctx
        from riverbatch import ctx as C, merge
        from riverbatch import state as S
        with bench_home() as h:
            gh, git, tools, rgit = mp.world(h)
            b = json.loads((h / "batches/b0001/batch.json").read_text())
            b.update(tag="known-good-b0001", merge_started={"expected_prs": {}})
            (h / "batches/b0001/batch.json").write_text(json.dumps(b))
            want = {p: s for p, s in b["commit_set"].items()}
            sl = merge.slugs()
            state = {"other": False}

            def push(ctx, path, *args):
                gh.tips[(sl[path], "refs/tags/known-good-b0001^{}")] = "e" * 40 if state["other"] else want[path]
                raise C.Refused("git push", path, None, "! [rejected] (already exists)")
            ctx = mk_ctx(gh=gh)
            ctx.git = git
            with mock.patch.object(merge, "remote_git", push):
                merge.push_tags(ctx, S.read()["running"])          # every tag is where it should be
                for p in sl:
                    gh.tips.pop((sl[p], "refs/tags/known-good-b0001^{}"))
                state["other"] = True
                with self.assertRaises(C.Halt):
                    merge.push_tags(ctx, S.read()["running"])

    def test_refused_fetch_of_a_member_head_needs_reapproval(self):
        from batchfakes import FakeGH, mk_ctx
        from riverbatch import ctx as C, eligibility, phases
        from riverbatch import state as S
        with bench_home():
            with S.transaction() as st:
                st["entries"] = [_entry("perch", 1)]
            e = S.read()["entries"][0]

            def fetch(ctx, path, *args):
                raise C.Refused("git fetch", path, None, "fatal: couldn't find remote ref refs/pull/1/head")
            with mock.patch.object(eligibility, "eligible", return_value=eligibility.Outcome(eligibility.OK, None)), \
                    mock.patch.object(phases, "entry_record", return_value=({}, [])), \
                    mock.patch("riverbatch.ctx.remote_git", fetch):
                bad = phases.member_checks(mk_ctx(gh=FakeGH()), [e], {}, {}, {})
            self.assertEqual([(x["name"], s) for x, s, _ in bad], [("perch", eligibility.REAPPROVE)])
            self.assertEqual(S.read()["entries"][0]["status"], "needs-reapproval")


class ForwardRefusalHalts(unittest.TestCase):
    def test_a_refusal_on_the_forward_path_is_a_halt_not_a_runner_error(self):
        from batchfakes import mk_ctx, write_json
        from riverbatch import ctx as C, recover
        from riverbatch import state as S
        with bench_home() as h:
            write_json(h / "batches/b0001/batch.json", {"merge_started": {"at": "t"}})
            with S.transaction() as st:
                st["running"] = {"id": "b0001", "purpose": "batch", "members": []}
            with mock.patch.object(recover, "forward", side_effect=C.Refused("pr close", "x", 403, "locked")):
                with self.assertRaises(C.Halt):
                    recover.recover_running(mk_ctx())


# --- Sol SM1, Gemini GM2-4: settle holds while landed or ambiguous ----------------------------------

class SettleHolds(unittest.TestCase):
    """settle.settle, then recover.cleanup_items, in test_batch_settle's world."""

    def settle_then_cleanup(self, gh, clear_rebaseline=False):
        from test_batch_settle import PartialSettle
        from riverbatch import state as S
        t = PartialSettle()
        out = t.settle(gh)
        if clear_rebaseline:
            with S.transaction() as st:
                st["rebaseline"] = None
        return out, t.cleanup(gh)

    def test_landed_but_open_is_never_closed_and_its_branch_kept(self):
        """GitHub lag: vendor/lineage #8's tested head is in river-22.2 but
        the PR still reads OPEN. Even after the re-baseline, it is expected,
        never closed, and its branch stays until GitHub shows it MERGED."""
        import test_batch_settle as ts
        from riverbatch import state as S
        with bench_home() as h:
            gh = ts.world(h)
            gh.raw[("gh", "api", f"repos/{ts.VENDOR}/compare/river-22.2...{ts.T_V}", "--jq", ".status")] = "behind"
            out, deleted = self.settle_then_cleanup(gh, clear_rebaseline=True)
            (item,) = S.read()["cleanup"]
            self.assertEqual(item["landed_open"], {ts.PV: {"repo": ts.VENDOR, "number": 8}})
            self.assertEqual(gh.prs[(ts.VENDOR, 8)]["state"], "OPEN")
            self.assertIn((ts.VENDOR, "refs/heads/batch/b0001"), gh.tips)
            self.assertNotIn(ts.PV, [p for p, _ in deleted])
            self.assertIsNone(S.read()["pause"])
            gh.prs[(ts.VENDOR, 8)].update(state="MERGED", mergeCommit={"oid": "8" * 40})
            from test_batch_settle import PartialSettle
            PartialSettle().cleanup(gh)
            self.assertEqual(S.read()["cleanup"], [])
            self.assertIsNone(S.read()["pause"])

    def test_rebaseline_owed_without_a_merged_pr_still_holds(self):
        """Gemini GM2-4: nothing reads MERGED (I5 lag past 24 h, or drift),
        yet a re-baseline is owed: the open PRs and branches are held."""
        import test_batch_settle as ts
        from riverbatch import state as S
        with bench_home() as h:
            gh = ts.world(h)
            gh.prs[(ts.RIVER, 7)].update(state="OPEN", mergeCommit=None)          # still reads OPEN
            gh.raw[("gh", "api", f"repos/{ts.RIVER}/compare/river-22.2...{ts.T_R}", "--jq", ".status")] = "behind"
            out, deleted = self.settle_then_cleanup(gh)
            self.assertTrue(out["rebaseline"])
            (item,) = S.read()["cleanup"]
            self.assertTrue(item["hold"])
            self.assertEqual(deleted, [])
            self.assertEqual((gh.prs[(ts.RIVER, 7)]["state"], gh.prs[(ts.VENDOR, 8)]["state"]), ("OPEN", "OPEN"))

    def test_a_held_pr_merged_at_another_head_is_not_accepted(self):
        import test_batch_settle as ts
        from riverbatch import ctx as C
        from riverbatch import state as S
        with bench_home() as h:
            gh = ts.world(h)
            self.settle_then_cleanup(gh)
            gh.prs[(ts.VENDOR, 8)].update(state="MERGED", headRefOid="5" * 40, mergeCommit={"oid": "8" * 40})
            with S.transaction() as st:
                st["rebaseline"] = None
            from test_batch_settle import PartialSettle
            with self.assertRaises(C.Pause):
                PartialSettle().cleanup(gh)
