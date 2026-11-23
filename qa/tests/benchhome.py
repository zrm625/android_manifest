"""A throwaway $BENCH_HOME for tests that exercise file-backed code."""
import contextlib
import os
import tempfile
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
from riverqa import core


@contextlib.contextmanager
def bench_home():
    with tempfile.TemporaryDirectory() as d, mock.patch.object(core, "BENCH_HOME", Path(d)), \
            mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BENCH_LOCK_TOKEN", None)
        yield Path(d)
