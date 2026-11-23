"""The control set (A1): a control qualifies only if its fault was really
injected and the target check reached the expected state."""
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from riverqa import controls, core, qa, reboots
from riverqa.controls import CONTROLS, control_verdict
from riverqa.results import FAIL, PASS, UNVERIFIED, Result


def res(**states):
    return {k.replace("__", "."): Result(k.replace("__", "."), v, f"{k} summary") for k, v in states.items()}


class Verdict(unittest.TestCase):
    def test_needs_the_marker(self):
        ok, rec = control_verdict("location-off", res(radios__gps=FAIL), None, 1)
        self.assertFalse(ok)                  # GPS failing on its own proves nothing
        self.assertFalse(rec["injected"])
        ok, rec = control_verdict("location-off", res(radios__gps=FAIL), "location-off", 1)
        self.assertTrue(ok)
        self.assertEqual(rec["targets"]["radios.gps"], {"state": FAIL, "summary": "radios__gps summary"})

    def test_marker_for_another_control_does_not_count(self):
        self.assertFalse(control_verdict("wifi-off", res(radios__wifi=FAIL), "wifi-unbound", 1)[0])

    def test_needs_the_expected_state(self):
        self.assertFalse(control_verdict("wifi-off", res(radios__wifi=UNVERIFIED), "wifi-off", 2)[0])
        self.assertFalse(control_verdict("bt-rfkill", res(radios__bluetooth=FAIL), "bt-rfkill", 1)[0])
        self.assertTrue(control_verdict("bt-rfkill", res(radios__bluetooth=UNVERIFIED), "bt-rfkill", 2)[0])
        self.assertFalse(control_verdict("wifi-off", {}, "wifi-off", 2)[0])   # target missing

    def test_any_listed_target(self):
        ok, rec = control_verdict("wrong-batch-json", res(identity__build_id=PASS, identity__props=FAIL),
                                  "wrong-batch-json", 1)
        self.assertTrue(ok)
        self.assertEqual(rec["check"], "identity.props")

    def test_killed_mid_check_needs_a_restored_leftover(self):
        def journal(restored, killed):
            return {"preflight.journal": Result("preflight.journal", PASS, "x",
                                                {"restored": restored, "killed_runs": killed})}
        self.assertTrue(control_verdict("killed-mid-check", journal(2, ["qa-1"]), "killed-mid-check", 0)[0])
        self.assertFalse(control_verdict("killed-mid-check", journal(0, []), "killed-mid-check", 0)[0])
        self.assertFalse(control_verdict("killed-mid-check", journal(2, []), "killed-mid-check", 0)[0])
        self.assertFalse(control_verdict("killed-mid-check", journal(2, ["qa-1"]), "killed-mid-check", 1)[0])
        self.assertFalse(control_verdict("killed-mid-check", journal(2, ["qa-1"]), None, 0)[0])

    def test_every_qa_control_has_an_injection_site(self):
        import inspect
        import re
        from riverqa import features
        src = inspect.getsource(features) + inspect.getsource(qa)
        pre = inspect.getsource(controls.apply_pre)
        for name, (kind, *_rest) in CONTROLS.items():
            if kind != "qa":
                continue
            marked = re.search(rf'(control_injected = |injected\(run, ){re.escape(repr(name).replace(chr(39), chr(34)))}', src)
            extra_reboot = name.startswith("extra-reboot-") and "control_injected = name" in src
            self.assertTrue(marked or extra_reboot or f'"{name}"' in pre, name)


class RecordControl(unittest.TestCase):
    def test_record_stores_marker_and_summaries(self):
        with bench_home(), mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "beacon-blocked"}):
            run = mock.Mock(version="v1", run_id="r1", control_injected="beacon-blocked",
                            results=res(system__webview=FAIL))
            with redirect_stdout(io.StringIO()):
                controls.record_control(run, 1)
            rec = core.read_json(controls.CONTROLS_FILE)["v1"]["results"]["beacon-blocked"]
        self.assertTrue(rec["qualified"])
        self.assertTrue(rec["injected"])
        self.assertEqual(rec["targets"]["system.webview"]["summary"], "system__webview summary")

    def test_not_injected_is_stored_unqualified(self):
        with bench_home(), mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "beacon-blocked"}):
            run = mock.Mock(version="v1", run_id="r1", control_injected=None, results=res(system__webview=FAIL))
            with redirect_stdout(io.StringIO()):
                controls.record_control(run, 1)
            rec = core.read_json(controls.CONTROLS_FILE)["v1"]["results"]["beacon-blocked"]
        self.assertFalse(rec["qualified"])


class HostControl(unittest.TestCase):
    def test_tag_pushed_host_killed_promotes_through_persisted_state(self):
        with bench_home() as h, redirect_stderr(io.StringIO()):
            ok, got = controls.host_control("tag-pushed-host-killed")
            # The scratch home is gone; the real (test) one is untouched.
            self.assertEqual(core.BENCH_HOME, h)
            self.assertFalse((h / "slot-state.json").exists())
        self.assertTrue(ok, got)
        self.assertIn("persisted known_good_slot b", got)


class EarlyReboot(unittest.TestCase):
    class Dev:
        def __init__(self, completed):
            self.completed, self.reboots = completed, 0

        def state(self):
            return "device"

        def boot_status(self):
            from riverqa.device import BootStatus
            return BootStatus("b", self.completed, 5.0, 0.0, 0.0)

        def reboot_adb(self, target=None):
            self.reboots += 1

    def test_reboots_before_completion(self):
        d = self.Dev(False)
        self.assertEqual(reboots.inject_reboot_before_completion(d, 5), "early-reboot")
        self.assertEqual(d.reboots, 1)

    def test_missed_if_already_completed(self):
        d = self.Dev(True)
        self.assertEqual(reboots.inject_reboot_before_completion(d, 5), "early-reboot-missed")
        self.assertEqual(d.reboots, 0)

    def test_only_in_control_mode(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RIVERQA_CONTROL", None)
            with self.assertRaises(RuntimeError):
                reboots.planned_reboot(self.Dev(False), "adb", boot_timeout=1, inject_early_reboot=True,
                                       watcher=mock.Mock())


class BatchRefusesControls(unittest.TestCase):
    def test_batch_mode_with_override_fails_preflight_controls(self):
        with bench_home(), mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "wifi-off"}):
            core.write_json(core.batch_dir("b1") / "batch.json", {"batch": "b1", "kind": "batch"})
            run = qa.Run("batch", "b1", [], [])
            self.assertEqual(qa.check_controls(run).state, FAIL)


if __name__ == "__main__":
    unittest.main()
