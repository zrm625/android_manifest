"""Planned reboots with the boot signals attached.

Three reboot paths (amendment A2):

- ``adb``: a normal ``adb reboot``, with a clean Android shutdown. Only
  bench boot 1 uses it; the charter's boot time comes from it. river's USB
  gadget drops seconds before the reset, so the disconnect can't start an
  uptime clock: its uptime is INFO only, and resets are caught by the
  kernel-log chain and the boot count.
- ``sysrq``: ``sync`` then ``echo b > /proc/sysrq-trigger`` as root, an
  immediate kernel reset. USB drops at the reset, so the uptime clock
  starts at the disconnect. Every other bench boot uses it.
- ``fastboot``: through the bootloader (flash writes images there). The
  clock starts at the disconnect after the final ``fastboot reboot``, so
  time spent writing images never counts.

Every planned reboot:
1. writes a leaving marker with a fresh 64-bit nonce to /dev/kmsg (as
   root, several copies), so the chain can tell this attempt's write from
   any other (amendment A2.1);
2. records the USB disconnect after the harness's reboot command;
3. at boot completion reads boot_id, sys.boot_completed and /proc/uptime in
   one shell command, then judges the kernel-log chain and the uptime.

The uptime clock (review round 4) runs from the earliest possible
disconnect, the start of the last USB poll that still saw the phone, to the
end of the status read, so every timing uncertainty makes elapsed longer.
A disconnect bracketed more loosely than bootsig.DISCONNECT_BOUND_S, or a
status read slower than bootsig.STATUS_READ_BOUND_S, makes the uptime gate
UNVERIFIED.

Only control mode may inject an extra bootloader pass, and only it may mark
the extra fastboot enumerations as planned.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field

from . import bootsig, fbmode
from .core import bench_path, log, read_json, write_json
from .device import Device, UsbWatcher, fastboot_ids_seen
from .results import FAIL, INFO, PASS, UNVERIFIED, worst

TIMING_FILE = "qa/boot-timing.json"
PATHS = ("adb", "sysrq", "fastboot")
GATED_PATHS = ("fastboot", "sysrq")     # the paths with an uptime gate, calibrated by step 1
PROOFS = ("proof_normal_pass", "proof_control_rejected", "proof_unmarked_rejected")


def load_timing() -> dict:
    return read_json(bench_path(TIMING_FILE), {}) or {}


def save_timing(data: dict) -> None:
    write_json(bench_path(TIMING_FILE), data)


def allowance_for(path: str, timing: dict | None = None) -> float | None:
    """A for a path, only if calibration proved it: a normal boot passed, the
    injected extra pass was rejected by the uptime check itself, and the
    unmarked one by the enumeration rule (review round 4)."""
    timing = load_timing() if timing is None else timing
    p = (timing.get("paths") or {}).get(path) or {}
    if p.get("A") is None or not all(p.get(k) for k in PROOFS):
        return None
    return float(p["A"])


def chain_survives(path: str, timing: dict | None = None) -> bool | None:
    """Step 1's record of whether the chain survives this path. Only the
    gated paths have one (review round 4): on the adb path the chain is
    bench boot 1's main signal and always counts, whatever an old file
    says. None for any other path."""
    if path not in GATED_PATHS:
        return None
    timing = load_timing() if timing is None else timing
    return ((timing.get("chain") or {}).get(path) or {}).get("survives")


@dataclass
class BootRecord:
    path: str
    marker: str | None
    boot_id_before: str | None
    boot_id: str = ""
    uptime: float = 0.0
    elapsed: float = 0.0                # host seconds from the clock's last-present poll to the end of the status read
    disconnect_gap: float | None = None  # first_gone - last_present of the clock's disconnect (review round 4)
    status_read_s: float | None = None   # latency of the status read that carried uptime (review round 4)
    host_boot_seconds: float = 0.0      # reboot command (after any image writes) to the FIRST read that saw boot completed (R5-2)
    status_reads: list = field(default_factory=list)  # [{at_s, read_s, used}] every status read after completion (R5-2)
    bootloader_pass: float = 0.0        # elapsed - uptime
    control: str | None = None
    usb_events: list = field(default_factory=list)
    planned_fastboot: int = 0
    unplanned_fastboot: int = 0
    chain: tuple = ("UNVERIFIED", "")
    uptime_check: tuple = ("UNVERIFIED", "")
    completed_wall: float = 0.0
    pstore_read_error: str = ""         # non-empty: the checked pstore read failed (review round 2)

    def to_json(self):
        d = asdict(self)
        d["chain"] = list(self.chain)
        d["uptime_check"] = list(self.uptime_check)
        return d


def control_active() -> str | None:
    """The control override in force, if any (RIVERQA_CONTROL)."""
    return os.environ.get("RIVERQA_CONTROL") or None


def leave_marker(dev: Device) -> tuple[str | None, str | None]:
    """Write a leaving marker: (marker, boot_id).

    Each attempt writes a new marker with its own nonce, and its write
    status is checked (review round 2). A failed write is retried with a new
    nonce and never becomes the leaving marker. marker is None if adb root
    failed, the boot_id read failed or was empty, or no write succeeded
    (the chain is then UNVERIFIED)."""
    try:
        dev.root()
        r = dev.read("cat /proc/sys/kernel/random/boot_id")
    except (TimeoutError, RuntimeError):
        return None, None
    # A checked read (review round 4): a failed or empty boot_id read never
    # becomes part of a marker.
    bid = r.out.strip() if r.ok else ""
    if not bid:
        return None, None
    for _ in range(3):
        m = bootsig.make_marker(bid)
        try:
            ok = dev.kmsg(m) is True
        except (TimeoutError, RuntimeError):
            ok = False
        if ok:
            return m, bid
    return None, bid


def _after(w: UsbWatcher, start_mark: int, event) -> list:
    """Every event recorded after ``event`` (D4): the window is taken from
    the recorded disconnect itself, never from an index sampled later, so an
    enumeration that lands before the sample still counts."""
    events = w.since(start_mark)
    for i, e in enumerate(events):
        if e is event:
            return events[i + 1:]
    raise RuntimeError("the disconnect event is not in the watcher's log")


def planned_reboot(dev: Device, path: str, *, boot_timeout: float, in_fastboot=None,
                   inject_extra_pass: bool = False, mark_planned: bool = True,
                   inject_early_reboot: bool = False,
                   watcher: UsbWatcher | None = None) -> BootRecord:
    """Reboot the phone along ``path`` and wait for boot completion.

    ``in_fastboot``: callback run while the phone sits in fastboot (flash
    writes images here); only for the fastboot path.
    ``inject_extra_pass``: control mode only, on a gated path. With
    ``mark_planned`` false the injected pass's fastboot enumerations are not
    marked planned, so the unplanned-enumeration rule must catch it.
    The uptime verdict comes later, from judge_signals().
    """
    if path not in PATHS:
        raise ValueError(path)
    if (inject_extra_pass or inject_early_reboot) and control_active() is None:
        raise RuntimeError("faults may only be injected in control mode")
    if inject_extra_pass and path not in GATED_PATHS:
        raise ValueError(f"the {path} path has no uptime gate, so no extra-pass control")
    own_watcher = watcher is None
    w = watcher or UsbWatcher(dev.serial).start()
    armed = owed = False
    try:
        if path == "sysrq" and inject_extra_pass:
            armed = True
            arm_bootloader_stop(dev, boot_timeout)
        marker, bid = leave_marker(dev) if dev.state() == "device" else (None, None)
        rec = BootRecord(path=path, marker=marker, boot_id_before=bid,
                         control="extra-pass" if inject_extra_pass else None)
        start_mark = w.mark()
        t_cmd = time.monotonic()
        planned = 0
        if path == "adb":
            dev.reboot_adb()
            clock = final = w.wait_gone(start_mark, 120)
        elif path == "sysrq":
            dev.reboot_sysrq()
            clock = final = w.wait_gone(start_mark, 60)      # USB drops at the reset
            if inject_extra_pass:
                dev.wait_fastboot(120)                       # fb_mode: the bootloader stops
                planned += 1
                # Clear now; resolve the obligation only once Android booted
                # (R5-1), so a host killed in between leaves it for recovery.
                owed = fbmode.clear_in_fastboot(dev)
                armed = False
                mark2 = w.mark()
                dev.reboot_fastboot()
                final = w.wait_gone(mark2, 60)
        else:
            if dev.state() == "device":
                dev.reboot_adb("bootloader")
            dev.wait_fastboot(120)
            planned += 1
            if in_fastboot is not None:
                in_fastboot()
                t_cmd = time.monotonic()  # time the boot, not the image writes
            if inject_extra_pass:
                mark2 = w.mark()
                dev.reboot_fastboot("bootloader")
                clock = w.wait_gone(mark2, 60)               # the injected pass begins
                dev.wait_fastboot(120)
                planned += 1
                mark3 = w.mark()
                dev.reboot_fastboot()
                final = w.wait_gone(mark3, 60)
            else:
                mark2 = w.mark()
                dev.reboot_fastboot()
                clock = final = w.wait_gone(mark2, 60)       # the final reboot's disconnect
        # Enumerations after this disconnect are unplanned. Unmarked control:
        # count from the injected pass's own disconnect instead.
        window_from = final
        if inject_extra_pass and not mark_planned:
            window_from = clock
            planned -= 1
            rec.control = "extra-pass-unmarked"
        if inject_early_reboot:
            rec.control = inject_reboot_before_completion(dev, boot_timeout, w)
        first, st, reads = completed_status(dev, boot_timeout)
        if owed:
            fbmode.settle()
        rec.completed_wall = st.host_wall
        rec.boot_id, rec.uptime = st.boot_id, st.uptime
        # Review round 4: the clock starts at the earliest possible
        # disconnect (the last poll that saw the phone) and stops at the end
        # of the status read, so every uncertainty makes elapsed longer.
        # judge_record bounds both brackets.
        rec.disconnect_gap = None if clock.gap is None else round(clock.gap, 3)
        rec.status_read_s = round(st.read_s, 3)
        start = clock.last_present if clock.last_present is not None else clock.t
        rec.elapsed = st.t1 - start
        # Boot time is when boot completion was first seen (R5-2): a retry
        # for a fast uptime read never moves it later.
        rec.host_boot_seconds = round(first.host_mono - t_cmd, 2)
        rec.status_reads = [{"at_s": round(r.host_mono - t_cmd, 3), "read_s": round(r.read_s, 3),
                             "used": r is st} for r in reads]
        rec.bootloader_pass = round(rec.elapsed - rec.uptime, 3)
        rec.usb_events = [asdict(e) for e in w.since(start_mark)]
        rec.unplanned_fastboot = len(fastboot_ids_seen(_after(w, start_mark, window_from)))
        rec.planned_fastboot = planned
        return rec
    finally:
        if armed:
            clear_fb_mode(dev, boot_timeout)
        if own_watcher:
            w.stop()


def completed_status(dev: Device, boot_timeout: float, tries: int = 3):
    """(first, used, reads). ``first`` is the first status read that saw
    boot completed; it alone times the boot (host_boot_seconds, R5-2). If
    it was slower than bootsig.STATUS_READ_BOUND_S, read again (up to
    ``tries`` times); ``used``, which carries the uptime evidence, is the
    first fast read of the same boot: a later read only makes elapsed
    longer, which is safe (review round 4). If no fast read comes, ``used``
    is ``first`` and the uptime gate is UNVERIFIED. ``reads`` lists every
    read that succeeded, in order."""
    first = dev.wait_boot_completed(boot_timeout)
    reads = [first]
    if first.read_s <= bootsig.STATUS_READ_BOUND_S:
        return first, first, reads
    for _ in range(tries):
        again = dev.boot_status()
        if again is None:
            continue
        reads.append(again)
        if again.completed and again.boot_id == first.boot_id and again.read_s <= bootsig.STATUS_READ_BOUND_S:
            return first, again, reads
    return first, first, reads


def arm_bootloader_stop(dev: Device, boot_timeout: float) -> None:
    """The sysrq extra-pass control, before its leaving marker: set fb_mode
    in fastboot, so the bootloader stops after the sysrq reset, then boot
    Android with ``fastboot continue`` (no reset) and wait for it. That stop
    is the injected bootloader pass."""
    if dev.state() == "device":
        dev.reboot_adb("bootloader")
    dev.wait_fastboot(120)
    fbmode.arm(dev)          # journaled before the set (review round 4)
    dev.fastboot_continue()
    dev.wait_for_device(boot_timeout)
    dev.wait_boot_completed(boot_timeout)


def clear_fb_mode(dev: Device, boot_timeout: float) -> None:
    """Best effort after a sysrq extra-pass control failed with fb_mode set:
    clear it and boot Android (fbmode.recover). If that fails too, the
    journaled obligation stays, and the next recovery under the device lock
    clears it (review round 4)."""
    try:
        fbmode.recover(dev, boot_timeout, boot=True)
    except Exception as e:  # noqa: BLE001 - say how to recover whatever went wrong
        log(f"warning: could not clear fastboot oem fb_mode ({e}); the obligation stays in the journal and "
            f"the next phone-qa, flash or boot-timing run clears it (or: phone-qa recover; by hand: fastboot "
            f"-s {dev.serial} oem fb_mode_clear && fastboot -s {dev.serial} reboot)")


def inject_reboot_before_completion(dev: Device, seconds: float, watcher: UsbWatcher | None = None) -> str:
    """Control only (extra-reboot-*): as soon as adb answers and
    sys.boot_completed is still unset, `adb reboot` (guarded), with no leaving
    marker, then wait for the phone to drop off USB, so the boot that was
    just rebooted can't be the one wait_boot_completed() sees complete
    (review round 2). Returns "early-reboot", or "early-reboot-missed" if
    boot completed first or the phone was never seen to drop off USB
    (review round 3); the caller records which in batch.json."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if dev.state() == "device":
            st = dev.boot_status()
            if st is not None:
                if st.completed:
                    return "early-reboot-missed"
                mark = watcher.mark() if watcher is not None else None
                dev.reboot_adb()
                if watcher is not None:
                    try:
                        watcher.wait_gone(mark, 120)
                    except TimeoutError:
                        # Never seen to drop off USB: the fault isn't proven
                        # to have gone in, so the control is unqualified
                        # (review round 3).
                        return "early-reboot-missed"
                return "early-reboot"
        time.sleep(0.3)
    return "early-reboot-missed"


