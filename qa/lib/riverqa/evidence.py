"""Per-boot health evidence: collect it from the phone and judge it.

Evidence for one boot lives in a directory with:
  meta.json      boot_id read at collection, boot_id recorded at completion,
                 flash attempt or bench row key, boot reason, SELinux mode,
                 sys.boot_completed, collection time
  dmesg.txt      kernel log
  crash.txt      logcat -b crash
  dropbox.txt    dumpsys dropbox (tags only matter)
  pstore.bin     /sys/fs/pstore/console-ramoops* (the previous kernel's log)
  signals.json   the three boot signals for this boot

meta.json's ``captured`` records, for each file, whether the read succeeded
and passed its format check. judge() treats anything not captured cleanly as
UNVERIFIED: a failed read must never look like an empty, clean log.
"""

from __future__ import annotations

import re
from pathlib import Path

from .core import now_iso, read_json, write_json
from .results import FAIL, PASS, UNVERIFIED

CRASH_SIGNALS = (4, 6, 7, 8, 11)
SERVICE_SIG_RE = re.compile(r"init: Service '([^']+)' \(pid \d+\) received signal (\d+)")
STOP_RE = re.compile(r"Processed ctl\.stop for '([^']+)'")
DIED_RE = re.compile(r"init: Service '([^']+)' \(pid \d+\) (?:exited with status (\d+)|received signal (\d+))")
# First-boot allowlist. Calibration may need entries here; each one is written
# down with its reason and never applied quietly. Empty on purpose.
FIRST_BOOT_ALLOW: dict[str, str] = {}
CAPTURED_FILES = ("dmesg.txt", "crash.txt", "dropbox.txt", "pstore.bin")


def service_problems(dmesg: str) -> tuple[list[str], list[str]]:
    """(crashed services, services restarting in a loop) from dmesg.

    SIGKILL is normal for lazy HALs and is not a crash. A clean exit (status
    0) is how lazy AIDL services such as artd go idle; they are started again
    on demand, so it is not a restart either. A death right after a ctl.stop
    was asked for on purpose (Overlay Manager stops idmap2d after each use) is
    not a restart. Three or more abnormal deaths is a loop."""
    crashed = sorted({f"{m.group(1)} (signal {m.group(2)})" for m in SERVICE_SIG_RE.finditer(dmesg)
                      if int(m.group(2)) in CRASH_SIGNALS})
    stopped: dict[str, bool] = {}
    deaths: dict[str, int] = {}
    for line in dmesg.splitlines():
        m = STOP_RE.search(line)
        if m:
            stopped[m.group(1)] = True
            continue
        m = DIED_RE.search(line)
        if m:
            name = m.group(1)
            clean = m.group(2) == "0" or m.group(3) == "9"
            if stopped.get(name) or clean:
                stopped[name] = False
                continue
            deaths[name] = deaths.get(name, 0) + 1
    loops = sorted(f"{n} x{c}" for n, c in deaths.items() if c >= 3)
    return crashed, loops


def crash_processes(crash_log: str) -> list[str]:
    """Processes named in logcat -b crash (Java and native)."""
    procs = re.findall(r"Process: ([^,\s]+)", crash_log)
    procs += re.findall(r">>> (\S+) <<<", crash_log)
    procs += re.findall(r"FATAL EXCEPTION IN SYSTEM PROCESS: (\S+)", crash_log)
    if not procs and re.search(r"FATAL EXCEPTION|Fatal signal", crash_log):
        procs.append("unknown")
    return sorted(set(procs))


# Format checks for diagnostic reads (A2). Each takes (rc, text) and returns
# (ok, why). A read that fails, or doesn't look like what it should, is not
# evidence of anything.
def _rc_only(rc, text):
    return (rc == 0, "" if rc == 0 else f"exit {rc}")


def _dropbox(rc, text):
    if rc != 0:
        return False, f"exit {rc}"
    return ("Drop box contents" in text, "" if "Drop box contents" in text else "no 'Drop box contents' header")


def _dmesg(rc, text):
    if rc != 0:
        return False, f"exit {rc}"
    return (bool(text.strip()), "" if text.strip() else "empty dmesg")


def _features(rc, text):
    if rc != 0:
        return False, f"exit {rc}"
    return ("feature:" in text, "" if "feature:" in text else "no 'feature:' lines")


CHECKS = {"dropbox": _dropbox, "dmesg": _dmesg, "features": _features, "crash": _rc_only,
          "tombstones": _rc_only, "pstore": _rc_only}


def validate(kind: str, rc: int, text) -> tuple[bool, str]:
    """Is this diagnostic read usable? ``text`` may be str or bytes."""
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return CHECKS[kind](rc, text)


def checked_read(dev, kind: str, cmd: str, timeout: float = 60) -> tuple[bool, str, str]:
    """(ok, text, why) for one shell read, with its format check."""
    rc, out, err = dev.sh_rc(cmd, timeout=timeout)
    ok, why = validate(kind, rc, out)
    return ok, out, (why + (f": {err.strip()[-200:]}" if err.strip() and not ok else ""))


