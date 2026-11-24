"""One batch end to end with its steps faked (R8.3, R9, R11.2, R11.6):
fail, split, pass to the merge path, a transport abort before
merge_started, and a crash mid-flash recovered by the rescue evidence."""
import unittest
from unittest import mock

import _path  # noqa: F401
from batchfakes import FakeTools, mk_ctx, write_json
from benchhome import bench_home
from riverbatch import ctx as C, outcomes as O, phases, recover, state as S, tick


def claim(members=("a",), purpose="batch"):
    with S.transaction() as st:
        st["entries"] = [{"name": m, "position": i + 1, "status": "in-batch", "batch": "b0001", "rev": 2,
                          "prs": [{"key": f"zrm625/x#{i}", "path": "p", "head": "1" * 40}], "flags": {},
                          "identity": f"zrm625/x#{i}", "kind": "change", "decisions": []}
                         for i, m in enumerate(members)]
        st["running"] = {"id": "b0001", "purpose": purpose, "phase": "claimed", "members": [{"name": m}
                                                                                            for m in members],
                         "touched": [], "base": {}, "composed": {}, "content_key": "k", "attempt_id": "fa-1",
                         "versions": {"harness": "h", "runner_build": "rb", "runner_tools": "rt", "image_id": "i"},
                         "merge": {"prs": {}, "merged": {}}, "outcome": None}
    return S.read()["running"]


def steps(outcome=None, raise_in=None, exc=None):
    """Patch every phase; ``raise_in`` names the phase that raises ``exc``."""
    names = ["checkout", "build", "stage_run", "restore_or_void", "flash", "bench", "setup_candidate"]
    ps = []
    for n in names:
        if n == raise_in:
            ps.append(mock.patch.object(phases, n, side_effect=exc))
        else:
            ps.append(mock.patch.object(phases, n))
    ps.append(mock.patch.object(phases, "test", return_value=outcome or {"class": O.PASS}))
    ps.append(mock.patch.object(tick, "commit_phase"))
    return ps


class Flow(unittest.TestCase):
    def run_batch(self, h, run, patches, pair=True, tools=None):
        if pair:
            write_json(h / "slot-state.json", {"pair": {"batch": "b0001", "attempts": ["fa-1"]}})
        tools = tools or FakeTools()
        ctx = mk_ctx(tools=tools)
        for p in patches:
            p.start()
        try:
            tick.run_batch(ctx, run, {"latest": {"cameras": [0, 1, 2]}})
        finally:
            mock.patch.stopall()
        return tools

    def test_single_fail_rolls_back_and_records(self):
        with bench_home() as h:
            run = claim(["a"])
            fail = {"class": O.FAIL, "checks": ["apps.camera"], "evidence": {"step": "qa", "run_ids": ["r"]}}
            tools = self.run_batch(h, run, steps(fail))
            st = S.read()
            self.assertIn(("flash", ("--rollback",), "b0001"), tools.calls)
            self.assertIsNone(st["running"])
            self.assertEqual(st["entries"][0]["status"], "failed")
            self.assertEqual(S.failures(st, "a")[0]["checks"], ["apps.camera"])
            self.assertEqual(st["last_fail"]["batch"], "b0001")
            self.assertIn("b0001", st["reports_pending"])

    def test_two_changes_fail_split(self):
        with bench_home() as h:
            run = claim(["a", "b"])
            self.run_batch(h, run, steps({"class": O.FAIL, "checks": ["x"]}))
            st = S.read()
            self.assertEqual([(e["status"], e["flags"]["solo"]) for e in st["entries"]], [("queued", True)] * 2)

    def test_rescued_flash_is_a_fail_without_rollback(self):
        with bench_home() as h:
            run = claim(["a"])
            exc = C.Abort({"class": O.FAIL, "checks": ["flash.boot"], "rescued": True, "evidence": {}})
            tools = self.run_batch(h, run, steps(raise_in="flash", exc=exc), pair=False)
            self.assertEqual(tools.calls, [])                         # the rescue already rolled back
            self.assertEqual(S.read()["entries"][0]["status"], "failed")

    def test_void_with_pause(self):
        with bench_home() as h:
            run = claim(["a"])
            exc = C.Abort({"class": O.VOID, "reason": "image"}, pause="build image changed: requalify, R14.4")
            with self.assertRaises(C.Pause):
                self.run_batch(h, run, steps(raise_in="build", exc=exc), pair=False)
            st = S.read()
            self.assertEqual(st["entries"][0]["status"], "queued")
            self.assertEqual(st["pause"]["reason"], "build image changed: requalify, R14.4")

    def test_pass_goes_to_the_merge_path(self):
        with bench_home() as h:
            run = claim(["a"])
            write_json(h / "batches/b0001/batch.json", {"kind": "batch", "experiment": False})
            ps = steps() + [mock.patch.object(tick, "merge_path"),
                            mock.patch.object(tick.version, "current",
                                              return_value={"harness": "h", "runner_build": "rb", "runner_tools": "rt"}),
                            mock.patch.object(tick, "qualification_problems", return_value=[])]
            for p in ps:
                p.start()
            try:
                tick.run_batch(mk_ctx(), run, {})
                self.assertTrue(tick.merge_path.called)
            finally:
                mock.patch.stopall()

    def test_pass_with_changed_tools_does_not_merge(self):
        with bench_home() as h:
            run = claim(["a"])
            write_json(h / "batches/b0001/batch.json", {"kind": "batch", "experiment": False})
            ps = steps() + [mock.patch.object(tick, "merge_path"),
                            mock.patch.object(tick.version, "current",
                                              return_value={"harness": "h2", "runner_build": "rb", "runner_tools": "rt"}),
                            mock.patch.object(tick, "qualification_problems", return_value=[])]
            for p in ps:
                p.start()
            try:
                with self.assertRaises(C.Pause):
                    tick.run_batch(mk_ctx(), run, {})
                self.assertFalse(tick.merge_path.called)
            finally:
                mock.patch.stopall()
            self.assertEqual(S.read()["entries"][0]["status"], "queued")

    def test_unknown_before_merge_started_is_a_transport_abort(self):
        with bench_home() as h:
            run = claim(["a"])
            write_json(h / "batches/b0001/batch.json", {"kind": "batch", "experiment": False})
            ps = steps() + [mock.patch.object(tick, "merge_path", side_effect=C.Unknown("pr create")),
                            mock.patch.object(tick.version, "current",
                                              return_value={"harness": "h", "runner_build": "rb", "runner_tools": "rt"}),
                            mock.patch.object(tick, "qualification_problems", return_value=[])]
            with self.assertRaises(C.Unknown):
                self.run_batch(h, run, ps)
            st = S.read()
            self.assertIsNone(st["running"])
            self.assertEqual(st["entries"][0]["status"], "queued")
            self.assertEqual(st["voids_in_a_row"], 0)                   # unknown never consumes a void

    def test_auth_before_merge_started_aborts_and_pauses(self):
        with bench_home() as h:
            run = claim(["a"])
            write_json(h / "batches/b0001/batch.json", {"kind": "batch", "experiment": False})
            ps = steps() + [mock.patch.object(tick, "merge_path",
                                              side_effect=C.AuthRefused("push", "zrm625/x", 401, "bad")),
                            mock.patch.object(tick.version, "current",
                                              return_value={"harness": "h", "runner_build": "rb", "runner_tools": "rt"}),
                            mock.patch.object(tick, "qualification_problems", return_value=[])]
            with self.assertRaises(C.Pause):
                self.run_batch(h, run, ps)
            self.assertIn("GitHub auth: zrm625/x: 401", S.read()["pause"]["reason"])


