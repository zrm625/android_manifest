"""The run flow with fakes: preflight can't be skipped and stops on a
failure, --only runs reach finish(), boot continuity (B2), the current flash
attempt's rows (B1) and the harness version across seed retirement (B3)."""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from riverqa import bench, core, qa, regression
from riverqa.results import FAIL, PASS, UNVERIFIED, Result


class FakeDev:
    serial = "ZY225DPT7F"

    def guard(self):
        pass

    def root(self, seconds=60):
        pass

    def state(self):
        return "device"

    def read(self, cmd, timeout=30):
        from riverqa.device import Read
        return Read(0, "false\n" if "low_ram" in cmd else "")


def make_run(mode="batch", only=(), skip=(), batch="b1"):
    if batch:
        core.write_json(core.batch_dir(batch) / "batch.json",
                        {"batch": batch, "kind": "batch", "experiment": False, "bench_runs": []})
    run = qa.Run(mode, batch, list(only), list(skip))
    run.dev = FakeDev()
    return run


def ok(cid):
    return lambda run: Result(cid, PASS, "ok")


class Wanted(unittest.TestCase):
    def test_preflight_is_always_wanted(self):
        with bench_home():
            run = make_run(only=["identity"], skip=["preflight.helper"])
            self.assertTrue(run.wanted("preflight.helper"))
            self.assertTrue(run.wanted("preflight.snapshot"))
            self.assertTrue(run.wanted("identity.props"))
            self.assertFalse(run.wanted("radios.wifi"))

    def test_skip_preflight_is_refused(self):
        with bench_home(), redirect_stdout(io.StringIO()):
            self.assertEqual(qa.main(["--batch", "b1", "--skip", "preflight"]), 1)
            self.assertEqual(qa.main(["--batch", "b1", "--skip", "preflight.journal"]), 1)

    def test_keystore_is_not_a_control_override(self):
        with bench_home():
            with mock.patch.dict(os.environ, {"RIVERQA_KEYSTORE": "/x"}):
                self.assertEqual(qa.control_overrides(), [])
            with mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "wifi-off"}):
                self.assertEqual(qa.control_overrides(), ["RIVERQA_CONTROL"])


class Preflight(unittest.TestCase):
    PATCHES = ("check_fixtures", "check_journal", "check_helper", "check_snapshot", "check_continuity")

    def run_preflight(self, run, **override):
        fns = {n: ok("preflight." + n[6:]) for n in self.PATCHES}
        fns.update(override)
        with mock.patch.multiple(qa, **fns), redirect_stdout(io.StringIO()):
            return qa.preflight(run, Result("preflight.lock", PASS, "held"))

    def test_controls_fail_stops(self):
        with bench_home(), mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "wifi-off"}):
            run = make_run()
            self.assertFalse(self.run_preflight(run))
            self.assertEqual(run.results["preflight.controls"].state, FAIL)
            self.assertNotIn("preflight.journal", run.results)

    def test_journal_failure_stops(self):
        with bench_home():
            run = make_run()
            self.assertFalse(self.run_preflight(
                run, check_journal=lambda r: Result("preflight.journal", FAIL, "journal recovery failed")))
            self.assertNotIn("preflight.helper", run.results)

    def test_helper_failure_skips_the_snapshot(self):
        with bench_home():
            run = make_run()
            self.assertFalse(self.run_preflight(
                run, check_helper=lambda r: Result("preflight.helper", UNVERIFIED, "build failed")))
            self.assertNotIn("preflight.snapshot", run.results)

    def test_clean_preflight(self):
        with bench_home():
            self.assertTrue(self.run_preflight(make_run()))


