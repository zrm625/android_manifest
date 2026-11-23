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
from riverqa.results import FAIL, PASS, UNVERIFIED

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


def proxy_dump(connected="true", enabled="true", bound="true", comp="app.zak.perch/.QuickstepService"):
    return (f"OverviewProxyService state:\n  isConnected={connected}\n  mIsEnabled={enabled}\n"
            f"  mRecentsComponentName=ComponentInfo{{{comp}}}\n"
            f"  mQuickStepIntent=Intent {{ act=android.intent.action.QUICKSTEP_SERVICE pkg={comp.split('/')[0]} }}\n"
            f"  mBound={bound}\n  mCurrentBoundedUserId=0\n")


class Gestures(unittest.TestCase):
    def test_connected_and_bound_to_perch(self):
        self.assertTrue(F.overview_proxy_bound(proxy_dump()))

    def test_enabled_alone_is_rejected(self):
        # SystemUI derives mIsEnabled from resolving the service, not from a
        # connection (review round 2).
        self.assertFalse(F.overview_proxy_bound(proxy_dump(connected="false", enabled="true", bound="false")))
        self.assertFalse(F.overview_proxy_bound(proxy_dump(connected="true", bound="false")))
        self.assertFalse(F.overview_proxy_bound(proxy_dump(connected="false", bound="true")))

    def test_other_component_or_loose_text_is_rejected(self):
        self.assertFalse(F.overview_proxy_bound(proxy_dump(comp="com.android.launcher3/.QuickstepService")))
        self.assertFalse(F.overview_proxy_bound("app.zak.perch isConnected=true mBound=true"))
        self.assertFalse(F.overview_proxy_bound(proxy_dump(connected="trueish")))

    def test_exact_package_only(self):
        # Review round 3: a package that merely starts with app.zak.perch is
        # not Perch.
        self.assertFalse(F.overview_proxy_bound(proxy_dump(comp="app.zak.perch.fake/.QuickstepService")))
        self.assertFalse(F.overview_proxy_bound(proxy_dump(comp="app.zak.perchx/.QuickstepService")))
        self.assertTrue(F.overview_proxy_bound(proxy_dump(comp="app.zak.perch/app.zak.perch.QuickstepService")))
        self.assertEqual(F.component_package("ComponentInfo{app.zak.perch.fake/.X}"), "app.zak.perch.fake")


NOTIF = "Current Notification Manager state:\n"
ALARM_RECORD = ("    NotificationRecord(0x1: pkg=com.android.deskclock user=UserHandle{0} id=1 "
                "tag=null importance=4 key=0|x: Notification(channel=alarmFiringNotificationChannel ...))\n"
                "      fullscreenIntent=PendingIntent{1: x}\n      river-qa-ab\n      when=0\n"
                "      mCreationTimeMs=1000000123456\n")


