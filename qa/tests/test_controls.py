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
            return BootStatus("b", self.completed, 5.0, 0.0, 0.1, 0.0)

        def reboot_adb(self, target=None):
            self.reboots += 1

    def test_reboots_before_completion(self):
        d = self.Dev(False)
        self.assertEqual(reboots.inject_reboot_before_completion(d, 5), "early-reboot")
        self.assertEqual(d.reboots, 1)

    def test_waits_for_the_phone_to_drop_off_usb(self):
        d = self.Dev(False)
        w = mock.Mock()
        w.mark.return_value = 7
        self.assertEqual(reboots.inject_reboot_before_completion(d, 5, w), "early-reboot")
        w.wait_gone.assert_called_once_with(7, 120)
        # A reboot that never drops off USB is not an injection: the
        # timeout is caught and the control is unqualified (review round 3).
        w.wait_gone.side_effect = TimeoutError("still on USB")
        self.assertEqual(reboots.inject_reboot_before_completion(self.Dev(False), 5, w), "early-reboot-missed")

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


class Attribution(unittest.TestCase):
    """Controls are credited only for their own fault (review round 2)."""

    def run_with(self, injections, sig_states, attempt="att-1"):
        """sig_states: (label, first boot, combined state, boot_id[, path[, uptime state]]).
        Bench rows default to the sysrq path, after an adb bench boot 1."""
        from pathlib import Path
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        dirs = []
        for label, first, st, bid, *rest in sig_states:
            path = "fastboot" if first else (rest[0] if rest else "sysrq")
            up = rest[1] if len(rest) > 1 else st
            d = tmp / label.replace(" ", "_").replace("#", "_")
            d.mkdir()
            verdict = [st, "x"]
            core.write_json(d / "signals.json", {"path": path, "chain": verdict, "uptime": [up, "x"],
                                                 "boot_count": verdict, "reboot": {"boot_id": bid}})
            dirs.append((label, d, path, first))
        run = mock.Mock()
        run.batch = {"control_injections": injections}
        run.attempt_id.return_value = attempt
        run.control_injected = None
        with mock.patch.object(qa, "evidence_dirs", lambda r: dirs), \
                mock.patch.object(reboots, "load_timing", lambda: {}):
            r = qa.check_boot_signals(run)
        return r, run.control_injected

    BENCH1 = ("bench dev-1#1", False, PASS, "B1", "adb")
    INJ = {"control": "extra-reboot-bench", "injected": True, "flash_attempt": "att-1", "where": "bench boot",
           "run_id": "dev-1", "boot": 2, "boot_id": "B2"}

    def test_injected_boots_own_record_must_be_rejected(self):
        with mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "extra-reboot-bench"}):
            # The injected boot passes; another boot fails. The aggregate is
            # FAIL but the control does not qualify.
            r, marked = self.run_with([self.INJ], [("first boot", True, FAIL, "B0"), self.BENCH1,
                                                   ("bench dev-1#2", False, PASS, "B2")])
            self.assertEqual(r.state, FAIL)
            self.assertIsNone(marked)
            r, marked = self.run_with([self.INJ], [("first boot", True, PASS, "B0"), self.BENCH1,
                                                   ("bench dev-1#2", False, FAIL, "B2")])
            self.assertEqual(marked, "extra-reboot-bench")

    def test_sysrq_bench_injection_must_fail_its_uptime_gate(self):
        # A2: extra-reboot-bench goes into the sysrq bench boot, and only its
        # uptime gate's FAIL qualifies it, not the chain or the count alone.
        with mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "extra-reboot-bench"}):
            _, marked = self.run_with([self.INJ], [("first boot", True, PASS, "B0"), self.BENCH1,
                                                   ("bench dev-1#2", False, FAIL, "B2", "sysrq", PASS)])
            self.assertIsNone(marked)

    def test_every_batch_needs_a_sysrq_bench_boot(self):
        r, _ = self.run_with([], [("first boot", True, PASS, "B0"), self.BENCH1])
        self.assertEqual(r.state, UNVERIFIED)
        self.assertIn("no sysrq bench boot", r.summary)
        r, _ = self.run_with([], [("first boot", True, PASS, "B0"), self.BENCH1,
                                  ("bench dev-1#2", False, PASS, "B2")])
        self.assertEqual(r.state, PASS, r.summary)

    def test_bench_boot_1_must_be_the_adb_reboot(self):
        r, _ = self.run_with([], [("first boot", True, PASS, "B0"), ("bench dev-1#1", False, PASS, "B1", "sysrq"),
                                  ("bench dev-1#2", False, PASS, "B2")])
        self.assertEqual(r.state, UNVERIFIED)
        self.assertIn("bench boot 1 used the sysrq path", r.summary)

    def test_signals_path_must_match_the_row(self):
        from pathlib import Path
        import tempfile
        d = Path(tempfile.mkdtemp())
        core.write_json(d / "signals.json", {"path": "adb", "chain": [PASS, "x"], "uptime": [PASS, "x"],
                                             "boot_count": [PASS, "x"], "reboot": {"boot_id": "B2"}})
        run = mock.Mock()
        run.batch = {}
        dirs = [("bench dev-1#2", d, "sysrq", False)]
        with mock.patch.object(qa, "evidence_dirs", lambda r: dirs), \
                mock.patch.object(reboots, "load_timing", lambda: {}):
            r = qa.check_boot_signals(run)
        self.assertEqual(r.state, UNVERIFIED)
        self.assertIn("differs from the row's sysrq", r.summary)

    def test_boot_id_must_match(self):
        with mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "extra-reboot-bench"}):
            _, marked = self.run_with([self.INJ], [("first boot", True, PASS, "B0"), self.BENCH1,
                                                   ("bench dev-1#2", False, FAIL, "OTHER")])
            self.assertIsNone(marked)

    def test_missing_boot_id_never_matches(self):
        # Review round 3: the injection must carry the boot_id, and the row
        # must have one too.
        rows = [("first boot", True, PASS, "B0"), self.BENCH1, ("bench dev-1#2", False, FAIL, "B2")]
        no_bid = {k: v for k, v in self.INJ.items() if k != "boot_id"}
        with mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "extra-reboot-bench"}):
            self.assertIsNone(self.run_with([no_bid], rows)[1])
            self.assertIsNone(self.run_with([dict(self.INJ, boot_id="")], rows)[1])
            self.assertIsNone(self.run_with([self.INJ], [("first boot", True, PASS, "B0"), self.BENCH1,
                                                         ("bench dev-1#2", False, FAIL, None)])[1])

    def test_first_boot_injection(self):
        inj = dict(self.INJ, control="extra-reboot-first-boot", where="first boot", boot_id="B0")
        with mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "extra-reboot-first-boot"}):
            _, marked = self.run_with([inj], [("first boot", True, FAIL, "B0"), self.BENCH1,
                                              ("bench dev-1#2", False, PASS, "B2")])
            self.assertEqual(marked, "extra-reboot-first-boot")

    def test_missed_injection_never_matches(self):
        missed = {k: v for k, v in self.INJ.items() if k != "flash_attempt"}
        missed["injected"] = False
        rows = [("first boot", True, PASS, "B0"), self.BENCH1, ("bench dev-1#2", False, FAIL, "B2")]
        with mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "extra-reboot-bench"}):
            self.assertIsNone(self.run_with([missed], rows)[1])
            # No attempt ID on the run: a record without one must not match.
            self.assertIsNone(self.run_with([missed], rows, attempt=None)[1])
            self.assertIsNone(self.run_with([dict(self.INJ, flash_attempt=None)], rows, attempt=None)[1])
            self.assertIsNone(self.run_with([dict(self.INJ, injected="yes")], rows)[1])
            self.assertIsNone(self.run_with([dict(self.INJ, flash_attempt="att-0")], rows)[1])

    def test_bt_rfkill_needs_a_valid_powered_report(self):
        from riverqa import features
        real = "Controller AA:BB:CC:DD:EE:FF forge [default]\n\tPowered: yes\n\tPowerState: on\n"
        stub = real.replace("Powered: yes", "Powered: no")
        self.assertTrue(features.bt_rfkill_stub_took(real, stub))
        self.assertFalse(features.bt_rfkill_stub_took("", ""))
        self.assertFalse(features.bt_rfkill_stub_took("No default controller available\n", ""))
        off = real.replace("Powered: yes", "Powered: no")
        self.assertFalse(features.bt_rfkill_stub_took(off, off))
        # Review round 3: a failed `bluetoothctl show` is no report, whatever it printed.
        self.assertFalse(features.bt_rfkill_stub_took(real, stub, 1))

    def test_bt_rfkill_failed_show_is_not_injected(self):
        from riverqa import features
        real = "Controller AA:BB:CC:DD:EE:FF forge [default]\n\tPowered: yes\n"
        run = mock.Mock(control_injected=None)
        with mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "bt-rfkill"}), \
                mock.patch.object(features, "server_cfg", lambda r: {}), \
                mock.patch.object(features, "bt_show", lambda: (1, real)):
            res = features.check_bluetooth(run)
        self.assertEqual(res[0].state, UNVERIFIED)
        self.assertIn("exit 1", res[0].summary)
        self.assertIsNone(run.control_injected)

    def torch_run(self, photo, held=True):
        """check_cameras for camera 0 only, under the torch-disabled control,
        with the helper's photo result ``photo``."""
        from riverqa import features, helper
        run = mock.Mock(control_injected=None, batch={"cameras": {"set": [0]}})

        class R:
            terminal, ok, error = True, True, None

            def __init__(self, d):
                self.d = d

            def get(self, k, default=None):
                return self.d.get(k, default)

        live = R({"timestamps": ",".join(str(i) for i in range(1, 11)), "diffs": "5,5,5,5,5,5,5,5,5"})

        def instrument(run, name, args, **kw):
            return R({"ids": "0"}) if name == "camera_list" else live if name == "camera_live" else R(photo)
        with mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "torch-disabled"}), \
                mock.patch.object(features, "expected_cameras", lambda r: ([0], "batch")), \
                mock.patch.object(helper, "instrument", instrument), \
                mock.patch.object(helper, "fg_problem", lambda r: None), \
                mock.patch.object(features, "disable_torch", lambda r: (True, "capped", ["n"])), \
                mock.patch.object(features, "torch_still_held", lambda r, n: (held, "held")), \
                mock.patch.object(features, "restore_led", lambda r: None), \
                mock.patch.object(features, "th", lambda r, k, d: d), \
                mock.patch.object(features, "param", lambda r, k, d: d):
            res = features.check_cameras(run)
        return res[0], run.control_injected

    def test_torch_needs_the_torch_assertion_to_reject_a_valid_capture(self):
        dark = {"flash_states": "0,2", "luma0": "40", "luma1": "41", "std1": "9"}
        r, marked = self.torch_run(dark)
        self.assertEqual(r.state, FAIL)
        self.assertEqual(marked, "torch-disabled")
        # The torch fired and lit the scene, but the photo failed another
        # assertion (std too low): a FAIL, but not the torch's.
        lit_flat = {"flash_states": "0,3", "luma0": "40", "luma1": "90", "std1": "1"}
        r, marked = self.torch_run(lit_flat)
        self.assertEqual(r.state, FAIL)
        self.assertIsNone(marked)
        # An incomplete capture result proves nothing.
        r, marked = self.torch_run({"flash_states": "0", "luma0": "40", "luma1": "41", "std1": "9"})
        self.assertEqual(r.state, FAIL)
        self.assertIsNone(marked)
        r, marked = self.torch_run({"flash_states": "0,2", "luma0": "40", "std1": "9"})
        self.assertIsNone(marked)
        # The cap did not hold through the capture.
        self.assertIsNone(self.torch_run(dark, held=False)[1])

    def test_torch_must_stay_capped_through_the_capture(self):
        from fakes import FakeShell
        from riverqa import features
        state = {"/sys/class/leds/led:torch_0/max_brightness": "200", "/sys/class/leds/led:torch_0/brightness": "0",
                 "/sys/class/leds/led:switch_0/brightness": "0"}

        def cat(cmd):
            return 0, state[cmd.split()[1]] + "\n"

        def echo(cmd):
            val, f = cmd.split()[1], cmd.split()[3]
            state[f] = val
            return 0, ""
        dev = FakeShell({r"^ls -d": (0, "/sys/class/leds/led:switch_0\n/sys/class/leds/led:torch_0\n"),
                         r"^cat ": cat, r"^echo ": echo})
        run = mock.Mock(dev=dev)
        run.attempt_id.return_value = "a"
        took, note, nodes = features.disable_torch(run)
        self.assertTrue(took, note)
        self.assertEqual(state["/sys/class/leds/led:torch_0/max_brightness"], "0")
        self.assertTrue(features.torch_still_held(run, nodes)[0])
        # The torch request got through during the capture: not held.
        state["/sys/class/leds/led:torch_0/brightness"] = "100"
        self.assertFalse(features.torch_still_held(run, nodes)[0])
        state["/sys/class/leds/led:torch_0/brightness"] = "0"
        state["/sys/class/leds/led:torch_0/max_brightness"] = "200"
        self.assertFalse(features.torch_still_held(run, nodes)[0])
        # Only a switch node: the request could re-enable it, so no control.
        dev2 = FakeShell({r"^ls -d": (0, "/sys/class/leds/led:switch_0\n"), r"^cat ": cat, r"^echo ": echo})
        self.assertFalse(features.disable_torch(mock.Mock(dev=dev2))[0])


class CalibrationRefusesControls(unittest.TestCase):
    def test_every_mode_but_control_fails_on_an_override(self):
        with bench_home(), mock.patch.dict(os.environ, {"RIVERQA_CONTROL": "wifi-off"}):
            core.write_json(core.batch_dir("b1") / "batch.json", {"batch": "b1", "kind": "calibration"})
            for mode in ("calibrate", "batch", "baseline"):
                run = qa.Run(mode, "b1", [], [])
                self.assertEqual(qa.check_controls(run).state, FAIL, mode)
            self.assertEqual(qa.check_controls(qa.Run("control", "b1", [], [])).state, PASS)


if __name__ == "__main__":
    unittest.main()