def read_pstore(dev, pattern: str = "*") -> tuple[bool, bytes, str]:
    """(ok, bytes, why) for /sys/fs/pstore/<pattern>, through the checked
    binary read (Device.read_files): a remote failure, a partial read or a
    failure on any one file is not a clean read (review round 2). With
    pattern "*" an empty pstore is a clean, empty read, but only once the
    directory itself was proven readable and listed (review round 3); a
    named pattern that matches nothing is not (R5-12: on every path)."""
    r = dev.read_files(f"/sys/fs/pstore/{pattern}", timeout=60)
    if pattern == "*" and r.no_match:
        return True, b"", ""
    if not r.ok:
        return False, r.data, f"reading /sys/fs/pstore/{pattern} failed: {r.why}"
    if pattern != "*" and not r.files:
        return False, b"", f"no /sys/fs/pstore/{pattern}"
    return True, r.data, ""


def collect(dev, out: Path, *, expected_boot_id: str, context: dict) -> dict:
    """Save evidence for the running boot (adb root first). Returns meta."""
    dev.root()
    out.mkdir(parents=True, exist_ok=True)
    one = dev.sh("cat /proc/sys/kernel/random/boot_id; getprop sys.boot_completed; getenforce; "
                 "getprop sys.boot.reason; getprop ro.boot.bootreason; cat /proc/uptime").split("\n")
    one += [""] * 6
    meta = {
        "boot_id_at_collection": one[0].strip(),
        "boot_id_at_completion": expected_boot_id,
        "boot_completed": one[1].strip(),
        "selinux": one[2].strip(),
        "sys_boot_reason": one[3].strip(),
        "ro_boot_bootreason": one[4].strip(),
        "uptime_at_collection": one[5].split(" ")[0].strip(),
        "collected_at": now_iso(),
        **context,
    }
    captured = {}
    for fname, kind, cmd in (("dmesg.txt", "dmesg", "dmesg"), ("crash.txt", "crash", "logcat -b crash -d"),
                             ("dropbox.txt", "dropbox", "dumpsys dropbox")):
        ok, text, why = checked_read(dev, kind, cmd)
        (out / fname).write_text(text)
        captured[fname] = {"ok": ok, "why": why}
    ok, data, why = read_pstore(dev, "console-ramoops*")
    (out / "pstore.bin").write_bytes(data)
    captured["pstore.bin"] = {"ok": ok, "why": why}
    meta["captured"] = captured
    # The boot_id must not have changed while collecting.
    meta["boot_id_after_collection"] = dev.boot_id()
    write_json(out / "meta.json", meta)
    return meta


def judge(ev_dir: Path, *, first_boot: bool = False) -> tuple[str, str, dict]:
    """Judge one boot's evidence: crashes, restart loops, SELinux, boot
    completed, and the boot_id binding. Missing evidence or a boot_id
    mismatch is UNVERIFIED."""
    meta = read_json(ev_dir / "meta.json")
    if meta is None:
        return UNVERIFIED, f"no evidence in {ev_dir}", {}
    want = meta.get("boot_id_at_completion")
    got = meta.get("boot_id_at_collection")
    after = meta.get("boot_id_after_collection")
    if not want or not got or got != want or after != want:
        return UNVERIFIED, f"boot_id mismatch (completed {want}, collected {got}, after {after})", meta
    captured = meta.get("captured")
    if not isinstance(captured, dict):
        return UNVERIFIED, "evidence has no capture record (collected by an older harness)", meta
    # The capture record is trusted only for files that are really there.
    missing = [f for f in CAPTURED_FILES if not (ev_dir / f).is_file()]
    if missing:
        return UNVERIFIED, "evidence file missing: " + ", ".join(missing), meta
    bad = [f"{f} ({(captured.get(f) or {}).get('why') or 'not recorded'})" for f in CAPTURED_FILES
           if not (captured.get(f) or {}).get("ok")]
    if bad:
        return UNVERIFIED, "not captured cleanly: " + ", ".join(bad), meta
    problems = []
    try:
        dmesg = (ev_dir / "dmesg.txt").read_text()
        crash = (ev_dir / "crash.txt").read_text()
    except FileNotFoundError as e:
        return UNVERIFIED, f"evidence file missing: {e.filename}", meta
    if meta.get("boot_completed") != "1":
        problems.append(f"sys.boot_completed={meta.get('boot_completed')!r}")
    if meta.get("selinux") != "Enforcing":
        problems.append(f"SELinux {meta.get('selinux')!r}")
    crashed, loops = service_problems(dmesg)
    procs = crash_processes(crash)
    if first_boot:
        procs = [p for p in procs if p not in FIRST_BOOT_ALLOW]
    if crashed:
        problems.append("services crashed: " + ", ".join(crashed))
    if loops:
        problems.append("restart loops: " + ", ".join(loops))
    if procs:
        problems.append("crashes: " + ", ".join(procs))
    details = {"crashed": crashed, "loops": loops, "crash_processes": procs,
               "boot_reason": meta.get("sys_boot_reason")}
    if problems:
        return FAIL, "; ".join(problems), details
    return PASS, "clean (boot_id bound, enforcing, no crashes or loops)", details