class Alarm(unittest.TestCase):
    T = 1000.0

    def polls(self, *rel):
        return [(self.T + r[0], self.T + r[1], r[2], None if len(r) < 4 else self.T + r[3]) for r in rel]

    def test_absent_then_firing(self):
        st, _ = F.alarm_verdict(self.polls((-11.2, -10.0, False), (-1, 0.5, False), (1.0, 2.5, True)), self.T)
        self.assertEqual(st, PASS)

    def test_observation_must_finish_inside_the_deadline(self):
        # The dumpsys that showed firing finished at +10.6 s: never PASS.
        st, _ = F.alarm_verdict(self.polls((-11.2, -10.0, False), (9.0, 10.6, True)), self.T)
        self.assertNotEqual(st, PASS)
        st, _ = F.alarm_verdict(self.polls((-11.2, -10.0, False), (8.0, 9.0, False), (9.5, 10.6, True)), self.T)
        self.assertNotEqual(st, PASS)
        # Still absent after +10 s: it fired late.
        st, _ = F.alarm_verdict(self.polls((-11.2, -10.0, False), (10.2, 11.0, False), (11.2, 12.0, True)), self.T)
        self.assertEqual(st, FAIL)

    def test_sol_firing_poll_spanning_minus_10(self):
        # Review round 3: absent at -15 s, then the first firing poll spans
        # -11 s .. -9 s. It may have fired at -10.5 s: never PASS.
        st, why = F.alarm_verdict(self.polls((-15.5, -15.0, False), (-11.0, -9.0, True)), self.T)
        self.assertEqual(st, UNVERIFIED, why)
        # With the notification's event time the answer is known.
        st, _ = F.alarm_verdict(self.polls((-15.5, -15.0, False), (-11.0, -9.0, True, -10.5)), self.T)
        self.assertEqual(st, FAIL)
        st, _ = F.alarm_verdict(self.polls((-15.5, -15.0, False), (-11.0, -9.0, True, -9.5)), self.T)
        self.assertEqual(st, PASS)

    def test_event_time_must_fall_inside_its_observation(self):
        st, _ = F.alarm_verdict(self.polls((-11.2, -10.0, False), (-1.0, 0.0, False), (1.0, 2.0, True, 30.0)),
                                self.T)
        self.assertEqual(st, UNVERIFIED)
        st, _ = F.alarm_verdict(self.polls((-11.2, -10.0, False), (-1.0, 0.0, False), (1.0, 2.0, True, 0.4)),
                                self.T)
        self.assertEqual(st, PASS)

    def test_event_time_is_parsed_from_the_record(self):
        dump = NOTIF + "  Notification List:\n" + ALARM_RECORD + "  \n  mArchive=Archive (1 notification)\n"
        self.assertEqual(F.alarm_firing_event(dump, "river-qa-ab"), (True, 1000000123.456))
        self.assertEqual(F.alarm_firing_event(dump.replace("mCreationTimeMs=1000000123456", "when=99"),
                                              "river-qa-ab"), (True, None))

    def test_only_a_posted_record_counts(self):
        """Review round 4: a record in "Enqueued Notification List" was never
        posted, so it is no evidence that the alarm fired."""
        other = ("    NotificationRecord(0x2: pkg=android user=UserHandle{0} id=9 tag=null importance=2 key=0|y: "
                 "Notification(channel=misc))\n      uid=1000 userId=0\n")
        enqueued_only = (NOTIF + "  Notification List:\n" + other + "  \n  mMaxPackageEnqueueRate=5.0\n"
                         "  hideSilentStatusBar=false\n  mArchive=Archive (0 notifications)\n"
                         "  Enqueued Notification List:\n" + ALARM_RECORD + "  \n")
        self.assertEqual(F.alarm_firing_event(enqueued_only, "river-qa-ab"), (False, None))
        nothing_posted = NOTIF + "  mArchive=Archive (0)\n  Enqueued Notification List:\n" + ALARM_RECORD
        self.assertEqual(F.alarm_firing_event(nothing_posted, "river-qa-ab"), (False, None))
        sample = F.parse_alarm_sample(0, f"1.0\n{enqueued_only}__DUMP_RC__0\n2.0\n", "river-qa-ab")
        self.assertEqual(sample, (1.0, 2.0, False, None))
        # Enqueued in the window, never posted: the alarm check can't PASS.
        st, _ = F.alarm_verdict(self.polls((-11.2, -10.0, False), (1.0, 2.0, False), (3.0, 4.0, False)), self.T)
        self.assertNotEqual(st, PASS)
        # Posted (and also still listed as enqueued): it counts.
        both = enqueued_only.replace("  Notification List:\n" + other, "  Notification List:\n" + other + ALARM_RECORD)
        self.assertEqual(F.alarm_firing_event(both, "river-qa-ab"), (True, 1000000123.456))

    def test_absence_must_be_at_or_before_minus_10(self):
        st, _ = F.alarm_verdict(self.polls((-10.2, -9.0, False), (1.0, 2.0, True)), self.T)
        self.assertEqual(st, FAIL)   # finished after target - 10 s
        st, _ = F.alarm_verdict(self.polls((-6.0, -4.0, False), (1.0, 2.0, True)), self.T)
        self.assertEqual(st, FAIL)
        st, _ = F.alarm_verdict(self.polls((-18.0, -16.8, False), (1.0, 2.0, True)), self.T)
        self.assertEqual(st, FAIL)   # started before target - 16 s

    def test_unreadable_sample_is_no_evidence(self):
        # The only sample in the absence window failed: it can't stand for absence.
        st, _ = F.alarm_verdict(self.polls((-11.2, -10.0, None), (1.0, 2.0, True)), self.T)
        self.assertEqual(st, FAIL)
        # Nor for firing.
        st, _ = F.alarm_verdict(self.polls((-11.2, -10.0, False), (1.0, 2.0, None)), self.T)
        self.assertEqual(st, FAIL)
        # A failed sample between two good ones changes nothing...
        st, _ = F.alarm_verdict(self.polls((-13.0, -12.0, False), (-11.2, -10.0, None), (-1.0, 0.0, False),
                                           (1.0, 2.0, True)), self.T)
        self.assertEqual(st, PASS)
        # ...but it can't narrow the firing interval: absent at -13 s and
        # firing by +2 s spans target - 10 s (review round 3).
        st, _ = F.alarm_verdict(self.polls((-13.0, -12.0, False), (-11.2, -10.0, None), (1.0, 2.0, True)), self.T)
        self.assertEqual(st, UNVERIFIED)

    def test_each_sample_carries_the_dump_status(self):
        ok = F.parse_alarm_sample(0, f"1.0\n{NOTIF}nothing\n__DUMP_RC__0\n2.0\n", "lbl")
        self.assertEqual(ok, (1.0, 2.0, False, None))
        # dumpsys failed but the trailing date succeeded: no evidence.
        self.assertEqual(F.parse_alarm_sample(0, "1.0\nCan't find service\n__DUMP_RC__1\n2.0\n", "lbl"),
                         (1.0, 2.0, None, None))
        # Exit 0 but not a notification dump (cut off, or another service).
        self.assertEqual(F.parse_alarm_sample(0, "1.0\n\n__DUMP_RC__0\n2.0\n", "lbl"), (1.0, 2.0, None, None))
        self.assertIsNone(F.parse_alarm_sample(1, "", "lbl"))
        self.assertIsNone(F.parse_alarm_sample(0, "x\n__DUMP_RC__0\n2.0\n", "lbl"))

    def test_early_firing_fails(self):
        st, _ = F.alarm_verdict(self.polls((-13.0, -11.0, True), (-10.2, -9.0, True)), self.T)
        self.assertEqual(st, FAIL)

    def test_alarm_checked_one_minute_early_fails(self):
        # The control shifts the target 60 s early: nothing fires by then.
        eff = self.T - 60
        polls = [(eff + a, eff + a + 1.2, False) for a in range(-16, 12, 2)]
        self.assertEqual(F.alarm_verdict(polls, eff)[0], FAIL)