class Crash(unittest.TestCase):
    """R11.2 step 2a: a crash after a completed rescue is a fail, not a void."""

    def test_rescue_evidence_first(self):
        with bench_home() as h:
            run = claim(["a"])
            with S.transaction() as st:
                st["running"]["phase"] = "flashing"
                st["running"]["attempt_id"] = None
            write_json(h / "batches/b0001/batch.json", {"flash_attempt": "fa-1"})
            write_json(h / "batches/b0001/flash.json", {"attempts": [{"attempt_id": "fa-1",
                                                                      "started_at": "2026-11-23T10:00:00+00:00"}]})
            write_json(h / "slot-state.json", {"pair": None, "history": [
                {"event": "rolled-back", "batch": "b0001", "how": "rescue: batch b0001: x; wipe #2; ok"}]})
            (h / "qa").mkdir(exist_ok=True)
            (h / "qa/wipes.jsonl").write_text('{"seq": 2, "attempt_id": "fa-1", "how": "rollback", "ok": true, '
                                              '"at": "2026-11-23T10:30:00+00:00"}\n')
            tools = FakeTools()
            recover.recover_running(mk_ctx(tools=tools))
            st = S.read()
            self.assertEqual(tools.calls, [])
            self.assertEqual(st["entries"][0]["status"], "failed")
            self.assertEqual(S.failures(st, "a")[0]["checks"], ["flash.boot"])

    def test_crash_mid_batch_rolls_back_and_voids(self):
        with bench_home() as h:
            run = claim(["a"])
            with S.transaction() as st:
                st["running"]["phase"] = "testing"
            write_json(h / "slot-state.json", {"pair": {"batch": "b0001"}})
            tools = FakeTools()
            recover.recover_running(mk_ctx(tools=tools))
            st = S.read()
            self.assertEqual(tools.calls, [("flash", ("--rollback",), "b0001")])
            self.assertEqual((st["entries"][0]["status"], st["voids_in_a_row"]), ("queued", 1))

    def test_failed_rollback_retries_and_second_pauses(self):
        with bench_home() as h:
            claim(["a"])
            write_json(h / "slot-state.json", {"pair": {"batch": "b0001"}})
            tools = FakeTools({"flash": [(1, ""), (1, "")]})
            with self.assertRaises(C.Retry):
                recover.recover_running(mk_ctx(tools=tools))
            with self.assertRaises(C.Pause):
                recover.recover_running(mk_ctx(tools=tools))
            self.assertIsNotNone(S.read()["running"])

    def test_merge_started_goes_forward_never_rolls_back(self):
        with bench_home() as h:
            claim(["a"])
            write_json(h / "batches/b0001/batch.json", {"merge_started": {"at": "t", "expected_prs": {}}})
            write_json(h / "slot-state.json", {"pair": {"batch": "b0001"}})
            tools = FakeTools()
            self.assertEqual(recover.recover_running(mk_ctx(tools=tools), local_only=True), "waits")
            self.assertEqual(tools.calls, [])


if __name__ == "__main__":
    unittest.main()
