"""Harness changes for the batch runner (qa/RUNNER.md section 17): H1, H2,
H3, H6, H10-H18 and I3, I4, I16. No phone: fakes and a throwaway
$BENCH_HOME only."""
import io
import json
import os
import subprocess
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from riverqa import bench, children, core, features, flashing, lock, qa, recovery, regression, reboots, \
    runnerstate, slots, stage

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def child_record(home, pid, pgid, boot_id=None, start=None, name=None):
    d = home / "runner" / "children"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"cmd": ["x"], "pid": pid, "pgid": pgid,
           "boot_id": boot_id or core.host_boot_id(),
           "start_time": start if start is not None else children.start_time(pid)}
    (d / (name or f"{pid}.json")).write_text(json.dumps(rec))
    return rec


class Children(unittest.TestCase):
    """H15: live child records."""

    def test_live_record_of_this_process_counts(self):
        with bench_home() as h:
            child_record(h, os.getpid(), os.getpgid(0))
            self.assertEqual(len(children.live_children(h)), 1)

    def test_other_boot_and_dead_pid_are_pruned(self):
        with bench_home() as h:
            child_record(h, os.getpid(), os.getpgid(0), boot_id="another-boot")
            p = subprocess.Popen(["true"])
            p.wait()
            child_record(h, p.pid, 999999999, start="1")
            self.assertEqual(children.live_children(h), [])
            self.assertEqual(list((h / "runner/children").iterdir()), [])

    def test_reused_pid_with_live_group_counts(self):
        with bench_home() as h:
            child_record(h, os.getpid(), os.getpgid(0), start="0")   # wrong start time, live group
            self.assertEqual(len(children.live_children(h)), 1)

    def test_unreadable_counts_and_tmp_never_counts(self):
        with bench_home() as h:
            d = h / "runner/children"
            d.mkdir(parents=True)
            (d / ".123.tmp").write_text("{")
            self.assertEqual(children.live_children(h), [])
            (d / "123.json").write_text("{")
            live = children.live_children(h)
            self.assertEqual(len(live), 1)
            self.assertIn("unreadable", live[0]["error"])

    def test_blocking_ignores_own_session(self):
        with bench_home() as h:
            child_record(h, os.getpid(), os.getpgid(0))
            self.assertEqual(children.blocking(os.getpgid(0), h), [])
            self.assertEqual(len(children.blocking(-1, h)), 1)


