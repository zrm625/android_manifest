import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _path  # noqa: F401
from riverqa import features, reboots, trial_ram
from riverqa.device import UsbEvent


class TrialBoot(unittest.TestCase):
    def setUp(self):
        self.dev = Mock()
        self.dev.state.return_value = "device"
        self.dev.in_fastboot.return_value = False
        self.departure = UsbEvent(0, 0, "gone", None)
        self.watcher = Mock()
        self.watcher.since.side_effect = lambda start: self.events[start:]
        self.events = [UsbEvent(-1, 0, "present", "22b8:2e80"), self.departure]

    def wait(self):
        with patch.object(reboots.time, "sleep"):
            return reboots.wait_trial_boot(self.dev, self.watcher, 0, self.departure, 30, "b", "7")

    def test_first_return_to_fastboot_checks_retries(self):
        self.events.append(UsbEvent(1, 0, "present", "22b8:2e80"))
        self.dev.in_fastboot.return_value = True
        self.dev.getvar.side_effect = ["6", "_b"]
        with self.assertRaisesRegex(RuntimeError, "retry count fell from 7 to 6"):
            self.wait()
        self.dev.getvar.assert_any_call("slot-retry-count:_b", timeout=2)
        self.dev.boot_status.assert_not_called()

    def test_changed_boot_id_aborts_before_completion(self):
        self.dev.boot_status.side_effect = [
            SimpleNamespace(boot_id="first", completed=False),
            SimpleNamespace(boot_id="second", completed=False),
        ]
        with self.assertRaisesRegex(RuntimeError, "rebooted before Android finished"):
            self.wait()

    def test_usb_loop_can_be_seen_without_adb_shell(self):
        for t in (1, 4, 7):
            if t > 1:
                self.events.append(UsbEvent(t - 1, 0, "gone", None))
            self.events.append(UsbEvent(t, 0, "present", "18d1:4ee2"))
        self.dev.state.return_value = ""
        with self.assertRaisesRegex(RuntimeError, "USB repeatedly disconnected"):
            self.wait()

    def test_normal_usb_mode_change_can_finish(self):
        self.events.extend([
            UsbEvent(1, 0, "present", "18d1:4ee2"),
            UsbEvent(2, 0, "gone", None),
            UsbEvent(3, 0, "present", "18d1:4ee7"),
        ])
        complete = SimpleNamespace(boot_id="first", completed=True)
        self.dev.boot_status.side_effect = [
            SimpleNamespace(boot_id="first", completed=False), complete,
        ]
        self.assertIs(self.wait(), complete)
        self.dev.getvar.assert_not_called()


class QuickNfc(unittest.TestCase):
    def test_default_contents_missing_hal_is_failure(self):
        dev = Mock()
        dev.sh.side_effect = lambda cmd: "" if "android.hardware.nfc@1.2-service" in cmd else {
            "ls /apex": "com.android.nfcservices",
            "pm list packages com.android.apps.tag": "package:com.android.apps.tag",
        }.get(cmd, "y")
        result = features.check_quick_nfc(SimpleNamespace(dev=dev, build_record=None))
        self.assertEqual(result.state, "FAIL")
        self.assertIn("android.hardware.nfc@1.2-service", result.summary)

    def test_off_requires_matching_record_and_standalone_jar(self):
        dev = Mock()
        dev.sh.side_effect = lambda cmd: "y" if "/system/framework/framework-nfc.jar" in cmd else ""
        with tempfile.TemporaryDirectory() as temp:
            record = Path(temp) / "river-build.json"
            record.write_text(json.dumps({"build_id": "off", "nfc_enabled": False}))
            run = SimpleNamespace(dev=dev, build_record=record)
            with patch("riverqa.fbmode.partition_build_ids", return_value=("off", "default")):
                self.assertEqual(features.check_quick_nfc(run).state, "FAIL")
            with patch("riverqa.fbmode.partition_build_ids", return_value=("off", "off")):
                self.assertEqual(features.check_quick_nfc(run).details, {"nfc_enabled": False})


class TrialRam(unittest.TestCase):
    def test_nfc_off_cannot_supply_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            (out / "river-build.json").write_text(json.dumps({"build_id": "off", "nfc_enabled": False}))
            with self.assertRaisesRegex(ValueError, "NFC-enabled"):
                trial_ram.reference_kib(out)

    def test_gate_uses_settled_median_and_retains_pre_setup_median(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "trial"
            out.mkdir()
            (out / "post-setup-state.txt").write_text("build=trial\nboot_id=c\nlow_ram=false\n")
            (out / "post-setup-ram-samples-kb.txt").write_text("10200\n10300\n90000\n")
            (out / "pre-setup-boots.csv").write_text(
                "build_id,run_id,boot_id,used_ram_kb\ntrial,r,a,900\ntrial,r,b,800\ntrial,r,c,700\n")
            record = out / "river-build.json"
            record.write_text(json.dumps({"build_id": "trial", "nfc_enabled": True}))
            with patch.object(trial_ram, "reference_kib", return_value=5000):
                self.assertEqual(trial_ram.summarize(out, record, root), "fail")
            result = json.loads((out / "trial-ram.json").read_text())
            self.assertEqual(result["settled_median_kib"], 10300)
            self.assertEqual(result["pre_setup_median_kib"], 800)
            record.write_text(json.dumps({"build_id": "trial", "nfc_enabled": False}))
            with patch.object(trial_ram, "reference_kib", return_value=5000):
                self.assertEqual(trial_ram.summarize(out, record, root), "experiment")
