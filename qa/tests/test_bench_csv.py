import csv
import os
import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from riverqa import bench

OLD = ["run_id", "env", "boot", "host_boot_seconds", "uptime_at_boot_completed", "mem_total_kb",
       "mem_available_kb", "used_ram_kb", "processes", "packages", "system_used_kb", "vendor_used_kb",
       "cpu_idle_pct", "lineage_version", "fingerprint"]


class Migrate(unittest.TestCase):
    def test_header_migration_keeps_rows(self):
        d = Path(tempfile.mkdtemp())
        p = d / "device.csv"
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(OLD)
            w.writerow(["r1", "current", "1"] + ["x"] * (len(OLD) - 3))
        bench.migrate_header(p)
        rows = bench.read_rows(p)
        self.assertEqual(list(rows[0].keys()), bench.COLUMNS)
        self.assertEqual(rows[0]["run_id"], "r1")
        self.assertEqual(rows[0]["batch"], "")
        self.assertTrue(any(n.startswith("device.csv.bak-") for n in os.listdir(d)))
        bench.migrate_header(p)  # idempotent
        self.assertEqual(len(bench.read_rows(p)), 1)

    def test_unknown_header_refused(self):
        d = Path(tempfile.mkdtemp())
        p = d / "device.csv"
        p.write_text("a,b\n1,2\n")
        with self.assertRaises(SystemExit):
            bench.migrate_header(p)


class BootPlan(unittest.TestCase):
    """Amendment A2: bench boot 1 is adb, every later boot sysrq, and every
    batch has a sysrq boot."""

    def test_paths(self):
        self.assertEqual(bench.boot_plan(0, 3, True, None), [("adb", False), ("sysrq", False), ("sysrq", False)])
        self.assertEqual(bench.boot_plan(2, 2, True, None), [("sysrq", False), ("sysrq", False)])
        self.assertEqual(bench.boot_plan(0, 3, False, None), [("adb", False), ("sysrq", False), ("sysrq", False)])

    def test_every_batch_gets_a_sysrq_boot(self):
        self.assertEqual(bench.boot_plan(0, 1, True, None), [("adb", False), ("sysrq", False)])
        self.assertEqual(bench.boot_plan(1, 1, True, None), [("sysrq", False)])
        self.assertEqual(bench.boot_plan(0, 1, False, None), [("adb", False)])     # no batch: as asked

    def test_extra_reboot_bench_goes_into_the_first_sysrq_boot(self):
        self.assertEqual(bench.boot_plan(0, 3, True, "extra-reboot-bench"),
                         [("adb", False), ("sysrq", True), ("sysrq", False)])
        self.assertEqual(bench.boot_plan(0, 2, True, "wifi-off"), [("adb", False), ("sysrq", False)])


if __name__ == "__main__":
    unittest.main()