def judge_signals(dev: Device, rec: BootRecord, timing: dict | None = None) -> None:
    """Fill in the chain and uptime verdicts (reads pstore as root)."""
    from .evidence import read_pstore
    timing = load_timing() if timing is None else timing
    try:
        dev.root()
        # A path where step 1 showed the chain does not survive may have no
        # console-ramoops at all: that is not a read error (review round 4).
        ok, data, why = read_pstore(dev, "console-ramoops*",
                                    missing_ok=chain_survives(rec.path, timing) is False)
    except (TimeoutError, RuntimeError) as e:
        ok, data, why = False, b"", str(e)
    rec.pstore_read_error = "" if ok else (why or "pstore read failed")
    judge_record(rec, data if ok else None, timing)


def judge_record(rec: BootRecord, pstore_log: bytes | None, timing: dict) -> None:
    """The verdicts for one boot from its pstore log. Pure.

    The chain needs a copy of this attempt's leaving marker
    (bootsig.chain_verdict, amendment A2.1). On the gated paths (fastboot, sysrq) the uptime
    clock starts at the recorded USB disconnect. On the adb path (bench boot
    1) uptime is INFO only (amendment A2), but an unplanned fastboot
    enumeration is still a FAIL."""
    rec.chain = bootsig.chain_verdict(pstore_log, rec.marker)
    if rec.pstore_read_error:
        rec.chain = (UNVERIFIED, f"pstore read failed: {rec.pstore_read_error}")
    if rec.path in GATED_PATHS:
        rec.uptime_check = bootsig.uptime_verdict(rec.uptime, rec.elapsed, allowance_for(rec.path, timing),
                                                  rec.unplanned_fastboot, disconnect_gap_s=rec.disconnect_gap,
                                                  status_read_s=rec.status_read_s)
    elif rec.unplanned_fastboot:
        rec.uptime_check = (FAIL, f"{rec.unplanned_fastboot} unplanned fastboot enumeration(s): extra boot")
    else:
        rec.uptime_check = (INFO, f"uptime {rec.uptime:.1f} s, {rec.elapsed:.1f} s after the USB disconnect; "
                                  "the adb path has no uptime gate (amendment A2)")


