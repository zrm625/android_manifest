import tempfile
import unittest
import unittest.mock
from pathlib import Path

import _path  # noqa: F401
from fakes import FakeShell
from riverqa import journal as j
from riverqa import restore


class Journal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_original_wins_and_persists(self):
        jr = j.Journal(self.home / "journal.json", self.home)
        jr.record(j.PHONE, "setting", "secure/x", "1", attempt_id="a1")
        jr.record(j.PHONE, "setting", "secure/x", "0", attempt_id="a1")
        again = j.Journal(self.home / "journal.json", self.home)
        self.assertEqual(len(again.entries), 1)
        self.assertEqual(again.entries[0]["original"], "1")

    def test_wipe_drops_only_older_phone_entries(self):
        jr = j.Journal(self.home / "journal.json", self.home)
        old = jr.record(j.PHONE, "setting", "secure/old", "1")
        host = jr.record(j.HOST, "adb_reverse", "47800", None)
        res = jr.record(j.RESOURCE, "alarm", "label", None)
        j.record_wipe(self.home, "flash-1", "flash")
        new = jr.record(j.PHONE, "setting", "secure/new", "2")
        restore, drop = j.plan_recovery(jr.entries, j.current_wipe_seq(self.home))
        self.assertEqual([e["id"] for e in drop], [old["id"]])
        ids = [e["id"] for e in restore]
        self.assertIn(host["id"], ids)
        self.assertIn(res["id"], ids)
        self.assertIn(new["id"], ids)
        # Resources first, then phone, then host.
        self.assertEqual(ids[0], res["id"])
        self.assertEqual(ids[-1], host["id"])

    def test_no_wipe_restores_after_reboot(self):
        jr = j.Journal(self.home / "journal.json", self.home)
        e = jr.record(j.PHONE, "location", "location", "false")
        restore, drop = j.plan_recovery(jr.entries, j.current_wipe_seq(self.home))
        self.assertEqual(drop, [])
        self.assertEqual(restore[0]["id"], e["id"])

    def test_resolve(self):
        jr = j.Journal(self.home / "journal.json", self.home)
        e = jr.record(j.HOST, "bt_discoverable", "forge", "no")
        jr.resolve(e)
        self.assertEqual(j.Journal(self.home / "journal.json", self.home).entries, [])

    def test_bad_where(self):
        jr = j.Journal(self.home / "journal.json", self.home)
        with self.assertRaises(ValueError):
            jr.record("elsewhere", "x", "y", None)


class Order(unittest.TestCase):
    def test_hidden_api_policy_after_the_bluetooth_entries(self):
        with tempfile.TemporaryDirectory() as d:
            jr = j.Journal(Path(d) / "journal.json", Path(d))
            on = jr.record(j.PHONE, "bt_enabled", "bt", "false")
            scan = jr.record(j.PHONE, "bt_scan_mode", "bt", "21")
            pol = jr.record(j.PHONE, "setting", "global/hidden_api_policy", None)
            bond = jr.record(j.RESOURCE, "bt_bond", "AA:BB", None)
            host = jr.record(j.HOST, "bt_discoverable", "forge", "no")
            order = [e["id"] for e in j.plan_recovery(jr.entries, 0)[0]]
        # Same-second timestamps: journal order breaks the tie, newest first.
        self.assertEqual(order, [bond["id"], scan["id"], on["id"], host["id"], pol["id"]])


class FakeRun:
    def __init__(self, dev, d):
        self.dev = dev
        self.journal = j.Journal(Path(d) / "journal.json", Path(d))
        self.run_id = "r1"

    def attempt_id(self):
        return None


def alarm_dump(live="", history="", past_due=""):
    """`dumpsys alarm` in the shape AlarmManagerService.dumpImpl prints it."""
    pending = f"1 pending alarms: \n  {live}\n" if live else ""
    due = f"1\n  {past_due}\n" if past_due else "(none)\n"
    return ("Current Alarm Manager state:\n  Settings:\n\n" + pending +
            "\nPending user blocked background alarms: \n  none\n\nPending alarms per uid: []\n\n"
            "Idle mode state:\n  Idling until: null\n\nPast-due non-wakeup alarms: " + due +
            "  Number of delayed alarms: 0, total delay time: 0\n\n"
            "Removal history:\n" + history + "\nAlarm Stats:\n")