class OnlyRuns(unittest.TestCase):
    def test_full_run_and_finish_with_only(self):
        """--only identity: preflight still runs, the identity checks run,
        nothing else is required, exit 0 is possible, eligible never is."""
        ran = []

        def identity(run):
            for cid in qa.GROUPS_IDENTITY:
                run.add(Result(cid, PASS, "ok"))
            ran.append("identity")

        def features(run):
            ran.append("features")

        def cleanup(run):
            ran.append("cleanup")
        fns = {n: ok("preflight." + n[6:]) for n in Preflight.PATCHES}
        with bench_home() as h, mock.patch.multiple(qa, identity=identity, health=lambda run: ran.append("health"),
                                                    **fns), \
                mock.patch("riverqa.features.run_features", features), \
                mock.patch("riverqa.features.cleanup_and_final", cleanup), redirect_stdout(io.StringIO()):
            run = make_run(only=["identity"])
            code = qa.full_run(run, Result("preflight.lock", PASS, "held"))
            doc = json.loads((run.dir / "results.json").read_text())
        self.assertEqual(ran, ["identity", "health", "features", "cleanup"])
        self.assertEqual(code, 0)
        self.assertFalse(doc["eligible"])
        self.assertIn("not the full check set (--only/--skip)", doc["not_eligible_because"])
        self.assertNotIn("radios.wifi", doc["results"])
        self.assertIn("preflight.continuity", doc["results"])

    def test_finish_with_only_fills_missing_wanted_checks(self):
        with bench_home(), redirect_stdout(io.StringIO()):
            run = make_run(only=["identity"])
            run.add(Result("identity.props", PASS, "ok"))
            code = qa.finish(run, qa.REQUIRED)
            doc = json.loads((run.dir / "results.json").read_text())
        self.assertEqual(code, 2)
        self.assertEqual(doc["results"]["identity.build_id"]["state"], UNVERIFIED)
        self.assertEqual(doc["results"]["preflight.guards"]["state"], UNVERIFIED)


class Continuity(unittest.TestCase):
    ROWS = [{"boot_id": "x1"}, {"boot_id": "x2"}]

    def test_clean(self):
        self.assertEqual(qa.continuity_verdict(self.ROWS, "x2", 3)[:1], (PASS,))

    def test_reset_after_the_bench(self):
        st, msg, _ = qa.continuity_verdict(self.ROWS, "y9", 3)
        self.assertEqual(st, FAIL)
        self.assertEqual(qa.continuity_verdict(self.ROWS, "x2", 4)[0], FAIL)

    def test_unreadable_or_missing(self):
        self.assertEqual(qa.continuity_verdict([], "x2", 1)[0], UNVERIFIED)
        self.assertEqual(qa.continuity_verdict(self.ROWS, None, 3)[0], UNVERIFIED)
        self.assertEqual(qa.continuity_verdict(self.ROWS, "x2", None)[0], UNVERIFIED)
        self.assertEqual(qa.continuity_verdict(self.ROWS, "x2", 2)[0], UNVERIFIED)


class AttemptRows(unittest.TestCase):
    def test_only_the_current_attempt(self):
        rows = [{"batch": "b1", "run_id": "r1", "flash_attempt": "f1"},
                {"batch": "b1", "run_id": "r2", "flash_attempt": "f2"},
                {"batch": "b2", "run_id": "r2", "flash_attempt": "f2"},
                {"batch": "b1", "run_id": "r3", "flash_attempt": "f2"}]
        batch = {"batch": "b1", "bench_runs": ["r2", "r1"], "flash_attempt": "f2"}
        self.assertEqual([r["run_id"] for r in bench.attempt_rows(batch, rows)], ["r2"])
        self.assertEqual(bench.attempt_rows(dict(batch, flash_attempt=None), rows), [])


class HarnessVersion(unittest.TestCase):
    def test_seed_retirement_keeps_the_version(self):
        rows = [{"used_ram_kb": str(k), "host_boot_seconds": "40", "system_used_kb": "1",
                 "vendor_used_kb": "1"} for k in (950 * 1024, 960 * 1024, 955 * 1024)]
        with bench_home():
            kg = regression.baseline_record(None, "base", {}, ["r"], rows, [0, 1, 2])
            core.write_json(qa.KNOWN_GOOD, kg)
            v1 = qa.harness_version()
            after = regression.update_on_tag(kg, "kg-1", "b1", {}, ["r1"], rows, [0, 1, 2])
            self.assertIsNone(after["ram_reference"]["seed"])
            core.write_json(qa.KNOWN_GOOD, after)
            self.assertEqual(qa.harness_version(), v1)
            core.write_json(qa.KNOWN_GOOD, dict(after, calibration_seed={"median_mb": 1.0}))
            self.assertNotEqual(qa.harness_version(), v1)

    def test_boot_timing_blocks_change_the_version(self):
        from riverqa import reboots
        with bench_home():
            v1 = qa.harness_version()
            reboots.save_timing({"paths": {"adb": {"A": 10.0, "proof_normal_pass": True}}, "chain": {}})
            v2 = qa.harness_version()
            reboots.save_timing({"paths": {"adb": {"A": 10.0, "proof_normal_pass": False}}, "chain": {}})
            self.assertNotEqual(v1, v2)
            self.assertNotEqual(v2, qa.harness_version())


if __name__ == "__main__":
    unittest.main()
