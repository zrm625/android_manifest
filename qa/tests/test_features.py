"""Pure pieces of the feature checks (A7-A13). No adb."""
import json
import os
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import _path  # noqa: F401
from riverqa import features as F
from riverqa.results import FAIL, PASS

BATCH = {"prs": [{"repo": "zrm625/android_device_motorola_river", "number": 12}]}
URL = "https://github.com/zrm625/android_device_motorola_river/pull/12#issuecomment-555"


def gh(login="zrm625", body="approve camera set: 0,2", parent=None, rc=0):
    def run(cmd):
        data = {"user": {"login": login}, "body": body,
                "issue_url": parent or "https://api.github.com/repos/zrm625/android_device_motorola_river/issues/12",
                "pull_request_url": parent or
                "https://api.github.com/repos/zrm625/android_device_motorola_river/pulls/12"}
        return subprocess.CompletedProcess(cmd, rc, json.dumps(data), "")
    return run


class CameraApproval(unittest.TestCase):
    def ok(self, change, runner=None, batch=BATCH):
        return F.verify_camera_change(change, batch, runner or gh())[0]

    def test_approved(self):
        self.assertTrue(self.ok({"url": URL, "set": [2, 0]}))
        review = "https://github.com/zrm625/android_device_motorola_river/pull/12#pullrequestreview-9"
        self.assertTrue(self.ok({"url": review, "set": [0, 2]}))

    def test_url_is_anchored_and_scoped(self):
        for url in ("https://github.com/someone/x/pull/12#issuecomment-1",
                    "https://evil.example/https://github.com/zrm625/a/pull/12#issuecomment-1",
                    URL + "/extra", "https://github.com/zrm625/android_device_motorola_river/pull/12",
                    "https://github.com/zrm625/android_device_motorola_river/issues/12#issuecomment-555"):
            self.assertFalse(self.ok({"url": url, "set": [0, 2]}), url)

    def test_must_be_this_batchs_pr(self):
        other = "https://github.com/zrm625/android_device_motorola_river/pull/13#issuecomment-555"
        self.assertFalse(self.ok({"url": other, "set": [0, 2]}))

    def test_comment_author_not_pr_author(self):
        self.assertFalse(self.ok({"url": URL, "set": [0, 2]}, gh(login="some-bot")))

    def test_comment_must_belong_to_the_pr(self):
        elsewhere = "https://api.github.com/repos/zrm625/android_device_motorola_river/issues/99"
        self.assertFalse(self.ok({"url": URL, "set": [0, 2]}, gh(parent=elsewhere)))

    def test_must_name_the_new_set(self):
        self.assertFalse(self.ok({"url": URL, "set": [0, 2]}, gh(body="LGTM")))
        self.assertFalse(self.ok({"url": URL, "set": [0, 1, 2]}, gh(body="approve camera set: 0,2")))
        self.assertFalse(self.ok({"url": URL, "set": [0, 2]}, gh(rc=1)))


class Gestures(unittest.TestCase):
    def test_positive_state_required(self):
        self.assertTrue(F.overview_proxy_bound("OverviewProxyService: app.zak.perch/.Overview isConnected=true"))
        self.assertTrue(F.overview_proxy_bound("app.zak.perch mIsConnected=true"))
        self.assertTrue(F.overview_proxy_bound("app.zak.perch mIsEnabled=true"))
        self.assertFalse(F.overview_proxy_bound("app.zak.perch isConnected=false"))
        self.assertFalse(F.overview_proxy_bound("app.zak.perch Connected"))
        self.assertFalse(F.overview_proxy_bound("app.zak.perch isConnected=trueish"))
        self.assertFalse(F.overview_proxy_bound("com.android.launcher3 isConnected=true"))


