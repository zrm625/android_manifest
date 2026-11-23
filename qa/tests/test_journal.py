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
        good = FakeShell({r"^pm clear": (0, "Success\n"), r"^dumpsys alarm$": (0, "Alarm Stats:\n")})
        self.assertEqual(self.restore(good, "alarm", "river-qa-1", where=j.RESOURCE)[::2], (True, 0))
        failed = FakeShell({r"^pm clear": (1, "Failed\n"), r"^dumpsys alarm$": (0, "")})
        self.assertEqual(self.restore(failed, "alarm", "river-qa-1", where=j.RESOURCE)[::2], (False, 1))
        still = FakeShell({r"^pm clear": (0, "Success\n"),
                           r"^dumpsys alarm$": (0, "RTC_WAKEUP #0: Alarm{1 com.android.deskclock}\n")})
        self.assertEqual(self.restore(still, "alarm", "river-qa-1", where=j.RESOURCE)[::2], (False, 1))
        unreadable = FakeShell({r"^pm clear": (0, "Success\n"), r"^dumpsys alarm$": (1, "")})
        self.assertFalse(self.restore(unreadable, "alarm", "river-qa-1", where=j.RESOURCE)[0])

    def test_ntp_is_verified(self):
        good = FakeShell({r"dumpsys network_time_update_service": (0, "mServerConfig=ntp://time.android.com\n")})
        self.assertTrue(self.restore(good, "ntp", "ntp")[0])
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
                                           lambda cmd, timeout: unittest.mock.Mock(stdout="Paired: no\n")):
            run = FakeRun(dev, d)
            e = run.journal.record(j.RESOURCE, "bt_bond", "AA:BB", None, extra={"phone_addr": "CC:DD"})
            ok, _ = restore.restore_entry(run, e)
            self.assertTrue(ok)
            self.assertEqual(run.journal.entries, [])   # its own policy entry was restored too
        self.assertEqual(seen, ["1"])
        self.assertEqual(state["policy"], "null")

    def test_recovery_reports_anything_left(self):
        dev = FakeShell({r"^pm clear": (1, "Failed\n")})
        with tempfile.TemporaryDirectory() as d:
            run = FakeRun(dev, d)
            run.journal.record(j.RESOURCE, "alarm", "river-qa-1", None)
            done, drop, failed = restore.recover_journal(run)
        self.assertTrue(failed)


if __name__ == "__main__":
    unittest.main()