def combined_verdict(signals: dict, path: str, timing: dict | None = None) -> tuple[str, str]:
    """One verdict from the chain, uptime and boot-count signals of a boot.

    A reboot path whose chain calibration showed does not survive relies on
    the other two signals: its chain UNVERIFIED does not count, a chain FAIL
    still does.
    """
    timing = load_timing() if timing is None else timing
    states, notes = [], []
    read_err = (signals.get("reboot") or {}).get("pstore_read_error")
    if read_err:
        # A failed pstore read is never the same as a chain that doesn't
        # survive this path: the whole verdict is UNVERIFIED.
        states.append(UNVERIFIED)
        notes.append(f"pstore read failed: {read_err}")
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
    after BOOT_COMPLETED). Every read is the checked dropbox read (review
    round 4): a failed read never counts as a dump with no entries, and if
    the last read before the deadline failed the verdict is UNVERIFIED."""
    from .evidence import checked_read
    end = time.monotonic() + settle_s
    last = None
    stable = 0
    while True:
        ok, dump, why = checked_read(dev, "dropbox", "dumpsys dropbox")
        if ok:
            tags = bootsig.dropbox_tags(dump)
            n = tags.count("SYSTEM_BOOT")
            stable = stable + 1 if n == last else 0
            last = n
            if n >= expected and stable >= 2:
                return bootsig.boot_count_verdict(tags, expected)
        else:
            stable, last = 0, None
        if time.monotonic() > end:
            if not ok:
                return UNVERIFIED, f"dumpsys dropbox unreadable: {why}"
            return bootsig.boot_count_verdict(tags, expected)
        time.sleep(3)


__all__ = ["planned_reboot", "judge_signals", "combined_verdict", "read_boot_count", "BootRecord",
           "allowance_for", "load_timing", "save_timing", "PASS", "FAIL", "UNVERIFIED"]
