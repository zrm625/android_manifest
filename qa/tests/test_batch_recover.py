"""The recovery entry point (R13.2, I15, I18), the paused pass (I19),
R11.2's abort and forward start (KW first), settle (I5, I9, I11) and
retention before the free-space check (I17)."""
import os
import time
import unittest
from unittest import mock

import _path  # noqa: F401
from batchfakes import FakeDev, FakeGH, FakeTools, mk_ctx, write_json
from benchhome import bench_home
from riverbatch import ctx as C, merge, recover, settle, state as S, tick

R = "zrm625/android_device_motorola_river"
P = "device/motorola/river"


def kg_files(h, slot_batch="kg-1", kg_batch="kg-1", pair=None):
    write_json(h / "slot-state.json", {"known_good_slot": "a", "known_good": {"batch": slot_batch,
                                                                              "build_id": "kg-1-abc"},
                                       "pair": pair, "history": []})
    write_json(h / "qa/known-good.json", {"latest": {"batch": kg_batch, "cameras": [0, 1, 2]}})
    write_json(h / f"batches/{slot_batch}/batch.json", {"batch": slot_batch, "dates": {"vendor": "100"}})


class EntryPoint(unittest.TestCase):
    def test_checkpoint_first(self):
        with bench_home() as h:
            kg_files(h)
            import json
            ss = json.loads((h / "slot-state.json").read_text())
            ss["baseline_closed"] = {"batch": "kg-1", "seed": {"x": 1}, "seed_written": False}
            (h / "slot-state.json").write_text(json.dumps(ss))
            tools = FakeTools()
            recover.entry_point(mk_ctx(tools=tools), setup=False, local_only=True)
            self.assertEqual(tools.calls[0][:2], ("flash", ("--recover",)))

    def test_known_good_records_disagree_pause(self):
        with bench_home() as h:
            kg_files(h, kg_batch="other")
            with self.assertRaises(C.Pause) as cm:
                recover.entry_point(mk_ctx(), setup=False)
            self.assertIn("known-good records disagree", cm.exception.message)

    def test_foreign_pairs(self):
        with bench_home() as h:
            kg_files(h, pair={"batch": "b0009", "kind": "batch"})
            with self.assertRaises(C.Pause):
                recover.entry_point(mk_ctx(), setup=True)
        with bench_home() as h:
            kg_files(h, pair={"batch": "cal-7", "kind": "calibration"})
            tools = FakeTools()
            with self.assertRaises(C.Blocked) as cm:
                recover.entry_point(mk_ctx(tools=tools), setup=True)
            self.assertIn("phone in use by cal-7 (calibration)", cm.exception.message)
            self.assertEqual(tools.calls, [])                 # nothing touched the phone

    def test_i18_fastboot_without_obligation_is_blocked(self):
        with bench_home() as h:
            kg_files(h)
            tools = FakeTools()
            with self.assertRaises(C.Blocked) as cm:
                recover.entry_point(mk_ctx(tools=tools, dev=FakeDev(state="", fastboot=True)), setup=False)
            self.assertEqual(cm.exception.message, "phone in fastboot")
            self.assertEqual(tools.calls, [])

    def test_i18_fb_mode_obligation_recovers(self):
        with bench_home() as h:
            kg_files(h)
            tools = FakeTools()
            with mock.patch("riverqa.fbmode.pending", lambda j=None: {"kind": "fb_mode"}):
                recover.entry_point(mk_ctx(tools=tools, dev=FakeDev(state="", fastboot=True)), setup=False,
                                    local_only=True)
            self.assertIn(("flash", ("--recover",), None), tools.calls)

    def test_recover_strike_pauses_on_the_second(self):
        with bench_home() as h:
            kg_files(h)
            tools = FakeTools({"flash": [(1, ""), (1, "")]})
            with mock.patch("riverqa.fbmode.pending", lambda j=None: {"kind": "fb_mode"}):
                recover.entry_point(mk_ctx(tools=tools), setup=False, local_only=True)
                self.assertEqual(S.read()["strikes"]["recover"], 1)
                with self.assertRaises(C.Pause):
                    recover.entry_point(mk_ctx(tools=tools), setup=False, local_only=True)

    def test_journal_restored_only_with_kb(self):
        with bench_home() as h:
            kg_files(h)
            write_json(h / "qa/journal.json", {"entries": [{"id": "x", "kind": "setting", "key": "k",
                                                            "where": "phone"}]})
            tools = FakeTools()
            recover.entry_point(mk_ctx(tools=tools), setup=False, local_only=True)
            self.assertIn(("phone-qa", ("journal", "--restore"), None), tools.calls)
            tools = FakeTools()
            recover.entry_point(mk_ctx(tools=tools, dev=FakeDev({"ro.boot.slot_suffix": "_b"})), setup=False,
                                local_only=True)
            self.assertEqual(tools.calls, [])                 # KB fails: the journal waits