class Locks(unittest.TestCase):
    """H2, H15, H1 (Python side) and R6.3: the orphan check runs after
    flock and before a token is written; a runner-token build lock is no
    build."""

    def test_device_lock_refuses_orphan_after_flock_without_writing(self):
        with bench_home() as h:
            (h / ".device-lock").write_text("old\n")
            child_record(h, os.getpid(), -5)          # pgid differs from our session
            with self.assertRaises(lock.LockError):
                lock.DeviceLock().acquire()
            self.assertEqual((h / ".device-lock").read_text(), "old\n")
            self.assertFalse(lock._held_by_someone())  # released

    def test_orphan_check_runs_after_flock(self):
        order = []
        real_flock = lock.fcntl.flock

        def flock(fd, op):
            if op & lock.fcntl.LOCK_EX:
                order.append("flock")
            return real_flock(fd, op)
        with bench_home():
            with mock.patch.object(lock.fcntl, "flock", flock), \
                    mock.patch.object(lock, "_refuse_orphans", lambda: order.append("orphans")):
                lock.DeviceLock(check_build=False).acquire().release()
                lock.BuildLock(export=False).acquire().release()
        self.assertEqual(order, ["flock", "orphans", "flock", "orphans"])

    def test_build_lock_with_runner_token_is_no_build(self):
        with bench_home():
            bl = lock.BuildLock(export=False).acquire()
            try:
                self.assertTrue(lock.build_running())
                with mock.patch.dict(os.environ, {"BENCH_BUILD_LOCK_TOKEN": bl.token}):
                    self.assertFalse(lock.build_running())
                    child = lock.BuildLock().acquire()        # token reuse
                    self.assertFalse(child.owned)
                with mock.patch.dict(os.environ, {"BENCH_BUILD_LOCK_TOKEN": "wrong"}):
                    self.assertTrue(lock.build_running())
                    with self.assertRaises(lock.LockError):
                        lock.BuildLock().acquire()
            finally:
                bl.release()
            self.assertFalse(lock.build_running())

    def shell(self, home, body):
        """Run ``body`` after sourcing a copy of scripts/common.sh placed in
        a scratch repo layout (common.sh finds .repo above itself)."""
        env = dict(os.environ, BENCH_HOME=str(home))
        env.pop("BENCH_LOCK_TOKEN", None)
        env.pop("BENCH_BUILD_LOCK_TOKEN", None)
        top = Path(tempfile.mkdtemp(dir=home))
        m = top / ".repo/manifests"
        (m / "scripts").mkdir(parents=True)
        (m / "scripts/common.sh").write_text((SCRIPTS / "common.sh").read_text())
        (m / "qa").mkdir()
        (m / "qa/lib").symlink_to(SCRIPTS.parent / "qa/lib")
        script = f'source "{m}/scripts/common.sh"\n{body.replace("COMMON", str(m / "scripts/common.sh"))}'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env)

    def test_shell_build_lock_orphan_after_flock_writes_nothing(self):
        with bench_home() as h:
            (h / ".lock").write_text("old-token\n")
            child_record(h, os.getpid(), -5)
            r = self.shell(h, "acquire_build_lock; echo got")
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("got", r.stdout)
            self.assertIn("batch-runner child", r.stderr)
            self.assertEqual((h / ".lock").read_text(), "old-token\n")
            # and the lock was released
            self.assertEqual(subprocess.run(["flock", "-n", str(h / ".lock"), "true"]).returncode, 0)

    def test_shell_build_lock_writes_token_and_reuses_it(self):
        with bench_home() as h:
            r = self.shell(h, 'acquire_build_lock; t=$BENCH_BUILD_LOCK_TOKEN; '
                              '[[ "$(cat "$BENCH_HOME/.lock")" == "$t" ]] && echo token-ok; '
                              "bash -c 'source \"$1\"; acquire_build_lock && echo reused' x COMMON; "
                              'build_running && echo running || echo no-build')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("token-ok", r.stdout)
            self.assertIn("reused", r.stdout)
            self.assertIn("no-build", r.stdout)        # H2: our own token is no build

    def test_shell_device_lock_refuses_during_a_build(self):
        with bench_home() as h:
            with lock.BuildLock(export=False):
                r = self.shell(h, "device_lock; echo got")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("a build holds", r.stderr)


class Qualified(unittest.TestCase):
    """H3: phone-qa qualified, host-only."""

    def test_missing_everything(self):
        with bench_home(), redirect_stdout(io.StringIO()) as out:
            rc = qa.main(["qualified"])
        doc = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(rc, 1)
        self.assertFalse(doc["qualified"])
        self.assertTrue(any("known-good" in m for m in doc["missing"]))
        self.assertTrue(any("closed baseline" in m for m in doc["missing"]))

    def test_qualified(self):
        with bench_home() as h:
            v = "v1"
            core.write_json(h / "qa/known-good.json", {"latest": {"batch": "x"}, "ram_reference": {"median_mb": 1},
                                                        "calibration_seed": {"batch": "x"}})
            st = slots.first_run("a", "f" * 64, "t")
            st["baseline_closed"] = {"batch": "x", "seed_written": True}
            flashing.save_state(st)
            with mock.patch.object(qa, "harness_version", lambda: v), \
                    mock.patch.object(qa, "calibration_runs", lambda ver: 5), \
                    mock.patch.object(qa, "controls_status", lambda ver: (True, "")), \
                    mock.patch.object(qa, "read_json", side_effect=lambda p, d=None: (
                        {"calibrated": True} if p == qa.THRESHOLDS_JSON else core.read_json(p, d))), \
                    redirect_stdout(io.StringIO()) as out:
                rc = qa.main(["qualified"])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue()), {"qualified": True, "harness_version": v, "missing": []})

    def test_checkpoint_pending_is_not_qualified(self):
        with bench_home():
            st = slots.first_run("a", "f" * 64, "t")
            st["baseline_closed"] = {"batch": "x", "seed": {"a": 1}, "seed_written": False}
            flashing.save_state(st)
            self.assertTrue(any("checkpoint" in m for m in qa.qualification_missing("v")))