class Handlers(unittest.TestCase):
    """Each restoration is verified before its entry is resolved (C4)."""

    def restore(self, dev, kind, key, original=None, where=j.PHONE):
        with tempfile.TemporaryDirectory() as d:
            run = FakeRun(dev, d)
            e = run.journal.record(where, kind, key, original)
            ok, msg = restore.restore_entry(run, e)
            left = len(run.journal.entries)
        return ok, msg, left

    def test_alarm_needs_success_and_no_alarm_left(self):
        good = FakeShell({r"^pm clear": (0, "Success\n"), r"^dumpsys alarm$": (0, alarm_dump())})
        self.assertEqual(self.restore(good, "alarm", "river-qa-1", where=j.RESOURCE)[::2], (True, 0))
        failed = FakeShell({r"^pm clear": (1, "Failed\n"), r"^dumpsys alarm$": (0, alarm_dump())})
        self.assertEqual(self.restore(failed, "alarm", "river-qa-1", where=j.RESOURCE)[::2], (False, 1))
        still = FakeShell({r"^pm clear": (0, "Success\n"),
                           r"^dumpsys alarm$": (0, alarm_dump(live="RTC_WAKEUP #0: Alarm{1 com.android.deskclock}"))})
        self.assertEqual(self.restore(still, "alarm", "river-qa-1", where=j.RESOURCE)[::2], (False, 1))
        unreadable = FakeShell({r"^pm clear": (0, "Success\n"), r"^dumpsys alarm$": (1, "")})
        self.assertFalse(self.restore(unreadable, "alarm", "river-qa-1", where=j.RESOURCE)[0])
        # Exit 0 but not an alarm dump: no proof either.
        odd = FakeShell({r"^pm clear": (0, "Success\n"), r"^dumpsys alarm$": (0, "Alarm Stats:\n")})
        self.assertFalse(self.restore(odd, "alarm", "river-qa-1", where=j.RESOURCE)[0])

    def test_past_due_alarm_is_a_leftover(self):
        # Review round 3: a DeskClock alarm waiting in the past-due
        # non-wakeup list is still live.
        dump = alarm_dump(past_due="RTC #0: Alarm{2 type 1 origWhen 1 com.android.deskclock}")
        dev = FakeShell({r"^pm clear": (0, "Success\n"), r"^dumpsys alarm$": (0, dump)})
        self.assertEqual(self.restore(dev, "alarm", "river-qa-1", where=j.RESOURCE)[::2], (False, 1))
        # A dump without the past-due section is not the expected shape.
        cut = alarm_dump().replace("Past-due non-wakeup alarms: (none)\n", "")
        self.assertIsNone(restore.live_alarms(cut))

    def test_setting_restore_uses_a_checked_read(self):
        dev = FakeShell({r"^settings get": (1, "1\n")})        # the read failed, whatever it printed
        self.assertFalse(self.restore(dev, "setting", "secure/navigation_mode", original="1")[0])
        dev = FakeShell({r"^settings get": (0, "1\n")})
        self.assertTrue(self.restore(dev, "setting", "secure/navigation_mode", original="1")[0])

    def test_helper_uninstall_needs_a_good_listing(self):
        from riverqa import helper
        for listing, want in (((1, ""), False), ((0, "package:river.qa\n"), False), ((0, ""), True),
                              ((0, "package:river.qa.other\n"), True)):
            with tempfile.TemporaryDirectory() as d:
                run = FakeRun(FakeShell({r"^pm list packages": listing}), d)
                run.journal.record(j.RESOURCE, "package", "river.qa", None)
                self.assertEqual(helper.uninstall(run), want, listing)
                self.assertEqual(run.journal.find(j.RESOURCE, "package", "river.qa") is None, want)

    def test_cleared_alarm_in_the_removal_history_does_not_block(self):
        dump = alarm_dump(history="  u0a50:\n    #1: rtc_wakeup, com.android.deskclock, removed by pm clear\n")
        dev = FakeShell({r"^pm clear": (0, "Success\n"), r"^dumpsys alarm$": (0, dump)})
        self.assertEqual(self.restore(dev, "alarm", "river-qa-1", where=j.RESOURCE)[::2], (True, 0))
        self.assertIsNone(restore.live_alarms("Removal history:\n"))

    def test_failed_reads_are_never_proof_of_absence(self):
        # pm list packages failed: the package is not proven uninstalled.
        dev = FakeShell({r"^pm list packages": (1, "")})
        self.assertEqual(self.restore(dev, "package", "com.example", where=j.RESOURCE)[::2], (False, 1))
        dev = FakeShell({r"^pm list packages": (0, "package:com.example.other\n")})
        self.assertEqual(self.restore(dev, "package", "com.example", where=j.RESOURCE)[::2], (True, 0))
        # adb reverse --list failed.
        dev = FakeShell()
        dev.adb = lambda *a, timeout=20: unittest.mock.Mock(returncode=1, stdout="")
        self.assertEqual(self.restore(dev, "adb_reverse", "8080", where=j.RESOURCE)[::2], (False, 1))
        dev.adb = lambda *a, timeout=20: unittest.mock.Mock(returncode=0, stdout="")
        self.assertEqual(self.restore(dev, "adb_reverse", "8080", where=j.RESOURCE)[::2], (True, 0))
        # Wi-Fi and Bluetooth state unreadable while restoring "off".
        dev = FakeShell({r"^cmd wifi status": (1, "")})
        with unittest.mock.patch.object(restore.time, "sleep", lambda s: None):
            self.assertFalse(self.restore(dev, "wifi", "wifi", "disabled")[0])
        dev = FakeShell({r"^dumpsys bluetooth_manager": (1, "")})
        self.assertFalse(self.restore(dev, "bt_enabled", "bt", "false")[0])
        # The marker check itself failed.
        dev = FakeShell({r"^\[ -e": (1, "")})
        self.assertFalse(self.restore(dev, "marker_file", "/data/local/tmp/x", where=j.RESOURCE)[0])
        # Host bond listing failed.
        with unittest.mock.patch.object(restore, "_needs_helper", lambda run: True), \
                unittest.mock.patch("riverqa.helper.instrument",
                                    lambda *a, **k: unittest.mock.Mock(get=lambda k, d=None: "10")), \
                unittest.mock.patch.object(restore, "host_run",
                                           lambda cmd, timeout: unittest.mock.Mock(returncode=1, stdout="")):
            with tempfile.TemporaryDirectory() as d:
                run = FakeRun(FakeShell({r"hidden_api_policy": (0, "1\n")}), d)
                e = run.journal.record(j.RESOURCE, "bt_bond", "AA:BB", None, extra={"phone_addr": "CC:DD"})
                self.assertFalse(restore.restore_entry(run, e)[0])

    def test_watchdog_unreadable_is_not_gone(self):
        from riverqa.device import BootStatus

        class Dev(FakeShell):
            def boot_status(self):
                return BootStatus("B", True, 1.0, 0.0, 0.0)
        def restore_wd(dev):
            with tempfile.TemporaryDirectory() as d:
                run = FakeRun(dev, d)
                e = run.journal.record(j.RESOURCE, "watchdog", "123", None, extra={"boot_id": "B", "start": "9"})
                return restore.restore_entry(run, e)[0]
        self.assertFalse(restore_wd(Dev({r"/proc/": (1, "")})))
        self.assertTrue(restore_wd(Dev({r"/proc/": (0, "__GONE__\n")})))

        class NoStatus(FakeShell):
            def boot_status(self):
                return None
        self.assertFalse(restore_wd(NoStatus()))

    def test_ntp_is_verified(self):
        good = FakeShell({r"dumpsys network_time_update_service":
                          (0, "mServerConfig=ntp://time.android.com\n  mNtpConfigForTests=null\n")})
        self.assertTrue(self.restore(good, "ntp", "ntp")[0])
        # The bad host is gone but a test config is still set.
        other = FakeShell({r"dumpsys network_time_update_service":
                           (0, "  mNtpConfigForTests=NtpConfig{mServerUris=[ntp://198.51.100.1]}\n")})
        self.assertFalse(self.restore(other, "ntp", "ntp")[0])
        silent = FakeShell({r"dumpsys network_time_update_service": (0, "mServerConfig=ntp://time.android.com\n")})
        self.assertFalse(self.restore(silent, "ntp", "ntp")[0])
        stuck = FakeShell({r"dumpsys network_time_update_service": (0, "server=ntp://192.0.2.1\n")})
        self.assertEqual(self.restore(stuck, "ntp", "ntp")[::2], (False, 1))
        unreadable = FakeShell({r"dumpsys network_time_update_service": (1, "")})
        self.assertFalse(self.restore(unreadable, "ntp", "ntp")[0])

    def test_bond_without_the_helper_is_not_removed(self):
        dev = FakeShell({r"pm list packages": (0, "")})
        with unittest.mock.patch.object(restore, "_needs_helper", lambda run: False), \
                unittest.mock.patch.object(restore, "host_run",
                                           lambda cmd, timeout: unittest.mock.Mock(stdout="Paired: no\n")):
            ok, msg, left = self.restore(dev, "bt_bond", "AA:BB", where=j.RESOURCE)
        self.assertFalse(ok)
        self.assertEqual(left, 1)
        self.assertIn("phone False", msg)

    def test_hidden_api_relaxed_while_the_helper_runs(self):
        seen = []
        state = {"policy": "null"}

        def settings(cmd):
            if cmd.startswith("settings put global hidden_api_policy"):
                state["policy"] = cmd.split()[-1].strip("'")
            elif cmd.startswith("settings delete global hidden_api_policy"):
                state["policy"] = "null"
            return 0, state["policy"] + "\n"
        dev = FakeShell({r"hidden_api_policy": settings})

        def instrument(run, test, args, **kw):
            seen.append(state["policy"])
            return unittest.mock.Mock(get=lambda k, d=None: "10" if k == "bond_state" else None)
        with tempfile.TemporaryDirectory() as d, \
                unittest.mock.patch.object(restore, "_needs_helper", lambda run: True), \
                unittest.mock.patch("riverqa.helper.instrument", instrument), \
                unittest.mock.patch.object(restore, "host_run",
                                           lambda cmd, timeout: unittest.mock.Mock(returncode=0, stdout="")):
            run = FakeRun(dev, d)
            e = run.journal.record(j.RESOURCE, "bt_bond", "AA:BB", None, extra={"phone_addr": "CC:DD"})
            ok, _ = restore.restore_entry(run, e)
            self.assertTrue(ok)
            self.assertEqual(run.journal.entries, [])   # its own policy entry was restored too
        self.assertEqual(seen, ["1"])
        self.assertEqual(state["policy"], "null")

    def test_recovery_rereads_the_whole_journal(self):
        # A handler leaves a new entry behind (as a failed hidden_api_policy
        # restore would): preflight must not go on.
        dev = FakeShell({r"^pm clear": (0, "Success\n"), r"^dumpsys alarm$": (0, alarm_dump())})
        with tempfile.TemporaryDirectory() as d:
            run = FakeRun(dev, d)
            run.journal.record(j.RESOURCE, "alarm", "river-qa-1", None)
            real = restore.HANDLERS["alarm"]

            def leaky(run_, e):
                run_.journal.record(j.PHONE, "setting", "global/hidden_api_policy", "0")
                return real(run_, e)
            with unittest.mock.patch.dict(restore.HANDLERS, {"alarm": leaky}):
                done, drop, failed = restore.recover_journal(run)
            self.assertTrue(any("journal not empty" in f for f in failed), failed)
            self.assertEqual(len(j.Journal(run.journal.path, run.journal.qa_home).entries), 1)

    def test_recovery_reports_anything_left(self):
        dev = FakeShell({r"^pm clear": (1, "Failed\n")})
        with tempfile.TemporaryDirectory() as d:
            run = FakeRun(dev, d)
            run.journal.record(j.RESOURCE, "alarm", "river-qa-1", None)
            done, drop, failed = restore.recover_journal(run)
        self.assertTrue(failed)


if __name__ == "__main__":
    unittest.main()
