"""No test may reach the real $BENCH_HOME (review rounds 4 and 5)."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

import _path
from benchhome import SESSION_HOME, bench_home, bench_paths
from riverqa import core, features, flashing

TESTS = Path(_path.__file__).resolve().parent
REAL = Path("/mnt/storage/lineageos-bench")


class SessionHomeTest(unittest.TestCase):
    def test_session_home_is_a_fresh_temp_dir(self):
        self.assertEqual(core.BENCH_HOME, Path(_path.TEST_BENCH_HOME))
        self.assertNotEqual(core.BENCH_HOME, REAL)
        self.assertIn("riverqa-test-bench-", core.BENCH_HOME.name)

    def test_inherited_test_home_is_ignored_and_cleaned_up(self):
        # R5-7: an inherited QA_TESTS_BENCH_HOME (here the real bench) must not be used.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import _path; "
                "from riverqa import core; print(core.BENCH_HOME)")
        env = dict(os.environ, QA_TESTS_BENCH_HOME=str(REAL), BENCH_HOME=str(REAL))
        out = subprocess.run([sys.executable, "-I", "-c", code, str(TESTS)], env=env,
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertNotEqual(Path(out), REAL)
        self.assertIn("riverqa-test-bench-", out)
        self.assertFalse(Path(out).exists(), "temp BENCH_HOME is removed at exit")


class BenchHomeTest(unittest.TestCase):
    def test_every_module_level_bench_path_is_redirected(self):
        # R5-19: features.BENCH_HOME and flashing.BENCH_HOME were left pointing at the session home.
        names = {(m.__name__, n) for m, n, _ in bench_paths()}
        self.assertIn(("riverqa.features", "BENCH_HOME"), names)
        self.assertIn(("riverqa.flashing", "BENCH_HOME"), names)
        with bench_home() as home:
            self.assertEqual(features.BENCH_HOME, home)
            self.assertEqual(flashing.BENCH_HOME, home)
            self.assertEqual(os.environ["BENCH_HOME"], str(home))
            for mod, name, _ in bench_paths():
                val = getattr(mod, name)
                self.assertTrue(val == home or home in val.parents, f"{mod.__name__}.{name} = {val}")
        self.assertEqual(features.BENCH_HOME, SESSION_HOME)


if __name__ == "__main__":
    unittest.main()