class H6Line(unittest.TestCase):
    def test_last_stdout_line(self):
        with bench_home() as h:
            run = types.SimpleNamespace(results={}, wanted=lambda c: True, mode="batch", run_id="qa-1",
                                        batch_id="b1", version="v", started=0, only=[], skip=[], halted=None,
                                        dir=h / "qa/runs/qa-1", attempt_id=lambda: "f1", batch={})
            with mock.patch("riverqa.report.write_html"), redirect_stdout(io.StringIO()) as out:
                code = qa.finish(run, [])
        last = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(set(last), {"run_id", "results", "exit_code", "eligible", "halted"})
        self.assertEqual((last["run_id"], last["exit_code"], last["halted"]), ("qa-1", code, None))


class JournalRestore(unittest.TestCase):
    """H10: phone-qa journal --restore exit codes; guard before root."""

    def run_restore(self, *, pending=None, guard_ok=True, left=(), recover_raises=None):
        calls = []

        class Dev:
            def guard(self):
                calls.append("guard")
                if not guard_ok:
                    raise qa.GuardError("not river")

            def root(self):
                calls.append("root")

        class FakeRun:
            def __init__(self, *a):
                self.dev = Dev()
                self.journal = types.SimpleNamespace(path=Path("/nonexistent"), qa_home=Path("/nonexistent"))

        pend = list(pending or [])

        def fake_pending(j=None):
            return pend[0] if pend else None

        def fake_recover(dev, t, boot=True, j=None):
            calls.append("fb_recover")
            if recover_raises:
                raise recover_raises
            pend.clear()
            return types.SimpleNamespace(what="cleared")

        def fake_recover_journal(run):
            calls.append("recover_journal")
            return ["x"], [], []

        with bench_home(), mock.patch.object(qa, "Run", FakeRun), \
                mock.patch("riverqa.fbmode.pending", fake_pending), \
                mock.patch("riverqa.fbmode.recover", fake_recover), \
                mock.patch("riverqa.restore.recover_journal", fake_recover_journal), \
                mock.patch.object(qa.jmod, "Journal", lambda *a: types.SimpleNamespace(entries=list(left), path=a[0],
                                                                                       qa_home=a[1])), \
                redirect_stderr(io.StringIO()):
            rc = qa.main(["journal", "--restore"])
        return rc, calls

    def test_empty_after_restore(self):
        rc, calls = self.run_restore()
        self.assertEqual(rc, 0)
        self.assertLess(calls.index("guard"), calls.index("root"))

    def test_left_over_is_1(self):
        self.assertEqual(self.run_restore(left=[{"kind": "setting"}])[0], 1)

    def test_guard_failure_is_3_and_never_roots(self):
        rc, calls = self.run_restore(guard_ok=False)
        self.assertEqual(rc, 3)
        self.assertNotIn("root", calls)

    def test_fb_mode_failure_is_3(self):
        rc, calls = self.run_restore(pending=[{"kind": "fb_mode"}], recover_raises=RuntimeError("x"))
        self.assertEqual(rc, 3)
        self.assertNotIn("recover_journal", calls)


