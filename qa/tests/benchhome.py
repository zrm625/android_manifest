"""A throwaway $BENCH_HOME for tests that exercise file-backed code. Every
module-level path under $BENCH_HOME, in every riverqa module, is redirected,
so nothing touches the real one (or the session's shared test home)."""
import contextlib
import importlib
import os
import pkgutil
import tempfile
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
import riverqa
from riverqa import core

# Import every module now, so a module-level copy of a path can't be created
# after the patch was applied.
MODULES = [importlib.import_module(f"riverqa.{m.name}") for m in pkgutil.iter_modules(riverqa.__path__)]
SESSION_HOME = core.BENCH_HOME


def bench_paths():
    """(module, attribute name, path) for every module-level Path under the
    session BENCH_HOME."""
    out = []
    for mod in MODULES:
        for name, val in vars(mod).items():
            if isinstance(val, Path) and (val == SESSION_HOME or SESSION_HOME in val.parents):
                out.append((mod, name, val))
    return out


@contextlib.contextmanager
def bench_home():
    with tempfile.TemporaryDirectory() as d, contextlib.ExitStack() as stack:
        home = Path(d)
        for mod, name, val in bench_paths():
            stack.enter_context(mock.patch.object(mod, name, home / val.relative_to(SESSION_HOME)))
        stack.enter_context(mock.patch.dict(os.environ, {"BENCH_HOME": str(home)}, clear=False))
        for k in [k for k in os.environ if k.startswith("RIVERQA_") or k == "BENCH_LOCK_TOKEN"]:
            os.environ.pop(k)
        yield home
