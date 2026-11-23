"""Planned reboots with the boot signals attached.

Every planned reboot:
1. writes a leaving marker to /dev/kmsg (as root, several copies);
2. records the USB disconnect after the harness's reboot command. On the
   fastboot path the uptime clock starts at the disconnect after the final
   ``fastboot reboot``, so time spent writing images never counts. On the adb
   path it starts at the old kernel's reset, mapped onto host time through
   the marker's own printk timestamp (amendment A1′, judge_record);
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
from .core import append_jsonl, bench_path, now_iso, read_json, read_jsonl, write_json
from .device import Device, UsbWatcher, fastboot_ids_seen
from .results import FAIL, PASS, UNVERIFIED, worst

TIMING_FILE = "qa/boot-timing.json"
TAIL_SAMPLES_FILE = "qa/shutdown-tails.jsonl"
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


def load_tail_samples() -> list[dict]:
    """The first-adb-reboot-after-wipe shutdown tails (amendment A1′ rule
    4), one per wipe (the first recorded for a wipe wins), in the order
    they were recorded. They accumulate across `phone-qa boot-timing` runs
    and the first bench boot of baseline and batch flashes."""
    out, seen = [], set()
    for r in read_jsonl(bench_path(TAIL_SAMPLES_FILE)):
        if r.get("wipe_seq") in seen or r.get("tail_s") is None:
            continue
        seen.add(r.get("wipe_seq"))
        out.append(r)
    return out


def add_tail_sample(rec: dict, wipe: dict, source: str, build: str = "") -> bool:
    """Record a boot's shutdown tail as wipe ``wipe``'s sample, if the boot
    (``rec``, BootRecord.to_json()) got a reset anchor ignoring any bound
    and the wipe has no sample yet. The caller has proven the boot is the
    first adb reboot after that wipe (calibrate.first_after_wipe). True if
    a sample was added."""
    if rec.get("reset_tail_s") is None or rec.get("path") != "adb":
        return False
    if any(r.get("wipe_seq") == wipe["seq"] for r in load_tail_samples()):
        return False
    append_jsonl(bench_path(TAIL_SAMPLES_FILE), {
        "wipe_seq": wipe["seq"], "wipe_how": wipe.get("how"), "tail_s": rec["reset_tail_s"],
        "source": source, "build": build, "boot_id_before": rec.get("boot_id_before"),
        "marker": rec.get("marker"), "at": now_iso()})
    return True


def chain_survives(path: str, timing: dict | None = None) -> bool | None:
    timing = load_timing() if timing is None else timing
    return ((timing.get("chain") or {}).get(path) or {}).get("survives")


MAX_PAIR_CMD_S = 0.3   # the kmsg write must bracket within this, or the pairing is too loose


@dataclass
class BootRecord:
    path: str
    marker: str | None
    boot_id_before: str | None
    boot_id: str = ""
    uptime: float = 0.0
    elapsed: float = 0.0                # host seconds from the clock start to the status read
    host_boot_seconds: float = 0.0      # reboot command (after any image writes) to boot completed
    bootloader_pass: float = 0.0        # elapsed - uptime
    control: str | None = None
    usb_events: list = field(default_factory=list)
    planned_fastboot: int = 0
    unplanned_fastboot: int = 0
    chain: tuple = ("UNVERIFIED", "")
    uptime_check: tuple = ("UNVERIFIED", "")
    completed_wall: float = 0.0
    # Amendment A1: where the clock starts, and what it is paired with.
    clock_source: str = "disconnect"    # "disconnect", "reset-anchor" or "none"
    marker_pair: dict | None = None     # {"marker", "printk_ts", "host_mono", "half_width"}
    disconnect_mono: float = 0.0        # the recorded disconnect event the clock (or bound) uses
    completed_mono: float = 0.0
    elapsed_disconnect: float = 0.0
    anchor: dict | None = None
    anchor_note: str = ""
    reset_tail_s: float | None = None   # disconnect to reset, from an anchor judged with no bound (A1′ rule 4)
    pstore_read_error: str = ""         # non-empty: the checked pstore read failed (review round 2)

    def to_json(self):
        d = asdict(self)
        d["chain"] = list(self.chain)
        d["uptime_check"] = list(self.uptime_check)
        return d


def control_active() -> str | None:
    """The control override in force, if any (RIVERQA_CONTROL)."""
    return os.environ.get("RIVERQA_CONTROL") or None


def leave_marker(dev: Device) -> tuple[str | None, str | None, dict | None]:
    """Write a leaving marker and pair it with its own printk timestamp:
    (marker, boot_id, pair).

    Each attempt writes a new marker with its own nonce, and its write
    status is checked (review round 2). A failed write is never paired and
    never becomes the leaving marker, and a retry can't pick up an older
    write's printk time, because the readback looks for this attempt's
    nonce only. The host brackets the write with time.monotonic(); a write
    slower than MAX_PAIR_CMD_S is retried. pair is None if no write could
    be bracketed tightly and read back. marker is the last verified write,
    or None if adb root failed or no write succeeded (the chain is then
    UNVERIFIED for this boot)."""
    try:
        dev.root()
        bid = dev.boot_id()
    except (TimeoutError, RuntimeError):
        return None, None, None
    marker = pair = None
    for _ in range(3):
        m = bootsig.make_marker(bid)
        t0 = time.monotonic()
        try:
            ok = dev.kmsg(m) is True
        except (TimeoutError, RuntimeError):
            ok = False
        t1 = time.monotonic()
        if not ok:
            continue
        marker = m
        if t1 - t0 > MAX_PAIR_CMD_S:
            continue
        try:
            r = dev.read(f"dmesg | grep -F {m}", timeout=20)
        except (TimeoutError, RuntimeError):
            break
        ts = bootsig.marker_printk_stamps(r.out, m) if r.ok else []
        if ts:
            # Every copy's stamp: the boot judge matches the one intact pstore
            # record against these to the microsecond (review round 3).
            pair = {"marker": m, "printk_ts": ts[-1], "printk_ts_all": ts,
                    "host_mono": (t0 + t1) / 2, "half_width": (t1 - t0) / 2}
        break
    return marker, bid, pair


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
                   already_marked: tuple | None = None,
                   watcher: UsbWatcher | None = None) -> BootRecord:
    """Reboot the phone along ``path`` and wait for boot completion.

    ``in_fastboot``: callback run while the phone sits in fastboot (flash
    writes images here); only for the fastboot path.
    ``inject_extra_pass``: control mode only. With ``mark_planned`` false the
    injected pass's fastboot enumerations are not marked planned, so the
    unplanned-enumeration rule must catch it (the second extra-pass control).
    The uptime verdict comes later, from judge_signals().
    """
    if path not in PATHS:
        raise ValueError(path)
    if (inject_extra_pass or inject_early_reboot) and control_active() is None:
        raise RuntimeError("faults may only be injected in control mode")
    own_watcher = watcher is None
    w = watcher or UsbWatcher(dev.serial).start()
    try:
        if already_marked:
            marker, bid, pair = (tuple(already_marked) + (None,))[:3]
        elif dev.state() == "device":
            marker, bid, pair = leave_marker(dev)
        else:
            marker, bid, pair = None, None, None
        rec = BootRecord(path=path, marker=marker, boot_id_before=bid, marker_pair=pair,
                         control="extra-pass" if inject_extra_pass else None)
        start_mark = w.mark()
        t_cmd = time.monotonic()
        planned = 0
        if path == "adb":
            if inject_extra_pass:
                dev.reboot_adb("bootloader")
                clock = w.wait_gone(start_mark, 120)        # the injected pass begins
                dev.wait_fastboot(120)
                planned += 1
                mark2 = w.mark()
                dev.reboot_fastboot()
                final = w.wait_gone(mark2, 60)
            else:
                dev.reboot_adb()
                clock = final = w.wait_gone(start_mark, 120)
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
        st = dev.wait_boot_completed(boot_timeout)
        rec.completed_wall = st.host_wall
        rec.boot_id, rec.uptime = st.boot_id, st.uptime
        rec.disconnect_mono, rec.completed_mono = clock.t, st.host_mono
        rec.elapsed = rec.elapsed_disconnect = st.host_mono - clock.t
        rec.host_boot_seconds = round(st.host_mono - t_cmd, 2)
        rec.bootloader_pass = round(rec.elapsed - rec.uptime, 3)
        rec.usb_events = [asdict(e) for e in w.since(start_mark)]
        rec.unplanned_fastboot = len(fastboot_ids_seen(_after(w, start_mark, window_from)))
        rec.planned_fastboot = planned
        return rec
    finally:
        if own_watcher:
            w.stop()


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


def tail_bound(rec: BootRecord, timing: dict) -> tuple[float, float] | None:
    """(disconnect, disconnect + max_shutdown_tail) for the adb path, or
    None if calibration recorded no bound (fewer than
    bootsig.MIN_TAIL_SAMPLES first-after-wipe tails)."""
    tail = ((timing.get("paths") or {}).get(rec.path) or {}).get("max_shutdown_tail")
    return None if tail is None else (rec.disconnect_mono, rec.disconnect_mono + float(tail))


def judge_signals(dev: Device, rec: BootRecord, timing: dict | None = None, *, measuring: bool = False) -> None:
    """Fill in the chain and uptime verdicts (reads pstore as root)."""
    from .evidence import read_pstore
    timing = load_timing() if timing is None else timing
    try:
        dev.root()
        ok, log, why = read_pstore(dev, "console-ramoops*")
    except (TimeoutError, RuntimeError) as e:
        ok, log, why = False, b"", str(e)
    rec.pstore_read_error = "" if ok else (why or "pstore read failed")
    judge_record(rec, log if ok else None, timing, measuring=measuring)


def judge_record(rec: BootRecord, log: bytes | None, timing: dict, *, measuring: bool = False) -> None:
    """The verdicts for one boot from its pstore log. Pure.

    The fastboot path keeps the disconnect clock. The adb path starts the
    clock at the old kernel's reset (amendment A1′): only if this boot's
    chain is PASS, the marker write was paired with its printk stamps, a
    marker copy is proven by its stamp and the log's final lines give a
    reset (bootsig.reset_anchor), and the anchor falls inside the
    calibrated shutdown-tail bound. Otherwise its uptime signal is
    UNVERIFIED; it never falls back to the disconnect.
    rec.reset_tail_s records the tail of an anchor judged with no bound, so
    a first-after-wipe boot can add its sample even when it is out of bound.
    ``measuring``: step 1 before anything is calibrated (no tail bound)."""
    rec.chain = bootsig.chain_verdict(log, rec.marker, rec.marker_pair)
    if rec.pstore_read_error:
        rec.chain = (UNVERIFIED, f"pstore read failed: {rec.pstore_read_error}")
    allowance = allowance_for(rec.path, timing)
    if rec.path != "adb":
        rec.clock_source = "disconnect"
        rec.uptime_check = bootsig.uptime_verdict(rec.uptime, rec.elapsed_disconnect, allowance,
                                                  rec.unplanned_fastboot)
        return
    anchor, why = None, ""
    rec.reset_tail_s = None
    if rec.chain[0] != PASS:
        why = f"chain is {rec.chain[0]}, so there is no reset anchor"
    elif not rec.marker_pair:
        why = "the leaving marker was not paired with its printk timestamp"
    else:
        anchor, why = bootsig.reset_anchor(log, rec.marker, rec.marker_pair)
        if anchor is not None:
            rec.reset_tail_s = round(anchor["host_mono"] - rec.disconnect_mono, 3)
            if not measuring:
                bound = tail_bound(rec, timing)
                if bound is None:
                    anchor, why = None, ("no calibrated shutdown-tail bound for the adb path (needs "
                                         f"{bootsig.MIN_TAIL_SAMPLES} first-adb-reboot-after-wipe samples)")
                else:
                    out = bootsig.check_tail_bound(anchor, bound)
                    if out:
                        anchor, why = None, out
    rec.anchor_note = why
    if anchor is None:
        rec.clock_source = "none"
        rec.anchor = None
        rec.uptime_check = ((FAIL, f"{rec.unplanned_fastboot} unplanned fastboot enumeration(s): extra boot")
                            if rec.unplanned_fastboot else (UNVERIFIED, f"adb path: {why}"))
        return
    rec.clock_source = "reset-anchor"
    anchor["tail_s"] = rec.reset_tail_s
    rec.anchor = anchor
    rec.elapsed = rec.completed_mono - anchor["host_mono"]
    rec.bootloader_pass = round(rec.elapsed - rec.uptime, 3)
    rec.uptime_check = bootsig.uptime_verdict(rec.uptime, rec.elapsed, allowance, rec.unplanned_fastboot)


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
