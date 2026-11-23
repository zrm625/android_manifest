"""Put qa/lib on sys.path (tests run with python3 -I, which drops the script dir)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
