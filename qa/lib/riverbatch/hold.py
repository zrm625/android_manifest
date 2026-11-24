"""The holder (R6.2): runs one tool for the runner while holding its locks.

    python3 -I qa/lib/riverbatch/hold.py CHILDREN_DIR -- COMMAND [ARGS...]

The runner starts it with ``pass_fds`` = its two lock fds and
``start_new_session=True``; the holder keeps those fds until the tool and
its process group are gone, so the locks live exactly as long as the child.
The holder:

1. creates a pipe and starts a stub (``close_fds=True``, a new session, the
   environment it was given: ``BENCH_LOCK_TOKEN``, ``BENCH_BUILD_LOCK_TOKEN``,
   ``RIVER_BATCH_ID``) that blocks reading the pipe; on one byte it
   ``exec``s the real command, so pid, pgid and start time do not change;
   on EOF (the holder died) it exits 1 without ``exec``;
2. writes ``CHILDREN_DIR/<pid>.json`` (``cmd``, ``pid``, ``pgid``, host
   ``boot_id``, start time from ``/proc/<pid>/stat`` field 22) through
   ``.<pid>.tmp`` and a rename, fsyncing the file and the directory;
3. writes one byte to the pipe, releasing the stub;
4. waits for it, then SIGTERMs whatever is left in its process group,
   SIGKILLs it after 10 s, deletes the record and exits with the child's
   status. On SIGTERM, SIGINT or SIGHUP it does the same at once.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

STUB = ("import os, sys\n"
        "fd = int(sys.argv[1])\n"
        "b = os.read(fd, 1)\n"
        "if not b:\n"
        "    sys.exit(1)\n"
        "os.close(fd)\n"
        "os.execvp(sys.argv[2], sys.argv[2:])\n")
GRACE = 10.0


def proc_start(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    f = text[text.rfind(")") + 2:].split()
    return f[19] if len(f) > 19 else None


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def write_record(d: Path, rec: dict) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{rec['pid']}.tmp"
    final = d / f"{rec['pid']}.json"
    with open(tmp, "w") as f:
        json.dump(rec, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return final


def end_group(pgid: int, proc: subprocess.Popen | None) -> None:
    """SIGTERM what is left of the group, SIGKILL after GRACE, reap."""
    if group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        end = time.monotonic() + GRACE
        while group_alive(pgid) and time.monotonic() < end:
            if proc is not None:
                proc.poll()
            time.sleep(0.1)
        if group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if proc is not None:
        try:
            proc.wait(timeout=GRACE)
        except subprocess.TimeoutExpired:
            pass


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print("usage: hold.py CHILDREN_DIR -- COMMAND [ARGS...]", file=sys.stderr)
        return 2
    children = Path(argv[0])
    cmd = argv[2:]
    r, w = os.pipe()
    proc = subprocess.Popen([sys.executable, "-I", "-c", STUB, str(r), *cmd], pass_fds=(r,), close_fds=True,
                            start_new_session=True)
    os.close(r)
    pgid = proc.pid
    record = None
    stopping = {"sig": None}

    def kill_group(sig):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass

    def on_signal(signum, _frame):
        # SIGTERM the group at once, SIGKILL it after GRACE; the main thread
        # reaps the child (a handler must not wait: wait() holds the lock).
        stopping["sig"] = signum
        kill_group(signal.SIGTERM)
        signal.alarm(int(GRACE))

    signal.signal(signal.SIGALRM, lambda *_: kill_group(signal.SIGKILL))
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, on_signal)
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        record = write_record(children, {"cmd": cmd, "pid": proc.pid, "pgid": pgid, "boot_id": boot,
                                         "start_time": proc_start(proc.pid)})
        if stopping["sig"] is None:
            os.write(w, b"x")
        os.close(w)
        w = None
        rc = proc.wait()
    except BaseException:
        if w is not None:
            os.close(w)
        end_group(pgid, proc)
        raise
    finally:
        end_group(pgid, proc)
        if record is not None:
            try:
                record.unlink()
            except FileNotFoundError:
                pass
    signal.alarm(0)
    if stopping["sig"] is not None and rc == 0:
        rc = 128 + stopping["sig"]
    return 128 - rc if rc < 0 else rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