class LocalOnly(unittest.TestCase):
    """I15: after an unknown outcome the end-of-tick pass is local only."""

    def test_forward_waits_and_no_github(self):
        with bench_home() as h:
            kg_files(h)
            write_json(h / "batches/b0001/batch.json", {"batch": "b0001", "kind": "batch",
                                                        "merge_started": {"at": "t", "expected_prs": {}}})
            with S.transaction() as st:
                st["running"] = {"id": "b0001", "purpose": "batch", "members": [], "merge": {"merged": {}}}
                st["cleanup"] = [{"batch": "b0000", "branch_repos": {P: "1" * 40}, "pr_repos": {}}]
            gh = FakeGH()
            gh.fail["pr_list"] = AssertionError("no GitHub call in a local-only pass")
            gh.fail["ls_remote"] = AssertionError("no GitHub call in a local-only pass")
            recover.entry_point(mk_ctx(gh=gh), setup=True, local_only=True)
            self.assertEqual(S.read()["running"]["id"], "b0001")        # the original outcome stands
            self.assertEqual(gh.calls, [])

    def test_tick_keeps_75_after_unknown(self):
        with bench_home() as h:
            kg_files(h)
            ctx = mk_ctx()
            seen = []

            def entry(ctx, setup, local_only=False):
                seen.append((setup, local_only))
            with mock.patch.object(recover, "entry_point", entry), \
                    mock.patch.object(tick, "preflight", side_effect=C.Unknown("rate_limit")):
                code, summary = tick.tick(ctx, 3)
            self.assertEqual(code, C.EXIT_RESUMABLE)
            self.assertEqual(seen, [(False, False), (True, True)])     # end-of-tick: setup, local only


class PausedPass(unittest.TestCase):
    """I19: a foreign pair only gates setup, never overwrites the pause; a
    paused tick still flushes reports."""

    def test_foreign_pair_keeps_the_pause_reason(self):
        with bench_home() as h:
            kg_files(h, pair={"batch": "cal-1", "kind": "calibration"})
            with S.transaction() as st:
                S.set_pause(st, "two voids in a row")
                S.add_report(st, "pause-1")
            tools = FakeTools()
            ctx = mk_ctx(tools=tools)
            with mock.patch.object(C.Locks, "__enter__", lambda self: self), \
                    mock.patch.object(C.Locks, "release", lambda self: None):
                code, summary = tick.tick(ctx, 3)
            st = S.read()
            self.assertEqual(code, C.EXIT_OK)
            self.assertEqual(st["pause"]["reason"], "two voids in a row")
            self.assertEqual(tools.calls, [])
            self.assertEqual(st["reports_pending"], [])
            self.assertTrue((h / "runner/reports/pause-1.html").exists())

    def test_setup_runs_while_paused_below_the_strike_limit(self):
        with bench_home() as h:
            kg_files(h)
            with S.transaction() as st:
                S.set_pause(st, "x")
            ctx = mk_ctx()
            with mock.patch.object(recover, "setup_known_good") as su:
                recover.paused_pass(ctx)
                su.assert_called_once()
            with S.transaction() as st:
                st["strikes"]["setup"] = 2
            with mock.patch.object(recover, "setup_known_good") as su:
                recover.paused_pass(ctx)
                su.assert_not_called()

    def test_halt_exits_3_and_flushes(self):
        with bench_home():
            with S.transaction() as st:
                S.set_halt(st, "x")
                S.add_report(st, "halt-1")
            code, _ = tick.tick(mk_ctx(), 1)
            self.assertEqual(code, C.EXIT_HALT)
            self.assertEqual(S.read()["reports_pending"], [])


