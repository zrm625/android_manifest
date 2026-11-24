"""Live child records of the batch runner (H15, batch-runner design R6.2).

The runner starts every tool through its holder (qa/lib/riverbatch/hold.py),
which writes ``$BENCH_HOME/runner/children/<pid>.json`` before it lets the
tool run: ``{"cmd", "pid", "pgid", "sid", "boot_id", "start_time"}``. A
record counts as **live** only if its ``boot_id`` is this host boot and
either the pid is alive with the recorded start time (``/proc/<pid>/stat``
field 22), or, with the leader gone, some live process still has the
recorded pgid as its process group or the recorded ``sid`` (``pgid`` for an
older record) as its session: a descendant that made its own process group
(GNU ``timeout``) is still the child's (Sol SB2).
An unreadable or malformed record counts as live (fail closed). Stale
records are deleted. Only names matching ``^[0-9]+\\.json$`` are read: the
holder's ``.<pid>.tmp`` files never count.

Every lock acquisition (DeviceLock.acquire, ``acquire_build_lock`` and
``device_lock`` in scripts/common.sh) refuses, after ``flock`` and before
it writes a token, while a live record's pgid differs from the caller's
session ID: the runner's own child and its descendants share the holder's
session and pass; an orphan of a killed runner blocks everyone.

Standalone standard library, so the shell scripts can run it:

    python3 -I qa/lib/riverqa/children.py        # exit 1 and name the records if any blocks
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[0-9]+\.json$")


def children_dir(home: Path | None = None) -> Path:
    home = home if home is not None else Path(os.environ.get("BENCH_HOME", "/mnt/storage/lineageos-bench"))
    return home / "runner" / "children"


def host_boot_id(proc: Path = Path("/proc")) -> str:
    return (proc / "sys/kernel/random/boot_id").read_text().strip()


def _stat_fields(proc: Path, pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the command name, or None."""
    try:
        text = (proc / str(pid) / "stat").read_text()
    except (OSError, ValueError):
        return None
    tail = text[text.rfind(")") + 2:].split()
    return tail or None


def start_time(pid: int, proc: Path = Path("/proc")) -> str | None:
    """Field 22 of /proc/<pid>/stat (the start time in clock ticks)."""
    f = _stat_fields(proc, pid)
    # The fields after "comm" start at field 3, so field 22 is index 19.
    return f[19] if f and len(f) > 19 else None


def pgid_alive(pgid: int, proc: Path = Path("/proc"), sid: int | None = None) -> bool:
    """Some live process has process group ``pgid`` or session ``sid``."""
    try:
        names = os.listdir(proc)
    except OSError:
        return True
    for n in names:
        if not n.isdigit():
            continue
        f = _stat_fields(proc, int(n))
        # Fields 5 (pgrp) and 6 (session) are indexes 2 and 3 after comm.
        if not f or len(f) < 4 or f[0] == "Z":
            continue
        if f[2] == str(pgid) or (sid is not None and f[3] == str(sid)):
            return True
    return False


def record_live(rec: dict, boot_id: str, proc: Path = Path("/proc")) -> bool:
    if rec.get("boot_id") != boot_id:
        return False
    pid, pgid = rec.get("pid"), rec.get("pgid")
    sid = rec.get("sid", pgid)
    if not isinstance(pid, int) or not isinstance(pgid, int) or not isinstance(sid, int):
        return True
    st = start_time(pid, proc)
    if st is not None:
        if st == str(rec.get("start_time")):
            return True
        # The pid was reused by another process: only a live group or session counts.
    return pgid_alive(pgid, proc, sid)


def live_children(home: Path | None = None, proc: Path = Path("/proc"), prune: bool = True) -> list[dict]:
    """The live child records (each with ``file``). Unreadable ones are
    live; stale ones are deleted when ``prune``."""
    d = children_dir(home)
    try:
        names = sorted(n for n in os.listdir(d) if NAME_RE.match(n))
    except FileNotFoundError:
        return []
    except OSError as e:
        return [{"file": str(d), "error": f"cannot list: {e}"}]
    boot = host_boot_id(proc)
    out = []
    for n in names:
        p = d / n
        try:
            rec = json.loads(p.read_text())
            if not isinstance(rec, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:
            out.append({"file": str(p), "error": f"unreadable: {e}"})
            continue
        if record_live(rec, boot, proc):
            out.append({**rec, "file": str(p)})
        elif prune:
            try:
                p.unlink()
            except OSError:
                pass
    return out


def blocking(caller_sid: int | None = None, home: Path | None = None, proc: Path = Path("/proc")) -> list[dict]:
    """Live records whose session (pgid for an older record) is not the
    caller's session ID."""
    sid = os.getsid(0) if caller_sid is None else caller_sid
    return [r for r in live_children(home, proc) if r.get("sid", r.get("pgid")) != sid]


def describe(rec: dict) -> str:
    if "error" in rec:
        return f"{rec['file']}: {rec['error']}"
    return f"{rec['file']}: pid {rec.get('pid')} pgid {rec.get('pgid')}: {' '.join(map(str, rec.get('cmd') or []))}"


def main(argv: list[str]) -> int:
    if argv:
        print("usage: children.py   (exit 1 if a live batch-runner child of another session exists)")
        return 2
    found = blocking()
    for r in found:
        print(f"error: a batch-runner child is still running: {describe(r)}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