class Alarm(unittest.TestCase):
    T = 1000.0

    def polls(self, *rel):
        return [(self.T + a, self.T + b, f) for a, b, f in rel]

    def test_absent_then_firing(self):
        st, _ = F.alarm_verdict(self.polls((-10.2, -9.0, False), (-1, 0.5, False), (1.0, 2.5, True)), self.T)
        self.assertEqual(st, PASS)

    def test_observation_must_finish_inside_the_deadline(self):
        # The dumpsys that showed firing finished at +10.6 s: too late.
        st, _ = F.alarm_verdict(self.polls((-10.2, -9.0, False), (9.0, 10.6, True)), self.T)
        self.assertEqual(st, FAIL)

    def test_absence_needs_a_timely_observation(self):
        st, _ = F.alarm_verdict(self.polls((-12.0, -10.8, False), (1.0, 2.0, True)), self.T)
        self.assertEqual(st, FAIL)   # started before target - 10.5 s
        st, _ = F.alarm_verdict(self.polls((-6.0, -4.0, False), (1.0, 2.0, True)), self.T)
        self.assertEqual(st, FAIL)   # finished after target - 5 s

    def test_early_firing_fails(self):
        st, _ = F.alarm_verdict(self.polls((-13.0, -11.0, True), (-10.2, -9.0, True)), self.T)
        self.assertEqual(st, FAIL)

    def test_alarm_checked_one_minute_early_fails(self):
        # The control shifts the target 60 s early: nothing fires by then.
        eff = self.T - 60
        polls = [(eff + a, eff + a + 1.2, False) for a in range(-14, 12, 2)]
        self.assertEqual(F.alarm_verdict(polls, eff)[0], FAIL)


class Usb(unittest.TestCase):
    def test_interface_symlinks_are_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path(d) / "devices/pci0/usb8/8-1"
            (real / "8-1:1.0").mkdir(parents=True)
            (real / "serial").write_text("ZY225DPT7F\n")
            (real / "8-1:1.0/bInterfaceClass").write_text("06\n")
            (real / "8-1:1.0/bInterfaceSubClass").write_text("01\n")
            bus = Path(d) / "bus"
            bus.mkdir()
            os.symlink(real, bus / "8-1")
            os.symlink(real / "8-1:1.0", bus / "8-1:1.0")
            self.assertEqual(F.mtp_interface_count(bus), ["ZY225DPT7F"])


class Sideload(unittest.TestCase):
    def full(self):
        sl = {k: {"install": True, "launch": True, "uninstall": True} for k in F.SIDELOAD_KEYS}
        sl["helper"].pop("launch")
        return sl

    def test_all_four(self):
        self.assertEqual(F.sideload_result(self.full()).state, PASS)

    def test_missing_app_fails(self):
        sl = self.full()
        del sl["t3code"]
        self.assertEqual(F.sideload_result(sl).state, FAIL)
        sl = self.full()
        sl["brave"]["launch"] = False
        self.assertEqual(F.sideload_result(sl).state, FAIL)


class Time(unittest.TestCase):
    DUMP = """TimeDetectorStrategy:
  Telephony suggestion history:
    TelephonyTimeSuggestion{mElapsedRealtimeMillis=900000}
  Network suggestion history:
    NetworkTimeSuggestion{mElapsedRealtimeMillis=1000}
  Gnss suggestion history:
    GnssTimeSuggestion{mElapsedRealtimeMillis=800000}
"""

    def test_only_the_network_section(self):
        self.assertEqual(F.network_suggestion_section(self.DUMP).strip(),
                         "NetworkTimeSuggestion{mElapsedRealtimeMillis=1000}")
        self.assertFalse(F.network_suggestion_newer(self.DUMP, 5000))   # GNSS/telephony don't count
        self.assertTrue(F.network_suggestion_newer(self.DUMP, 1000))
        self.assertFalse(F.network_suggestion_newer("nothing here", 0))


class Brave(unittest.TestCase):
    def nodes(self, *texts):
        root = ET.fromstring("<hierarchy>" + "".join(f'<node text="{t}" content-desc=""/>' for t in texts) +
                             "</hierarchy>")
        return list(root.iter("node"))

    def test_url_bar_token_is_not_enough(self):
        tok = "0123456789abcdef0123"
        self.assertFalse(F.brave_rendered(self.nodes(f"127.0.0.1:47800/page/{tok}", tok), tok))
        self.assertTrue(F.brave_rendered(self.nodes(F.rendered_token(tok)), tok))

    def test_page_script_renders_it(self):
        page = (Path(_path.__file__).resolve().parent.parent / "fixtures" / "page.html").read_text()
        self.assertIn('"js-" + token.split("").reverse().join("")', page)
        self.assertEqual(F.rendered_token("abc"), "js-cba")


if __name__ == "__main__":
    unittest.main()
