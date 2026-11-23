"""Put qa/lib on sys.path (tests run with python3 -I, which drops the script
dir), and point $BENCH_HOME at a fresh throwaway directory before riverqa is
imported, so no test can ever write the real one (review round 4: a test
once journaled a fake fb_mode obligation there).

The directory is always new: an inherited QA_TESTS_BENCH_HOME or BENCH_HOME is
ignored (review round 5, R5-7), and the directory is removed at exit."""
import atexit
import os
import shutil
import sys
import tempfile

TEST_BENCH_HOME = tempfile.mkdtemp(prefix="riverqa-test-bench-")
atexit.register(shutil.rmtree, TEST_BENCH_HOME, True)
os.environ["QA_TESTS_BENCH_HOME"] = TEST_BENCH_HOME
os.environ["BENCH_HOME"] = TEST_BENCH_HOME
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
