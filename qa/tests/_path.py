"""Put qa/lib on sys.path (tests run with python3 -I, which drops the script
dir), and point $BENCH_HOME at a throwaway directory before riverqa is
imported, so no test can ever write the real one (review round 4: a test
once journaled a fake fb_mode obligation there)."""
import os
import sys
import tempfile

if not os.environ.get("QA_TESTS_BENCH_HOME"):
    os.environ["QA_TESTS_BENCH_HOME"] = tempfile.mkdtemp(prefix="riverqa-test-bench-")
os.environ["BENCH_HOME"] = os.environ["QA_TESTS_BENCH_HOME"]
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
