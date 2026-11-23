import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from riverqa import journal as j


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


if __name__ == "__main__":
    unittest.main()
