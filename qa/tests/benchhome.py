"""A throwaway $BENCH_HOME for tests that exercise file-backed code. Every
module-level path under $BENCH_HOME is redirected, so nothing touches the
real one."""
import contextlib
import os
import tempfile
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
from riverqa import bench, controls, core, qa


@contextlib.contextmanager
def bench_home():
    with tempfile.TemporaryDirectory() as d, contextlib.ExitStack() as stack:
        home = Path(d)
        qa_home = home / "qa"
        stack.enter_context(mock.patch.object(core, "BENCH_HOME", home))
        for name, val in (("QA_HOME", qa_home), ("KNOWN_GOOD", qa_home / "known-good.json"),
                          ("CONTROLS_FILE", qa_home / "controls.json"),
                          ("CONTROL_OVERRIDE", qa_home / "control-override.json"),
                          ("HISTORY", qa_home / "history.jsonl"), ("CALIBRATION_FILE", qa_home / "calibration.json"),
                          ("BENCH_HOME", home)):
            stack.enter_context(mock.patch.object(qa, name, val))
        stack.enter_context(mock.patch.object(controls, "CONTROLS_FILE", qa_home / "controls.json"))
        stack.enter_context(mock.patch.object(bench, "BENCH_HOME", home))
        stack.enter_context(mock.patch.dict(os.environ, {}, clear=False))
        for k in [k for k in os.environ if k.startswith("RIVERQA_") or k == "BENCH_LOCK_TOKEN"]:
            os.environ.pop(k)
        yield home