class SmallItems(unittest.TestCase):
    """Review round 2, section 9."""

    def test_approval_line_does_not_span_lines(self):
        self.assertEqual(F.APPROVAL_LINE_RE.findall("approve camera set: 0,1,2\n"), ["0,1,2"])
        self.assertEqual(F.APPROVAL_LINE_RE.findall("x\r\napprove camera set: 0, 2 \r\nmore"), ["0, 2"])
        # The set on the next line, or a second line of digits, must not count.
        self.assertEqual(F.APPROVAL_LINE_RE.findall("approve camera set:\n0,1,2\n"), [])
        self.assertEqual(F.APPROVAL_LINE_RE.findall("approve camera set: 0,1\n2\n"), ["0,1"])
        self.assertEqual(F.APPROVAL_LINE_RE.findall("\n\napprove camera set: 0\n"), ["0"])

    def test_usb_reconnect_timeout_stays_a_fail(self):
        self.assertTrue(F.usb_reconnect_failed(True, "device"))
        self.assertTrue(F.usb_reconnect_failed(False, ""))
        self.assertFalse(F.usb_reconnect_failed(False, "device"))

    def test_vpn_dialog_alone_is_not_a_launch(self):
        self.assertFalse(F.vpn_launch_ok(("com.android.vpndialogs/.ConfirmDialog", True), "com.tailscale.ipn"))
        self.assertTrue(F.vpn_launch_ok(("com.tailscale.ipn/.MainActivity", True), "com.tailscale.ipn"))
        self.assertFalse(F.vpn_launch_ok(("com.tailscale.ipnx/.Main", True), "com.tailscale.ipn"))
        # Review round 3: on top but not resumed is not launched.
        self.assertFalse(F.vpn_launch_ok(("com.tailscale.ipn/.MainActivity", False), "com.tailscale.ipn"))

    def test_resumed_exact_package_rule(self):
        from riverqa import helper
        dump = ("TASK 10 id=1\n  ACTIVITY app.zak.perch/.MainActivity 1a pid=100\n    mResumed=false\n"
                "TASK 10 id=2\n  ACTIVITY app.zak.perch.fake/.MainActivity 2b pid=200\n    mResumed=true\n")
        top = helper.resumed_top(dump)
        self.assertEqual(top, ("app.zak.perch.fake/.MainActivity", True))
        for pkg in ("app.zak.perch", "com.brave.browser", "com.t3tools.t3code", "com.android.deskclock"):
            self.assertFalse(helper.activity_is(top, pkg), pkg)
        self.assertFalse(helper.activity_is(("app.zak.perch/.MainActivity", False), "app.zak.perch"))
        self.assertTrue(helper.activity_is(("app.zak.perch/.MainActivity", True), "app.zak.perch", "MainActivity"))
        self.assertTrue(helper.activity_is(("app.zak.perch/app.zak.perch.MainActivity", True), "app.zak.perch",
                                           "MainActivity"))
        self.assertFalse(helper.activity_is(("app.zak.perch/.MainActivityX", True), "app.zak.perch", "MainActivity"))
        self.assertFalse(helper.activity_is(("com.android.deskclockx/.DeskClock", True), "com.android.deskclock"))

    def test_helper_foreground_needs_the_exact_package(self):
        from fakes import FakeShell
        from riverqa import helper
        for comp, want in (("river.qa/.MainActivity", True), ("river.qa.evil/.MainActivity", False)):
            dev = FakeShell({r"dumpsys activity top": (0, f"TASK 1 id=1\n  ACTIVITY {comp} 1 pid=1\n"
                                                          "    mResumed=true\n"),
                             r"dumpsys power": (0, "mWakefulness=Awake\n")})
            self.assertEqual(helper.foreground(dev)[0], want, comp)

    def test_build_counts_untracked_files_as_dirty(self):
        build = Path(__file__).resolve().parents[2] / "scripts" / "build"
        line = next(l for l in build.read_text().splitlines() if l.startswith("dirty()"))
        self.assertNotIn("-uno", line)
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
            for cmd in (["git", "init", "-q", d], ["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t",
                                                   "commit", "-q", "--allow-empty", "-m", "x"]):
                subprocess.run(cmd, check=True, env=env)
            run = lambda: subprocess.run(["bash", "-c", line + '\ndirty "$1"', "x", d], capture_output=True,
                                         text=True, env=env).stdout.strip()
            self.assertEqual(run(), "")
            Path(d, "new-input.mk").write_text("x")
            self.assertEqual(run(), "+dirty")


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