class TypedLookup(unittest.TestCase):
    """H11: each class of lookup outcome."""

    BATCH = {"merge_started": {"expected_prs": {"a": {"repo": "zrm625/a", "number": 1}}}, "tag": "t",
             "tag_repos": [], "commit_set": {"a": "1" * 40}}

    def look(self, rc, out="", err=""):
        return recovery.lookup(self.BATCH, lambda cmd: subprocess.CompletedProcess(cmd, rc, out, err), {})

    def test_classes(self):
        cases = [
            ((1, "", "HTTP 502: Bad Gateway"), recovery.TRANSPORT),
            ((1, "", "gh: API rate limit exceeded (HTTP 403)"), recovery.TRANSPORT),
            ((1, "", "HTTP 429"), recovery.TRANSPORT),
            ((1, "", "error connecting to api.github.com"), recovery.TRANSPORT),
            ((1, "", "GraphQL: Something went wrong while executing your query"), recovery.TRANSPORT),
            ((1, "", "HTTP 401: Bad credentials"), recovery.REFUSED),
            ((1, "", "gh: Resource not accessible by integration (HTTP 403)"), recovery.REFUSED),
            ((1, "", "gh: Not Found (HTTP 404)"), recovery.REFUSED),
            ((1, "", "HTTP 422: Validation Failed"), recovery.REFUSED),
            ((1, "", "GraphQL: Could not resolve to a PullRequest with the number of 1."), recovery.REFUSED),
            ((0, "<html>", ""), recovery.MALFORMED),
            ((0, json.dumps({"number": 1}), ""), recovery.MALFORMED),
        ]
        for args, want in cases:
            got = self.look(*args)
            self.assertEqual(got.outcome, want, args)
            self.assertIsNone(got.data)
            self.assertEqual(got.evidence["repo"], "zrm625/a")
        self.assertEqual(self.look(1, "", "HTTP 404").evidence["status"], 404)

    def test_timeout_is_transport(self):
        def boom(cmd):
            raise TimeoutError("gh: timed out")
        self.assertEqual(recovery.lookup(self.BATCH, boom, {}).outcome, recovery.TRANSPORT)

    def test_ok(self):
        out = json.dumps({"number": 1, "state": "OPEN", "headRefOid": "1" * 40, "baseRefName": "river-22.2"})
        got = self.look(0, out)
        self.assertEqual(got.outcome, recovery.OK)
        self.assertEqual(got.data["prs"][0]["state"], "OPEN")

    def test_classify_auth(self):
        self.assertEqual(recovery.classify(1, "", "HTTP 401: Bad credentials")[0], recovery.AUTH)
        self.assertEqual(recovery.classify(1, "", "gh: Forbidden (HTTP 403)")[0], recovery.AUTH)
        self.assertEqual(recovery.classify(1, "", "HTTP 403 x-ratelimit-remaining: 0")[0], recovery.TRANSPORT)


from test_promote import BASES, COMMITS, MERGES, SLUGS, TREES, phone, setup, world  # noqa: E402


def promote_with(runner):
    err, out = io.StringIO(), io.StringIO()
    with mock.patch.object(flashing, "Device", lambda *a: phone()), \
            mock.patch.object(recovery, "default_runner", runner), \
            mock.patch.object(stage, "expected_repos", lambda manifest=None: set(COMMITS)), \
            mock.patch.object(stage, "repo_slugs", lambda manifest=None: dict(SLUGS)), \
            redirect_stderr(err), redirect_stdout(out):
        rc = flashing.main(["--promote", "b1"])
    return rc, err.getvalue(), out.getvalue()


