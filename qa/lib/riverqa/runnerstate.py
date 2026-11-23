"""Read the batch runner's state file from outside the runner (H4, H12).

``$BENCH_HOME/runner/state.json`` is written only by the runner, under
``runner/state.lock`` (qa/RUNNER.md, R4.1). Outside readers take that lock
shared and **fail closed**: a file that exists and does not parse counts as
"a batch may be running".

Standalone standard library, so the shell scripts can run it:

    python3 -I qa/lib/riverqa/runnerstate.py    # exit 1 while running or lease is set (or unreadable)
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path


class Unreadable(RuntimeError):
    pass


def runner_dir(home: Path | None = None) -> Path:
    home = home if home is not None else Path(os.environ.get("BENCH_HOME", "/mnt/storage/lineageos-bench"))
    return home / "runner"


def read_shared(home: Path | None = None) -> dict | None:
    """The state file, read under its lock taken shared (blocking briefly),
    or None if it does not exist. Raises Unreadable if it exists and does
    not parse."""
    d = runner_dir(home)
    path = d / "state.json"
    if not path.exists():
        return None
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(d / "state.lock", os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        raise Unreadable(f"cannot open {d / 'state.lock'}: {e}") from e
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise Unreadable(f"{path} does not parse: {e}") from e
        if not isinstance(data, dict):
            raise Unreadable(f"{path} is not a JSON object")
        return data
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def busy_reason(home: Path | None = None) -> str | None:
    """Why a tool that moves a base or the tree must wait, or None (H4)."""
    try:
        st = read_shared(home)
    except Unreadable as e:
        return f"the batch runner's state file is unreadable ({e}); refusing"
    if not st:
        return None
    run = st.get("running")
    if run:
        return f"batch {run.get('id')} is running (batch status)"
    if st.get("lease"):
        return f"batch {st['lease'].get('batch')} holds a tree lease (batch status)"
    return None


def running_id(home: Path | None = None) -> str | None:
    """running.id, or None. Raises Unreadable (fail closed)."""
    st = read_shared(home)
    return ((st or {}).get("running") or {}).get("id")


def main(argv: list[str]) -> int:
    if argv:
        print("usage: runnerstate.py   (exit 1 while a batch runs or holds a tree lease)")
        return 2
    why = busy_reason()
    if why:
        print(f"error: {why}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
