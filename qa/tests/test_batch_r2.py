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
