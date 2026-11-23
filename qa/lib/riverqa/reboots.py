"""Planned reboots with the boot signals attached.

Every planned reboot:
1. writes a leaving marker to /dev/kmsg (as root, several copies);
2. starts the uptime clock at the first USB disconnect after the harness's
   reboot command (for the fastboot path: the disconnect after the final
   ``fastboot reboot``, so time spent writing images never counts);
3. at boot completion reads boot_id, sys.boot_completed and /proc/uptime in
   one shell command, then judges the kernel-log chain and the uptime.

Only control mode may inject an extra bootloader pass, and only it may mark
the extra fastboot enumerations as planned.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field

from . import bootsig
from .core import bench_path, read_json, write_json
from .device import Device, UsbWatcher, fastboot_ids_seen
from .results import FAIL, PASS, UNVERIFIED, worst

TIMING_FILE = "qa/boot-timing.json"
PATHS = ("adb", "fastboot")


def load_timing() -> dict:
    return read_json(bench_path(TIMING_FILE), {}) or {}


def save_timing(data: dict) -> None:
    write_json(bench_path(TIMING_FILE), data)


def allowance_for(path: str, timing: dict | None = None) -> float | None:
    """A for a path, only if calibration proved it (a normal boot passed and
    the injected extra pass was rejected by the uptime check itself)."""
    timing = load_timing() if timing is None else timing
    p = (timing.get("paths") or {}).get(path) or {}
    if p.get("A") is None or not p.get("proof_normal_pass") or not p.get("proof_control_rejected"):
        return None
    return float(p["A"])


def chain_survives(path: str, timing: dict | None = None) -> bool | None:
    timing = load_timing() if timing is None else timing
    return ((timing.get("chain") or {}).get(path) or {}).get("survives")


@dataclass
class BootRecord:
    path: str
    marker: str | None
    boot_id_before: str | None
    boot_id: str = ""
    uptime: float = 0.0
    elapsed: float = 0.0                # host seconds from clock start to the status read
    host_boot_seconds: float = 0.0      # reboot command (after any image writes) to boot completed
    bootloader_pass: float = 0.0        # elapsed - uptime
    control: str | None = None
    usb_events: list = field(default_factory=list)
    planned_fastboot: int = 0
    unplanned_fastboot: int = 0
    chain: tuple = ("UNVERIFIED", "")
    uptime_check: tuple = ("UNVERIFIED", "")
    completed_wall: float = 0.0

    def to_json(self):
        d = asdict(self)
        d["chain"] = list(self.chain)
        d["uptime_check"] = list(self.uptime_check)
        return d


def control_active() -> str | None:
    """The control override in force, if any (RIVERQA_CONTROL)."""
    return os.environ.get("RIVERQA_CONTROL") or None


def leave_marker(dev: Device) -> tuple[str | None, str | None]:
    """Write a leaving marker; returns (marker, boot_id) or (None, None) if
    adb root is not available (the chain is then UNVERIFIED for this boot)."""
    try:
        dev.root()
        bid = dev.boot_id()
        marker = bootsig.make_marker(bid)
        dev.kmsg(marker)
        return marker, bid
    except (TimeoutError, RuntimeError):
        return None, None


def planned_reboot(dev: Device, path: str, *, boot_timeout: float, in_fastboot=None,
                   inject_extra_pass: bool = False, mark_planned: bool = True,
                   already_marked: tuple | None = None,
                   watcher: UsbWatcher | None = None) -> BootRecord:
    """Reboot the phone along ``path`` and wait for boot completion.

    ``in_fastboot``: callback run while the phone sits in fastboot (flash
    writes images here); only for the fastboot path.
    ``inject_extra_pass``: control mode only. With ``mark_planned`` false the
    injected pass's fastboot enumerations are not marked planned, so the
    unplanned-enumeration rule must catch it (the second extra-pass control).
    """
    if path not in PATHS:
        raise ValueError(path)
    if inject_extra_pass and control_active() is None:
        raise RuntimeError("an extra bootloader pass may only be injected in control mode")
    own_watcher = watcher is None
    w = watcher or UsbWatcher(dev.serial).start()
    try:
        if already_marked:
            marker, bid = already_marked
        elif dev.state() == "device":
            marker, bid = leave_marker(dev)
        else:
            marker, bid = None, None
        rec = BootRecord(path=path, marker=marker, boot_id_before=bid,
                         control="extra-pass" if inject_extra_pass else None)
        start_mark = w.mark()
        t_cmd = time.monotonic()
        planned = 0
        if path == "adb":
            if inject_extra_pass:
                dev.adb("reboot", "bootloader", timeout=30)
                clock = w.wait_gone(start_mark, 120)        # the injected pass begins
                unmarked_from = w.mark()
                dev.wait_fastboot(120)
                planned += 1
                mark2 = w.mark()
                dev.fastboot_ok("reboot", timeout=60)
                w.wait_gone(mark2, 60)
            else:
                dev.adb("reboot", timeout=30)
                clock = w.wait_gone(start_mark, 120)
            after_mark = w.mark()
        else:
            if dev.state() == "device":
                dev.adb("reboot", "bootloader", timeout=30)
            dev.wait_fastboot(120)
            planned += 1
            if in_fastboot is not None:
                in_fastboot()
                t_cmd = time.monotonic()  # time the boot, not the image writes
            if inject_extra_pass:
                mark2 = w.mark()
                dev.fastboot_ok("reboot", "bootloader", timeout=60)
                clock = w.wait_gone(mark2, 60)               # the injected pass begins
                unmarked_from = w.mark()
                dev.wait_fastboot(120)
                planned += 1
                mark3 = w.mark()
                dev.fastboot_ok("reboot", timeout=60)
                w.wait_gone(mark3, 60)
            else:
                mark2 = w.mark()
                dev.fastboot_ok("reboot", timeout=60)
                clock = w.wait_gone(mark2, 60)               # the final reboot's disconnect
            after_mark = w.mark()
        if inject_extra_pass and not mark_planned:
            after_mark = unmarked_from
            planned -= 1
            rec.control = "extra-pass-unmarked"
        st = dev.wait_boot_completed(boot_timeout)
        rec.completed_wall = st.host_wall
        rec.boot_id, rec.uptime = st.boot_id, st.uptime
        rec.elapsed = st.host_mono - clock.t
        rec.host_boot_seconds = round(st.host_mono - t_cmd, 2)
        rec.bootloader_pass = round(rec.elapsed - rec.uptime, 3)
        events = w.since(start_mark)
        rec.usb_events = [asdict(e) for e in events]
        after = w.since(after_mark)
        rec.unplanned_fastboot = len(fastboot_ids_seen(after))
        rec.planned_fastboot = planned
        return rec
    finally:
        if own_watcher:
            w.stop()


def judge_signals(dev: Device, rec: BootRecord, timing: dict | None = None) -> None:
    """Fill in the chain and uptime verdicts (reads pstore as root)."""
    timing = load_timing() if timing is None else timing
    try:
        dev.root()
        log = dev.sh_bytes("cat /sys/fs/pstore/console-ramoops* 2>/dev/null", timeout=60)
    except (TimeoutError, RuntimeError):
        log = None
    rec.chain = bootsig.chain_verdict(log, rec.marker)
    rec.uptime_check = bootsig.uptime_verdict(rec.uptime, rec.elapsed, allowance_for(rec.path, timing),
                                              rec.unplanned_fastboot)


def combined_verdict(signals: dict, path: str, timing: dict | None = None) -> tuple[str, str]:
    """One verdict from the chain, uptime and boot-count signals of a boot.

    A reboot path whose chain calibration showed does not survive relies on
    the other two signals: its chain UNVERIFIED does not count, a chain FAIL
    still does.
    """
    timing = load_timing() if timing is None else timing
    states, notes = [], []
    chain = signals.get("chain") or ["UNVERIFIED", "missing"]
    if chain[0] == UNVERIFIED and chain_survives(path, timing) is False:
        notes.append(f"chain: not used on the {path} path ({chain[1]})")
    else:
        states.append(chain[0])
        notes.append(f"chain: {chain[1]}")
    up = signals.get("uptime") or ["UNVERIFIED", "missing"]
    states.append(up[0])
    notes.append(f"uptime: {up[1]}")
    count = signals.get("boot_count") or ["UNVERIFIED", "missing"]
    states.append(count[0])
    notes.append(f"boot count: {count[1]}")
    return worst(states), "; ".join(notes)


def read_boot_count(dev: Device, expected: int, settle_s: float = 60) -> tuple[str, str]:
    """Count SYSTEM_BOOT entries once the count has settled (it is written
    after BOOT_COMPLETED)."""
    end = time.monotonic() + settle_s
    last = None
    stable = 0
    while True:
        tags = bootsig.dropbox_tags(dev.sh("dumpsys dropbox", timeout=60))
        n = tags.count("SYSTEM_BOOT")
        stable = stable + 1 if n == last else 0
        last = n
        if (n >= expected and stable >= 2) or time.monotonic() > end:
            return bootsig.boot_count_verdict(tags, expected)
        time.sleep(3)


__all__ = ["planned_reboot", "judge_signals", "combined_verdict", "read_boot_count", "BootRecord",
           "allowance_for", "load_timing", "save_timing", "PASS", "FAIL", "UNVERIFIED"]