class Settle(unittest.TestCase):
    def setup_run(self, h, halt=True, unknown_since=None):
        write_json(h / "batches/b0001/batch.json", {
            "batch": "b0001", "kind": "batch", "commit_set": {P: "t" * 40}, "tag": "known-good-b0001",
            "merge_started": {"at": "t", "expected_prs": {P: {"repo": R, "number": 7}}}, "bench_runs": [],
            "cameras": {"set": [0, 1, 2]}, "flash_attempt": "fa"})
        with S.transaction() as st:
            if halt:
                S.set_halt(st, "mismatch")
            st["entries"] = [{"name": "perch", "status": "in-batch", "batch": "b0001", "rev": 2, "position": 1,
                              "prs": [{"key": f"{R}#6", "path": P, "head": "h" * 40}], "decisions": [],
                              "record_sha256": "x", "identity": f"{R}#6", "flags": {}}]
            st["running"] = {"id": "b0001", "purpose": "batch", "members": [{"name": "perch"}], "touched": [P],
                             "composed": {P: {"sha": "t" * 40, "tree": "c" * 40}}, "base": {P: "b" * 40},
                             "merge": {"merged": {P: "9" * 40}}, "reconcile_unknown_since": unknown_since}

    def gh(self, landed=True, drift=False):
        gh = FakeGH()
        gh.prs[(R, 7)] = {"state": "MERGED"}
        gh.raw = {}
        status = "behind" if landed else "ahead"
        gh.raw[("gh", "api", f"repos/{R}/compare/river-22.2...{'h' * 40}", "--jq", ".status")] = status
        for (p, s) in merge.slugs().items():
            gh.tips[(s, "refs/heads/river-22.2")] = "k" * 40 if not drift else "d" * 40
            gh.commits[(s, "k" * 40)] = {"parents": [], "tree": "1" * 40}
            gh.commits[(s, "d" * 40)] = {"parents": [], "tree": "2" * 40}
        return gh

    def kg_commitset(self, h):
        cs = {p: "k" * 40 for p in merge.slugs()}
        return cs

    def locks(self):
        return mock.patch.object(C.Locks, "__enter__", lambda self: self), \
            mock.patch.object(C.Locks, "__exit__", lambda self, *a: None)

    def test_i11_known_good_written_before_any_github_call(self):
        with bench_home() as h:
            self.setup_run(h)
            write_json(h / "slot-state.json", {"known_good": {"batch": "b0001"},
                                               "history": [{"event": "promote", "batch": "b0001"}]})
            write_json(h / "qa/known-good.json", {"latest": {"batch": "base", "commit_set": self.kg_commitset(h)},
                                                  "ram_reference": {"median_mb": 900.0}})
            gh = self.gh()
            seen = []
            orig = gh._f

            def spy(name, *a):
                if not seen:
                    import json
                    seen.append(json.loads((h / "qa/known-good.json").read_text())["latest"]["batch"])
                return orig(name, *a)
            gh._f = spy
            l1, l2 = self.locks()
            rows = [{"run_id": "r1", "host_boot_seconds": "30", "system_used_kb": "1", "vendor_used_kb": "1",
                     "used_ram_kb": str(900 * 1024)}]
            with l1, l2, mock.patch("riverqa.bench.attempt_rows", return_value=rows):
                out = settle.settle(mk_ctx(gh=gh), "b0001")
            self.assertEqual(seen, ["b0001"])
            self.assertTrue(out["promoted"] and out["known_good_written"])
            st = S.read()
            self.assertIsNone(st["running"])
            self.assertIsNone(st["halt"])
            self.assertEqual(st["entries"][0]["status"], "merged")
            self.assertIsNone(st["rebaseline"])

    def test_i11_promotion_record_is_the_local_proof(self):
        """Opus (I11 gap): the merge commit is not in running.merge (flash
        --promote ran by hand); the promotion record flash wrote into
        batch.json is enough to write known-good.json before any GitHub call."""
        import json
        with bench_home() as h:
            self.setup_run(h)
            with S.transaction() as st:
                st["running"]["merge"]["merged"] = {}
            b = json.loads((h / "batches/b0001/batch.json").read_text())
            b["promotion"] = {"at": "t", "merge_commits": {P: "9" * 40}}
            (h / "batches/b0001/batch.json").write_text(json.dumps(b))
            write_json(h / "slot-state.json", {"known_good": {"batch": "b0001"},
                                               "history": [{"event": "promote", "batch": "b0001"}]})
            write_json(h / "qa/known-good.json", {"latest": {"batch": "base", "commit_set": self.kg_commitset(h)},
                                                  "ram_reference": {"median_mb": 900.0}})
            gh = self.gh()
            seen = []
            orig = gh._f

            def spy(name, *a):
                if not seen:
                    seen.append(json.loads((h / "qa/known-good.json").read_text())["latest"])
                return orig(name, *a)
            gh._f = spy
            l1, l2 = self.locks()
            rows = [{"run_id": "r1", "host_boot_seconds": "30", "system_used_kb": "1", "vendor_used_kb": "1",
                     "used_ram_kb": str(900 * 1024)}]
            with l1, l2, mock.patch("riverqa.bench.attempt_rows", return_value=rows):
                settle.settle(mk_ctx(gh=gh), "b0001")
            self.assertEqual(seen[0]["batch"], "b0001")
            self.assertEqual(seen[0]["commit_set"][P], "9" * 40)

    def test_not_promoted_landed_owes_a_rebaseline(self):
        with bench_home() as h:
            self.setup_run(h)
            write_json(h / "slot-state.json", {"known_good": {"batch": "base"}, "history": []})
            write_json(h / "qa/known-good.json", {"latest": {"batch": "base", "commit_set": self.kg_commitset(h)}})
            l1, l2 = self.locks()
            with l1, l2:
                out = settle.settle(mk_ctx(gh=self.gh(landed=True)), "b0001")
            st = S.read()
            self.assertTrue(out["rebaseline"])
            self.assertEqual(st["entries"][0]["followup"], "settled in b0001: untagged")
            self.assertEqual(st["rebaseline"]["settle"], "b0001")
            self.assertIn("revert", out["advice"])

    def test_i9_drift_owes_a_rebaseline_even_if_nothing_landed(self):
        with bench_home() as h:
            self.setup_run(h)
            write_json(h / "slot-state.json", {"known_good": {"batch": "base"}, "history": []})
            write_json(h / "qa/known-good.json", {"latest": {"batch": "base", "commit_set": self.kg_commitset(h),
                                                             "cameras": [0, 1, 2]}})
            l1, l2 = self.locks()
            with l1, l2, mock.patch("riverbatch.commands.eligibility_of",
                                    return_value=mock.Mock(kind="ok", reason=None)):
                out = settle.settle(mk_ctx(gh=self.gh(landed=False, drift=True)), "b0001")
            self.assertTrue(out["rebaseline"])
            self.assertTrue(out["drifted"])
            self.assertEqual(S.read()["entries"][0]["status"], "queued")

    def test_i5_preconditions(self):
        with bench_home() as h:
            self.setup_run(h, halt=False)
            write_json(h / "slot-state.json", {"known_good": {"batch": "base"}})
            with self.assertRaises(S.Refused):
                settle.preconditions(mk_ctx(), S.read(), "b0001")
        with bench_home() as h:
            old = "2026-01-01T00:00:00+00:00"
            self.setup_run(h, halt=False, unknown_since=old)
            write_json(h / "slot-state.json", {"known_good": {"batch": "base"}})
            settle.preconditions(mk_ctx(), S.read(), "b0001")          # unknown > 24 h: allowed (I5)
        with bench_home() as h:
            self.setup_run(h)
            write_json(h / "slot-state.json", {"pair": {"batch": "b0001"}})
            with self.assertRaises(S.Refused):
                settle.preconditions(mk_ctx(), S.read(), "b0001")


