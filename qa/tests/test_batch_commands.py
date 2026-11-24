"""Queue commands (R5.5, R5.6, R9.4, I13), eligibility (R5.3, R5.4, I21
renames) and the CLI's Zak-only guard."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
from batchfakes import FakeGH, mk_ctx, write_json
from benchhome import bench_home
from riverbatch import cli, commands, eligibility, merge, records, state as S
from test_batch_records import H2, H3, K1, K2, make_record

RIVER = "zrm625/android_device_motorola_river"
VENDOR = "zrm625/android_vendor_lineage"


def world(h, gh: FakeGH, rec, heads=None):
    """A GitHub where the record's PRs are open at the approved heads."""
    write_json(h / "qa/known-good.json", {"latest": {"batch": "kg", "cameras": [0, 1, 2]}})
    for path, s in merge.slugs().items():
        gh.tips[(s, "refs/heads/river-22.2")] = "b" * 40
    heads = heads or records.approved_heads(rec)
    for k, head in heads.items():
        repo, n = records.split_key(k)
        gh.prs[(repo, n)] = {"state": "OPEN", "headRefName": rec["branch"], "headRefOid": head,
                             "baseRefName": "river-22.2", "isCrossRepository": False}


class Enqueue(unittest.TestCase):
    def enqueue(self, h, d, gh=None, rec=None):
        gh = gh or FakeGH()
        rec = rec or make_record(d)
        world(h, gh, rec)
        return commands.enqueue(mk_ctx(gh=gh), d / "record.json"), gh

    def test_ok_then_duplicate_refused(self):
        with bench_home() as h, tempfile.TemporaryDirectory() as t:
            status, _ = self.enqueue(h, Path(t))
            self.assertEqual(status, "queued")
            e = S.read()["entries"][0]
            self.assertEqual({p["key"]: p["head"] for p in e["prs"]}, {K1: H3, K2: H2})
            self.assertTrue((h / f"runner/council/perch-launcher/{e['record_sha256']}/record.json").exists())
            with self.assertRaises(S.Refused):
                self.enqueue(h, Path(t))

    def test_i13_alias_refused(self):
        with bench_home() as h, tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as t2:
            self.enqueue(h, Path(t))
            commands.withdraw("perch-launcher")
            rec2 = make_record(Path(t2), name="perch-again")
            with self.assertRaises(S.Refused) as cm:
                self.enqueue(h, Path(t2), rec=rec2)
            self.assertIn("belong to another change", str(cm.exception))

    def test_third_failure_needs_zak_across_withdraw_and_reenqueue(self):
        with bench_home() as h, tempfile.TemporaryDirectory() as t:
            self.enqueue(h, Path(t))
            with S.transaction() as st:
                e = st["entries"][0]
                for i in range(3):
                    S.add_failure(st, e, {"batch": f"b{i}"})
                S.set_status(e, "failed", "x")
            commands.withdraw("perch-launcher")
            status, _ = self.enqueue(h, Path(t))
            self.assertEqual(status, "waiting-zak")
            self.assertIn("failed 3 times", S.read()["entries"][-1]["reason"])

    def test_pr_set_problems_refuse(self):
        with bench_home() as h, tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d)
            gh = FakeGH()
            world(h, gh, rec)
            gh.prs[(RIVER, 6)]["headRefOid"] = "f" * 40                    # head moved
            with self.assertRaises(S.Refused):
                commands.enqueue(mk_ctx(gh=gh), d / "record.json")
            self.assertEqual(S.read()["entries"], [])

    def test_transport_writes_nothing(self):
        with bench_home() as h, tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d)
            gh = FakeGH()
            world(h, gh, rec)
            from riverbatch import ctx as C
            gh.fail["pr_list"] = C.Unknown("x")
            with self.assertRaises(C.Unknown):
                commands.enqueue(mk_ctx(gh=gh), d / "record.json")
            self.assertEqual(S.read()["entries"], [])

    def test_requeue_and_tried(self):
        with bench_home() as h, tempfile.TemporaryDirectory() as t:
            status, gh = self.enqueue(h, Path(t))
            with S.transaction() as st:
                e = st["entries"][0]
                S.add_failure(st, e, {"batch": "b1"})
                S.set_status(e, "failed", "x")
            with self.assertRaises(S.Refused):
                commands.requeue(mk_ctx(gh=gh), "perch-launcher", "short", False)
            reason = "radios.wifi failed in b0001 and in b0002 for an unrelated change: the desk."
            self.assertEqual(commands.requeue(mk_ctx(gh=gh), "perch-launcher", reason, False), "queued")
            e = S.read()["entries"][0]
            self.assertTrue(e["flags"]["solo"])
            self.assertEqual(e["decisions"][-1]["action"], "requeue")
            with S.transaction() as st:
                for i in range(2):
                    S.add_failure(st, st["entries"][0], {"batch": f"c{i}"})
                S.set_status(st["entries"][0], "failed", "x")
            self.assertEqual(commands.requeue(mk_ctx(gh=gh), "perch-launcher", reason, False), "waiting-zak")
            self.assertEqual(commands.requeue(mk_ctx(gh=gh), "perch-launcher", reason, True), "queued")
            with S.transaction() as st:
                S.set_status(st["entries"][0], "failed", "x")
            commands.tried("perch-launcher", "The QA log shows the launcher crashing on start (apps.launcher).")
            self.assertEqual(S.read()["entries"][0]["status"], "tried")

    def test_resume(self):
        with bench_home():
            with self.assertRaises(S.Refused):
                commands.resume()
            with S.transaction() as st:
                S.set_pause(st, "setup failed twice", step="setup")
                st["strikes"]["setup"] = 2
                st["voids_in_a_row"] = 1
            commands.resume()
            st = S.read()
            self.assertEqual((st["pause"], st["strikes"]["setup"], st["voids_in_a_row"]), (None, 0, 0))


