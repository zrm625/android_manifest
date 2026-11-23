import unittest

import _path  # noqa: F401
from riverqa.results import (FAIL, HELPER_CHECKS, INFO, PASS, REQUIRED, UNVERIFIED, GateInputs, Result, eligibility,
                             exit_code, fill_missing, low_ram_check, parse_bool, worst)


def all_pass(**override):
    r = {c: Result(c, PASS, "ok", helper_terminal=True if c in HELPER_CHECKS else None) for c in REQUIRED}
    for k, v in override.items():
        r[k.replace("__", ".")] = Result(k.replace("__", "."), v, "x")
    return r


def gate(**kw):
    base = dict(mode="batch", full_set=True, experiment=False, low_ram="false", controls_qualified=True,
                kind="batch", thresholds_calibrated=True, calibration_runs=5)
    base.update(kw)
    return GateInputs(**base)


class States(unittest.TestCase):
    def test_worst(self):
        self.assertEqual(worst([PASS, UNVERIFIED, FAIL]), FAIL)
        self.assertEqual(worst([PASS, UNVERIFIED]), UNVERIFIED)
        self.assertEqual(worst([PASS, INFO]), PASS)
        self.assertEqual(worst([INFO]), UNVERIFIED)
        self.assertEqual(worst([]), UNVERIFIED)

    def test_bad_state(self):
        with self.assertRaises(ValueError):
            Result("x", "OK", "")

    def test_exit_codes(self):
        self.assertEqual(exit_code(all_pass(), REQUIRED), 0)
        self.assertEqual(exit_code(all_pass(radios__gps=UNVERIFIED), REQUIRED), 2)
        # FAIL wins over UNVERIFIED.
        self.assertEqual(exit_code(all_pass(radios__gps=UNVERIFIED, apps__brave=FAIL), REQUIRED), 1)
        # An INFO result never satisfies a required ID.
        self.assertEqual(exit_code(all_pass(radios__gps=INFO), REQUIRED), 2)

    def test_dropped_check_cannot_pass(self):
        r = all_pass()
        del r["system.video"]
        self.assertEqual(exit_code(r, REQUIRED), 2)
        filled = fill_missing(r, REQUIRED)
        self.assertEqual(filled["system.video"].state, UNVERIFIED)


class Gate(unittest.TestCase):
    def test_clean_batch_is_eligible(self):
        ok, why = eligibility(all_pass(), gate())
        self.assertTrue(ok, why)

    def test_not_batch_mode(self):
        for mode in ("quick", "calibrate", "baseline", "control"):
            ok, why = eligibility(all_pass(), gate(mode=mode))
            self.assertFalse(ok)

    def test_only_skip(self):
        self.assertFalse(eligibility(all_pass(), gate(full_set=False))[0])

    def test_experiment_and_low_ram(self):
        self.assertFalse(eligibility(all_pass(), gate(experiment=True))[0])
        self.assertFalse(eligibility(all_pass(), gate(experiment=None))[0])
        self.assertFalse(eligibility(all_pass(), gate(low_ram="true"))[0])

    def test_controls_not_qualified(self):
        ok, why = eligibility(all_pass(), gate(controls_qualified=False))
        self.assertFalse(ok)
        self.assertTrue(any("controls not qualified" in w for w in why))

    def test_helper_non_terminal(self):
        r = all_pass()
        r["radios.wifi"].helper_terminal = False
        self.assertFalse(eligibility(r, gate())[0])

    def test_unverified_blocks(self):
        self.assertFalse(eligibility(all_pass(final__boot_id=UNVERIFIED), gate())[0])
        self.assertFalse(eligibility(all_pass(identity__build_id=FAIL), gate())[0])

    def test_low_ram_rule(self):
        self.assertEqual(low_ram_check("true", False)[0], FAIL)
        self.assertEqual(low_ram_check("true", True)[0], PASS)
        self.assertEqual(low_ram_check("", False)[0], PASS)
        self.assertEqual(low_ram_check("false", None)[0], UNVERIFIED)

    def test_low_ram_uses_android_boolean_semantics(self):
        # system/libbase/parsebool.cpp: 1, y, yes, on, true all mean enabled.
        for v in ("1", "y", "yes", "on", "true", " true\n"):
            self.assertEqual(low_ram_check(v, False)[0], FAIL, v)
            self.assertFalse(eligibility(all_pass(), gate(low_ram=v))[0], v)
        for v in ("", "0", "n", "no", "off", "false"):
            self.assertEqual(low_ram_check(v, False)[0], PASS, v)
        self.assertIsNone(parse_bool("TRUE"))  # parsebool is case-sensitive
        self.assertEqual(low_ram_check("TRUE", False)[0], UNVERIFIED)

    def test_failed_low_ram_read_is_not_unset(self):
        self.assertEqual(low_ram_check(None, False)[0], UNVERIFIED)
        ok, why = eligibility(all_pass(), gate(low_ram=None))
        self.assertFalse(ok)
        self.assertIn("ro.config.low_ram could not be read", why)

    def test_only_batch_kind_merges(self):
        for kind in ("baseline", "calibration", "experiment", None):
            ok, why = eligibility(all_pass(), gate(kind=kind))
            self.assertFalse(ok, kind)

    def test_helper_terminal_must_be_true(self):
        r = all_pass()
        r["system.gestures"].helper_terminal = None   # a PASS that never said it ran the helper
        self.assertFalse(eligibility(r, gate())[0])

    def test_thresholds_and_calibration_runs(self):
        self.assertFalse(eligibility(all_pass(), gate(thresholds_calibrated=False))[0])
        ok, why = eligibility(all_pass(), gate(calibration_runs=4))
        self.assertFalse(ok)
        self.assertTrue(any("4/5 clean --calibrate runs" in w for w in why))


if __name__ == "__main__":
    unittest.main()