class Retention(unittest.TestCase):
    def test_old_images_go_except_protected(self):
        with bench_home() as h:
            kg_files(h)
            for b in ("b0001", "kg-1", "b0003"):
                d = h / f"batches/{b}/images"
                d.mkdir(parents=True)
                old = time.time() - 8 * 86400
                os.utime(d, (old, old))
            (h / "batches/b0003/images").touch()
            removed = recover.retention(mk_ctx())
            self.assertEqual(removed, [str(h / "batches/b0001/images")])
            self.assertTrue((h / "batches/kg-1/images").exists())

    def test_i17_retention_runs_before_the_free_space_check(self):
        order = []
        import tempfile
        f = tempfile.NamedTemporaryFile()
        self.addCleanup(f.close)
        ctx = mk_ctx(cfg={"wifi_ssid_file": f.name, "wifi_psk_file": f.name})
        ctx.free_gb = lambda p: order.append("free") or 1000.0
        with mock.patch.object(recover, "retention", lambda c: order.append("retention")):
            calls = [("tools", []), ("kg", {"latest": {"batch": "x", "commit_set": {}}})]
            del calls
            with mock.patch.object(tick.tc, "tools_problems", return_value=[]), \
                    mock.patch.object(tick, "remote_git"), \
                    mock.patch.object(tick.phases, "baseline_xml", return_value={}), \
                    mock.patch.object(tick.compose, "manifest_xml", return_value={}), \
                    mock.patch.object(tick, "branch", side_effect=lambda c, p: "default" if p == ".repo/manifests"
                                      else "river-22.2"), \
                    mock.patch.object(tick, "clean", return_value=True), \
                    mock.patch.object(tick, "qualification_problems", side_effect=C.NotQualified("stop here")):
                ctx.git = mock.Mock()
                ctx.git.out.return_value = "a" * 40
                ctx.known_good = lambda: {"latest": {}}
                with self.assertRaises(C.NotQualified):
                    tick.preflight(ctx)
        self.assertEqual(order, ["retention", "free"])


if __name__ == "__main__":
    unittest.main()