class Eligibility(unittest.TestCase):
    def base(self):
        return {p: "b" * 40 for p in merge.slugs()}

    def test_base_camera_set(self):
        gh = FakeGH()
        gh.files[(RIVER, "qa/cameras", "b" * 40)] = ("blob", "0 2\n")
        with self.assertRaises(eligibility.BaseCameraMismatch):
            eligibility.eligible(gh, {}, [], self.base(), merge.slugs(), [0, 1, 2])

    def test_invalid_record_needs_reapproval(self):
        out = eligibility.eligible(FakeGH(), None, ["bad"], self.base(), merge.slugs(), [0, 1, 2])
        self.assertEqual(out.kind, eligibility.REAPPROVE)

    def test_camera_set_change_waits(self):
        with tempfile.TemporaryDirectory() as t:
            rec = make_record(Path(t))
            gh = FakeGH()
            with bench_home() as h:
                world(h, gh, rec)
            gh.files[(RIVER, "qa/cameras", H3)] = ("blob2", "0 1\n")
            out = eligibility.eligible(gh, rec, [], self.base(), merge.slugs(), [0, 1, 2])
            self.assertEqual(out.kind, eligibility.WAITING)
            self.assertIn("camera", out.reason)

    def test_manifest_prs_i21_renames(self):
        gh = FakeGH()
        rec = {"prs": [{"key": "zrm625/android_manifest#12", "path": ".repo/manifests"}]}
        gh.changed[("zrm625/android_manifest", 12)] = [{"filename": "qa/x.py"}]
        self.assertEqual(eligibility.manifest_outcome(gh, rec).kind, eligibility.REFUSED)
        gh.changed[("zrm625/android_manifest", 12)] = [{"filename": "qa/charter.md",
                                                        "previous_filename": "CHARTER.md"}]
        self.assertEqual(eligibility.manifest_outcome(gh, rec).kind, eligibility.WAITING)
        gh.changed[("zrm625/android_manifest", 12)] = [{"filename": "default.xml"}]
        self.assertEqual(eligibility.manifest_outcome(gh, rec).kind, eligibility.WAITING)

    def test_tooling_paths(self):
        for p in ("qa/a.py", "scripts/x", "workspace/AGENTS.md", "AGENTS.md", "qa/README.md"):
            self.assertTrue(eligibility.tooling_path(p), p)
        for p in ("CHARTER.md", "default.xml", "local.xml", "foo.txt"):
            self.assertFalse(eligibility.tooling_path(p), p)


class CLI(unittest.TestCase):
    def run_cli(self, argv):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            code = cli.main(argv, mk_ctx())
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    def test_zak_only(self):
        with bench_home():
            for argv in (["resume"], ["settle", "b0001"], ["cleanup", "drop", "b0001"]):
                code, last = self.run_cli(argv)
                self.assertEqual(code, 1, argv)
                self.assertIn("Zak-only", last["refused"])

    def test_status_json_and_last_line(self):
        with bench_home():
            code, last = self.run_cli(["withdraw", "nothing"])
            self.assertEqual((code, last["exit"]), (1, 1))
            with redirect_stdout(io.StringIO()) as out:
                cli.main(["status", "--json"], mk_ctx())
            lines = out.getvalue().strip().splitlines()
            self.assertEqual(json.loads(lines[-1])["exit"], 0)

    def test_runner_error_twice_halts(self):
        with bench_home():
            with mock.patch("riverbatch.commands.withdraw", side_effect=RuntimeError("bug")):
                self.assertEqual(self.run_cli(["withdraw", "x"])[0], 1)
                self.assertEqual(self.run_cli(["withdraw", "x"])[0], 3)
            self.assertIn("second runner error", S.read()["halt"]["reason"])

    def test_a_bad_record_is_a_refusal_not_a_runner_error(self):
        with bench_home(), tempfile.TemporaryDirectory() as t:
            f = Path(t) / "record.json"
            f.write_text("not json")
            for _ in range(2):                                       # twice: still no HALT
                code, last = self.run_cli(["enqueue", "--record", str(f)])
                self.assertEqual(code, 1)
                self.assertIn("not JSON", last["refused"])
            st = S.read()
            self.assertIsNone(st["halt"])
            self.assertIsNone(st["last_error"])


if __name__ == "__main__":
    unittest.main()