class PromoteTyped(unittest.TestCase):
    """H11 and I4 through flash --promote."""

    def test_transport_is_75_and_touches_nothing(self):
        with bench_home() as h:
            setup(h)
            rc, err, _ = promote_with(lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "HTTP 503"))
            self.assertEqual(rc, 75, err)
            self.assertEqual(flashing.load_state()["pair"]["batch"], "b1")

    def test_refused_is_1_with_the_lookup_line(self):
        for err_text, want in (("HTTP 401: Bad credentials", "refused"), ("gh: Not Found (HTTP 404)", "refused")):
            with bench_home() as h:
                setup(h)
                rc, _, out = promote_with(lambda cmd: subprocess.CompletedProcess(cmd, 1, "", err_text))
                line = json.loads(out.strip().splitlines()[-1])
                self.assertEqual(rc, 1)
                self.assertEqual(line["lookup"], want)
                self.assertEqual(line["repo"], "zrm625/r0")
                self.assertIn(line["evidence"]["status"], (401, 404))

    def test_malformed_is_1_with_the_lookup_line(self):
        with bench_home() as h:
            setup(h)
            rc, _, out = promote_with(lambda cmd: subprocess.CompletedProcess(cmd, 0, "not json", ""))
            self.assertEqual(rc, 1)
            self.assertEqual(json.loads(out.strip().splitlines()[-1])["lookup"], "malformed")

    def lag(self, tagged=(True, True, True)):
        # r0 merged; r1 landed on river-22.2 ([base, head], tested tree) but
        # GitHub still reports its PR OPEN (I4).
        w = world([True, False], tagged)
        w.merge("zrm625/r1", 1, "7" * 40, MERGES[1], TREES["zrm625/r1"])
        w.prs[("zrm625/r1", 1)].update(state="OPEN", mergeCommit=None)
        if tagged[1]:
            w.repos["zrm625/r1"]["tags"]["kg-1"] = MERGES[1]
        return w

    def test_pr_still_open_with_its_merge_tagged_is_75(self):
        with bench_home() as h:
            setup(h)
            rc, err, _ = promote_with(self.lag())
            self.assertEqual(rc, 75, err)
            self.assertIn("still reports zrm625/r1#1 open", err)
            self.assertEqual(flashing.load_state()["pair"]["batch"], "b1")

    def test_open_at_landed_tip_before_any_tag_is_75(self):
        # Sol B2: the runner tags only after it saw MERGED, so a lag before
        # the first tag must also wait (75), never halt.
        with bench_home() as h:
            setup(h)
            rc, err, _ = promote_with(self.lag(tagged=(False, False, False)))
            self.assertEqual(rc, 75, err)
            self.assertEqual(flashing.load_state()["pair"]["batch"], "b1")
            with mock.patch.object(stage, "repo_slugs", lambda manifest=None: dict(SLUGS)), \
                    redirect_stderr(io.StringIO()):
                rc = flashing.recover(runner=self.lag(tagged=(False, False, False)), dev=phone())
            self.assertEqual(rc, 75)                                  # flash --recover: the same rule
            self.assertEqual(flashing.load_state()["pair"]["batch"], "b1")

    def test_lag_next_to_another_mismatch_halts(self):
        # Opus minor: one PR lags and another is wrong: not a lag, a HALT.
        with bench_home() as h:
            setup(h)
            w = self.lag(tagged=(False, False, False))
            w.prs[("zrm625/r0", 0)]["isCrossRepository"] = True
            rc, err, _ = promote_with(w)
            self.assertEqual(rc, flashing.EXIT_HALT, err)

    def test_a_pending_pr_is_not_a_lag(self):
        with bench_home() as h:
            setup(h)
            rc, err, _ = promote_with(world([True, False], (False, False, False)))   # r1 still at its base
            self.assertEqual(rc, flashing.EXIT_HALT, err)

    def test_failed_tip_read_is_75(self):
        with bench_home() as h:
            setup(h)
            w = self.lag()
            w.fail = lambda cmd: (subprocess.CompletedProcess(cmd, 1, "", "HTTP 502")
                                  if cmd[:2] == ["gh", "api"] and "parents" in cmd[-1] else None)
            rc, err, _ = promote_with(w)
            self.assertEqual(rc, 75, err)


