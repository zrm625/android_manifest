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
2. writes ``CHILDREN_DIR/<pid>.json`` (``cmd``, ``pid``, ``pgid``, ``sid``,
   host ``boot_id``, start time from ``/proc/<pid>/stat`` field 22) through
   ``.<pid>.tmp`` and a rename, fsyncing the file and the directory;
3. writes one byte to the pipe, releasing the stub;
4. waits for it, then SIGTERMs whatever is left in its **session** (every
   process group in it: a descendant that made its own group, such as GNU
   ``timeout`` without ``--foreground``, is still in the session; Sol SB2),
   SIGKILLs it after 10 s, and deletes the record only once no process of
   the session is left; a session that outlives the SIGKILL keeps its record,
   so every lock stays refused until it is gone. It exits with the child's
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


def session_pids(sid: int) -> list[int]:
    """Every live (not zombie) process whose process group or session is
    ``sid``: the stub starts a new session, so its pid is both its pgid and
    its sid, and every descendant keeps that session unless it calls setsid."""
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for n in names:
        if not n.isdigit():
            continue
        try:
            text = Path(f"/proc/{n}/stat").read_text()
        except OSError:
            continue
        f = text[text.rfind(")") + 2:].split()
        # state, ppid, pgrp, session: fields 3-6, indexes 0-3 after comm
        if len(f) > 3 and f[0] != "Z" and (f[2] == str(sid) or f[3] == str(sid)):
            out.append(int(n))
    return out


def group_alive(sid: int) -> bool:
    """True while any process of the managed session (or its first group) is
    alive."""
    try:
        os.killpg(sid, 0)
        return True
    except ProcessLookupError:
        pass
    except PermissionError:
        return True
    return bool(session_pids(sid))


def signal_session(sid: int, sig: int) -> None:
    try:
        os.killpg(sid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    for pid in session_pids(sid):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


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


def end_group(sid: int, proc: subprocess.Popen | None) -> bool:
    """SIGTERM what is left of the session, SIGKILL after GRACE, reap.
    True once no process of the session is left."""
    def settle(limit: float) -> None:
        end = time.monotonic() + limit
        while group_alive(sid) and time.monotonic() < end:
            if proc is not None:
                proc.poll()
            time.sleep(0.1)
    if group_alive(sid):
        signal_session(sid, signal.SIGTERM)
        settle(GRACE)
        if group_alive(sid):
            signal_session(sid, signal.SIGKILL)
            settle(GRACE)
    if proc is not None:
        try:
            proc.wait(timeout=GRACE)
        except subprocess.TimeoutExpired:
            pass
    return not group_alive(sid)


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
        signal_session(pgid, sig)

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
        record = write_record(children, {"cmd": cmd, "pid": proc.pid, "pgid": pgid, "sid": pgid, "boot_id": boot,
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
        gone = end_group(pgid, proc)
        if record is not None and gone:
            # Sol SB2: the record (and so the interlock) stays while any
            # process of the session is alive.
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