class PromoteProof(unittest.TestCase):
    """Sol B2: flash --promote accepts exactly the runner's landed proof."""

    def promote(self, w, **kw):
        with bench_home() as h:
            setup(h, **kw)
            rc, err, _ = promote_with(w)
            return rc, err, flashing.load_state(), core.read_json(core.batch_dir("b1") / "batch.json")

    def test_clean_merge_promotes_and_records_the_merge_commits(self):
        rc, err, st, b = self.promote(world([True, True], (True, True, True)))
        self.assertEqual(rc, 0, err)
        self.assertEqual(st["known_good_slot"], "b")
        # I11: the proven merge commits, durable and local
        self.assertEqual(b["promotion"]["merge_commits"], {"build/make": MERGES[0], "vendor/lineage": MERGES[1]})

    def test_cross_repository_merged_pr_is_never_promoted(self):
        rc, err, st, _ = self.promote(world([True, True], (True, True, True), cross=(False, True)))
        self.assertEqual(rc, flashing.EXIT_HALT, err)
        self.assertIn("isCrossRepository", err)
        self.assertEqual(st["known_good_slot"], "a")

    def test_river_22_2_moved_past_the_merge_halts(self):
        w = world([True, True], (True, True, True))
        w.repos["zrm625/r1"]["tip"] = "c" * 40                     # someone merged on top
        w.commits[("zrm625/r1", "c" * 40)] = {"tree": "d" * 40, "parents": [MERGES[1]]}
        rc, err, st, _ = self.promote(w)
        self.assertEqual(rc, flashing.EXIT_HALT, err)
        self.assertIn("not the merge commit", err)

    def test_merge_parents_must_be_the_frozen_base_and_the_tested_head(self):
        b0 = {"build/make": BASES[0], "vendor/lineage": BASES[1], ".repo/manifests": COMMITS[".repo/manifests"]}
        rc, err, _, _ = self.promote(world([True, True], (True, True, True)), base=b0)
        self.assertEqual(rc, 0, err)                                  # parents [frozen base, tested]
        other = dict(b0, **{"vendor/lineage": "e" * 40})
        rc, err, st, _ = self.promote(world([True, True], (True, True, True)), base=other)
        self.assertEqual(rc, flashing.EXIT_HALT, err)
        self.assertIn("parents", err)

    def test_repo_without_a_pr_must_still_be_at_its_tested_commit(self):
        w = world([True, True], (True, True, True))
        w.repos["zrm625/r2"]["tip"] = "c" * 40
        rc, err, _, _ = self.promote(w)
        self.assertEqual(rc, flashing.EXIT_HALT, err)


class H12(unittest.TestCase):
    """flash --rollback/--recover refuse on the running batch's pair."""

    def prep(self, h, running="b1", state_text=None):
        st = slots.first_run("a", "f" * 64, "t")
        st["baseline_closed"] = {"batch": "x", "seed_written": True}
        slots.reserve(st, "b1", "batch", "f" * 64, "t")
        flashing.save_state(st)
        (h / "runner").mkdir(parents=True, exist_ok=True)
        (h / "runner/state.json").write_text(state_text if state_text is not None else
                                             json.dumps({"running": {"id": running} if running else None}))

    def call(self, argv, env=None):
        with mock.patch.dict(os.environ, env or {}), redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(flashing, "rollback", lambda *a, **k: {"ok": 1}), \
                mock.patch.object(flashing, "recover", lambda *a, **k: 0), \
                mock.patch.object(flashing, "Device", lambda *a: types.SimpleNamespace(state=lambda: "")):
            return flashing.main(argv), err.getvalue()

    def test_refused_for_the_running_batch(self):
        for argv in (["--rollback"], ["--recover"]):
            with bench_home() as h:
                self.prep(h)
                rc, err = self.call(argv)
                self.assertEqual(rc, 1, argv)
                self.assertIn("belongs to the running batch b1", err)

    def test_zak_overrides_and_other_batches_pass(self):
        with bench_home() as h:
            self.prep(h)
            self.assertEqual(self.call(["--rollback", "--zak"])[0], 0)
        with bench_home() as h:
            self.prep(h, running="b2")
            self.assertEqual(self.call(["--rollback"])[0], 0)

    def test_unparsable_state_refuses(self):
        with bench_home() as h:
            self.prep(h, state_text="{")
            rc, err = self.call(["--recover"])
            self.assertEqual(rc, 1)
            self.assertIn("does not parse", err)

    def test_runner_token_passes(self):
        with bench_home() as h:
            self.prep(h)
            with lock.DeviceLock(check_build=False) as owner:
                rc, err = self.call(["--rollback"], {"RIVER_BATCH_ID": "b1", "BENCH_LOCK_TOKEN": owner.token})
                self.assertEqual(rc, 0, err)
                os.environ["BENCH_LOCK_TOKEN"] = owner.token
            # Same batch ID but the lock taken by the caller itself (owned): refused.
            rc, _ = self.call(["--rollback"], {"RIVER_BATCH_ID": "b1"})
            os.environ.pop("BENCH_LOCK_TOKEN", None)
            self.assertEqual(rc, 1)


class CameraParse(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(stage.parse_camera_set(None), stage.BOOTSTRAP_CAMERAS)
        self.assertEqual(stage.parse_camera_set("2 0\n1 x\n"), [0, 1, 2])
        self.assertEqual(stage.parse_camera_set(""), [])
        with tempfile.TemporaryDirectory() as t:
            f = Path(t, stage.CAMERA_SWITCH_FILE)
            f.parent.mkdir(parents=True)
            f.write_text("0 2\n")
            self.assertEqual(stage.camera_set(Path(t))["set"], [0, 2])


class H14(unittest.TestCase):
    ROWS = [{"used_ram_kb": str(int(m * 1024)), "host_boot_seconds": "30", "system_used_kb": "1",
             "vendor_used_kb": "1"} for m in (900, 910, 920)]

    def test_lower_reference_is_kept(self):
        old = {"ram_reference": {"tag": "known-good-b0003", "batch": "b0003", "run_ids": ["r"], "median_mb": 850.0,
                                 "seed": None}}
        kg = regression.baseline_record(None, "base-2", {}, ["n"], self.ROWS, [0, 1, 2], existing=old)
        self.assertEqual(kg["ram_reference"], {"tag": "known-good-b0003", "batch": "b0003", "run_ids": ["r"],
                                               "median_mb": 850.0, "seed": None})
        self.assertEqual(kg["calibration_seed"]["median_mb"], 910.0)

    def test_higher_reference_is_replaced(self):
        old = {"ram_reference": {"median_mb": 990.0, "batch": "b1"}}
        kg = regression.baseline_record(None, "base-2", {}, ["n"], self.ROWS, [0, 1, 2], existing=old)
        self.assertEqual(kg["ram_reference"]["median_mb"], 910.0)
        self.assertEqual(kg["ram_reference"]["seed"], "calibration")
        self.assertEqual(regression.baseline_record(None, "b", {}, [], self.ROWS, [0])["ram_reference"]["batch"], "b")


class H18(unittest.TestCase):
    def test_final_settings_reads_the_table(self):
        self.assertEqual(set(features.SETUP_SETTINGS), {"navigation_mode", "location", "extra_dim", "sound_effects",
                                                        "lock_disabled"})
        src = Path(features.__file__).read_text()
        self.assertNotIn("want_s", src)
        self.assertIn("SETUP_SETTINGS.items()", src)


class RescueAttemptIDs(unittest.TestCase):
    """H17 and I3: both exit-4 paths go through rescue() with the attempt ID."""

    def test_bench_boot_failure_rescue_carries_the_attempt(self):
        got = {}
        with bench_home() as h:
            core.write_json(h / "batches/b1/batch.json", {"batch": "b1", "kind": "batch", "flash_attempt": "fa-1",
                                                          "bench_runs": []})
            core.write_json(h / "batches/b1/flash.json", {"attempts": [{"attempt_id": "fa-1", "result": "booted"}]})
            dev = types.SimpleNamespace(guard=lambda: None, root=lambda: None)

            def boom(*a, **k):
                raise TimeoutError("boot to complete")

            def rescue(dev, st, reason, attempt_id=None):
                got.update(reason=reason, attempt_id=attempt_id)
            a = types.SimpleNamespace(batch="b1", flash=False, env="current", boots=1, settle=0)
            with mock.patch.object(bench, "Device", lambda *x: dev), \
                    mock.patch.object(reboots, "planned_reboot", boom), \
                    mock.patch.object(reboots, "load_timing", lambda: {}), \
                    mock.patch.object(stage, "sysrq_boots_required", lambda b, k: (1, "x")), \
                    mock.patch.object(flashing, "rescue", rescue), redirect_stderr(io.StringIO()):
                rc = bench.run(a)
        self.assertEqual(rc, flashing.EXIT_RESCUED)
        self.assertEqual(got["attempt_id"], "fa-1")

    def test_bootloader_fallback_goes_through_rescue(self):
        from test_flash_batch import FakePhone, stage_batch
        got = {}
        with bench_home() as h:
            stage_batch(h)
            phone = FakePhone(h)

            def ok_reboot(dev, path, *, boot_timeout, in_fastboot=None, **kw):
                in_fastboot()
                return types.SimpleNamespace(boot_id="x", host_boot_seconds=30.0, control=None)

            def rescue(dev, st, reason, attempt_id=None):
                got.update(reason=reason, attempt_id=attempt_id)
                return {"attempt_id": attempt_id}
            with mock.patch.object(flashing, "Device", lambda *a: phone), \
                    mock.patch.object(reboots, "planned_reboot", ok_reboot), \
                    mock.patch.object(flashing, "running_slot", lambda dev: "a"), \
                    mock.patch.object(flashing, "rescue", rescue), redirect_stderr(io.StringIO()):
                rc = flashing.flash_batch("b1", None, True)
            batch = core.read_json(h / "batches/b1/batch.json")
        self.assertEqual(rc, flashing.EXIT_RESCUED)
        self.assertEqual(got["attempt_id"], batch["flash_attempt"])
        self.assertIn("bootloader fell back to slot a", got["reason"])


class StageLock(unittest.TestCase):
    """I16: phone-qa stage takes the build lock and refuses runner IDs."""

    def test_refuses_runner_ids_by_hand(self):
        for bid in ("b0001", "q0007"):
            with bench_home(), redirect_stderr(io.StringIO()) as err:
                rc = qa.main(["stage", bid, "--kind", "calibration"])
            self.assertEqual(rc, 1)
            self.assertIn("batch-runner ID", err.getvalue())

    def test_busy_build_lock_refuses(self):
        with bench_home():
            with lock.BuildLock(export=False), redirect_stderr(io.StringIO()) as err:
                rc = qa.main(["stage", "cal-1", "--kind", "calibration"])
            self.assertEqual(rc, 1)
            self.assertIn("build or batch is running", err.getvalue())

    def test_runner_reuse_passes(self):
        with bench_home():
            with lock.BuildLock(export=False) as bl, \
                    mock.patch.dict(os.environ, {"BENCH_BUILD_LOCK_TOKEN": bl.token, "RIVER_BATCH_ID": "b0001"}):
                reuse = lock.BuildLock().acquire()
                self.assertIsNone(qa.stage_refusal("b0001", reuse))
                self.assertIsNotNone(qa.stage_refusal("b0002", reuse))


class RunnerStateReader(unittest.TestCase):
    def test_busy_reason(self):
        with bench_home() as h:
            self.assertIsNone(runnerstate.busy_reason(h))
            (h / "runner").mkdir()
            (h / "runner/state.json").write_text(json.dumps({"running": None, "lease": {"batch": "b0002"}}))
            self.assertIn("lease", runnerstate.busy_reason(h))
            (h / "runner/state.json").write_text("[")
            self.assertIn("unreadable", runnerstate.busy_reason(h))


if __name__ == "__main__":
    unittest.main()
