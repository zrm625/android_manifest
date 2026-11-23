"""Calibration step 1: boot timing (DESIGN.md, "Calibration and bootstrap").
It flashes nothing itself, but it runs straight after `flash --wipe`, so its
first adb reboot is the first adb reboot after a wipe (F1, compute_paths).

For each reboot path (a plain ``adb reboot``, and a reboot through fastboot
with no writes) this measures the normal bootloader pass (host time from the
USB disconnect to kernel start), sets the allowance A with the rule in
bootsig.compute_allowance, then proves on fresh reboots that:

- a normal boot passes the uptime check;
- an injected extra bootloader pass, with its enumerations marked planned, is
  rejected by the uptime check itself;
- the same injection without the planned marking is rejected by the
  unplanned-enumeration rule.

"One more bootloader pass" is the smaller of the flash path's normal pass
(reset to kernel start) and the measured increment of an injected pass; see
run() for why.

Amendment A1: on the adb path the clock starts at the old kernel's reset
(reboots.judge_record), so only reboots whose chain passed and whose anchor
could be mapped count. Their shutdown tails (disconnect to reset) set
max_shutdown_tail, the bound every later adb-path anchor must fall inside;
it is set only if the first adb reboot after the wipe is among them.

`phone-qa controls --boot-timing` (prove_main) runs only the proofs again,
with the recorded A and bound, so they can be renewed under the final
harness version.

It also records whether the kernel-log chain survives each path and how
often ramoops corrupts the marker. Results: $BENCH_HOME/qa/boot-timing.json.
The proofs are part of the control set (controls.json).
"""

from __future__ import annotations

import argparse
import os
import statistics
import time

from . import bootsig, reboots
from .core import SERIAL, log, now_iso, write_json, bench_path
from .device import Device, UsbWatcher
from .lock import DeviceLock, LockError
from .results import FAIL, PASS


def one(dev: Device, w: UsbWatcher, path: str, timing: dict, *, inject=False, mark_planned=True,
        count_before: int | None = None, measuring: bool = False) -> dict:
    if inject:
        os.environ["RIVERQA_CONTROL"] = "boot-timing-extra-pass"
    try:
        rec = reboots.planned_reboot(dev, path, boot_timeout=600, inject_extra_pass=inject,
                                     mark_planned=mark_planned, watcher=w)
    finally:
        if inject:
            os.environ.pop("RIVERQA_CONTROL", None)
    reboots.judge_signals(dev, rec, timing, measuring=measuring)
    from .evidence import read_pstore
    _ok, log_bytes, _why = read_pstore(dev, "console-ramoops*")
    exact = log_bytes.count(rec.marker.encode()) if rec.marker else 0
    fuzzy = bootsig.find_marker(log_bytes, rec.marker) is not None if rec.marker else False
    intact = bootsig.marker_record(log_bytes, rec.marker) is not None if rec.marker else False
    d = rec.to_json()
    d["marker_copies_exact"] = exact
    d["marker_found_fuzzy"] = fuzzy
    d["marker_record_intact"] = intact
    d["pstore_bytes"] = len(log_bytes)
    d["pstore_last_ts"] = bootsig.last_timestamp(log_bytes)
    if count_before is not None:
        st, msg = reboots.read_boot_count(dev, count_before + 1, settle_s=90)
        d["boot_count"] = [st, msg]
    tail_note = "" if not rec.anchor else " (tail %.2f s)" % rec.anchor["tail_s"]
    log(f"  {path:8} {'inject' if inject else 'normal'}{'' if mark_planned else ' unmarked'}: "
        f"boot {rec.host_boot_seconds:.1f} s, uptime {rec.uptime:.1f} s, elapsed {rec.elapsed:.2f} s, "
        f"pass {rec.bootloader_pass:.2f} s, fastboot planned {rec.planned_fastboot} unplanned "
        f"{rec.unplanned_fastboot}, chain {rec.chain[0]} ({exact} exact copies), clock {rec.clock_source}"
        f"{tail_note}, uptime-check {rec.uptime_check[0]}"
        f"{'' if rec.clock_source != 'none' else ' (' + rec.anchor_note + ')'}")
    ids = sorted({e['ids'] for e in d['usb_events'] if e['kind'] == 'present'})
    log(f"           usb ids seen: {ids}")
    return d


def usable(path: str, r: dict) -> bool:
    """A step-1 reboot that can set A: on the adb path only one whose clock
    started at the reset anchor."""
    return path != "adb" or r.get("clock_source") == "reset-anchor"


def boot_count_now(dev: Device, settle_s: float = 60) -> int:
    """SYSTEM_BOOT entries in dropbox once the count has settled (it is
    written after BOOT_COMPLETED, so a settled count is at least 1; review
    round 3). At the deadline, whatever was last read (possibly 0)."""
    end = time.monotonic() + settle_s
    last = None
    while True:
        n = bootsig.dropbox_tags(dev.sh("dumpsys dropbox", timeout=60)).count("SYSTEM_BOOT")
        if (n >= 1 and n == last) or time.monotonic() > end:
            return n
        last = n
        time.sleep(3)


def first_after_wipe(path: str, index: int, boot_id_before: str | None, boots_before: int,
                     wipe: dict | None) -> tuple[bool, str]:
    """Is this step-1 reboot the first adb reboot after a recorded wipe
    (review round 3)? Only if it is step 1's first reboot, on the adb path,
    from the boot the flash record names as the wipe's first boot (so no
    other boot came between), and dropbox holds exactly that one boot."""
    if path != "adb" or index != 0:
        return False, "not step 1's first adb reboot"
    if not wipe:
        return False, "no wipe recorded"
    if not wipe.get("first_boot_id"):
        return False, f"wipe #{wipe.get('seq')} has no recorded first boot"
    if boot_id_before != wipe["first_boot_id"]:
        return False, (f"rebooted from boot {str(boot_id_before)[:8]}, not wipe #{wipe['seq']}'s first boot "
                       f"{wipe['first_boot_id'][:8]}")
    if boots_before != 1:
        return False, f"dropbox holds {boots_before} SYSTEM_BOOT entries, not 1"
    return True, f"the first adb reboot after wipe #{wipe['seq']} ({wipe.get('how')})"


def compute_paths(data: dict) -> None:
    """Set A, the shutdown-tail bound and the chain record for each path
    from data["raw"] and data["inject_measure"]. Pure.

    F1 (review round 2): the tail bound must include the first adb reboot
    after a wipe, which is every batch's first bench boot and has the
    longest shutdown tail (20.6 s disconnect-clocked on baseline-1). Step 1
    runs straight after `flash --wipe`, so its first adb reboot is that
    reboot (raw records carry ``first_after_wipe``). Without an anchored one
    there is no bound, and every adb-path uptime signal stays UNVERIFIED."""
    passes = {p: [r["bootloader_pass"] for r in data["raw"][p] if usable(p, r)] for p in reboots.PATHS}
    adb_ok = [r for r in data["raw"]["adb"] if usable("adb", r)]
    tails = [r["anchor"]["tail_s"] for r in adb_ok]
    wipe_tails = [r["anchor"]["tail_s"] for r in adb_ok if r.get("first_after_wipe")]
    # "One more bootloader pass": the flash path's normal pass (reset to
    # kernel start, DESIGN.md round-10 note) or the measured increment of
    # an injected pass, whichever is smaller. On river the injected pass
    # (reset to fastboot and on) adds about 4 s, far less than the 10 s
    # reset-to-kernel pass, so the smaller value is the one that lets the
    # uptime check reject the control.
    flash_pass = min(passes["fastboot"]) if passes["fastboot"] else None
    for path in reboots.PATHS:
        raws = data["raw"][path]
        chains = [r["chain"][0] for r in raws]
        data["chain"][path] = {"survives": bool(chains) and all(c == PASS for c in chains), "verdicts": chains,
                               "exact_copies": [r.get("marker_copies_exact") for r in raws],
                               "fuzzy_found": [r.get("marker_found_fuzzy") for r in raws]}
        inj = data["inject_measure"].get(path) or {}
        empty = {"A": None, "normal_passes": passes[path], "proof_normal_pass": False,
                 "proof_control_rejected": False, "proof_unmarked_rejected": False}
        if not passes[path] or flash_pass is None or not usable(path, inj):
            notes = sorted({r.get("anchor_note") or "" for r in raws + [inj] if not usable(path, r)} - {""})
            why = (f"no usable {path} reboots" if not passes[path] else
                   "no usable fastboot reboots, so no flash-path pass" if flash_pass is None else
                   "the injected-pass measurement got no reset anchor")
            if path == "adb" and notes:
                why += " (reset anchor: " + "; ".join(notes[:4]) + ")"
            data["paths"][path] = dict(empty, why=why)
            continue
        inc = inj["bootloader_pass"] - min(passes[path])
        extra = min(flash_pass, inc)
        a_val, why = bootsig.compute_allowance(passes[path], extra)
        data["paths"][path] = dict(empty, A=a_val, why=why, extra_pass=round(extra, 3),
                                   extra_pass_flash_path=flash_pass, extra_pass_injected_increment=round(inc, 3),
                                   mean=round(statistics.mean(passes[path]), 3))
        if path == "adb":
            p = data["paths"][path]
            p["clock"] = "reset-anchor"
            p["shutdown_tails"] = tails
            p["first_after_wipe_tails"] = wipe_tails
            # The consecutive-stamp gap bound for the clean-tail rule
            # (bootsig.clean_tail, review round 3): the largest gap any
            # anchored step-1 tail had, plus a margin.
            gaps = [r["anchor"].get("max_gap") for r in adb_ok]
            p["stamp_gaps"] = gaps
            p["max_stamp_gap"] = (round(max(gaps) + bootsig.GAP_MARGIN_S, 3)
                                  if gaps and all(g is not None for g in gaps) else None)
            if wipe_tails:
                p["max_shutdown_tail"] = round(max(tails), 3)
            else:
                p["max_shutdown_tail"] = None
                p["tail_why"] = ("no anchored first adb reboot after a wipe: run `flash --wipe` and then "
                                 "`phone-qa boot-timing` straight away")


def prove(dev: Device, w: UsbWatcher, data: dict) -> None:
    """The step-1 proofs on fresh reboots, judged with the recorded A and
    tail bound. allowance_for() only uses A once both proofs are recorded."""
    for path in reboots.PATHS:
        p = data["paths"].get(path) or {}
        if p.get("A") is None or (path == "adb" and (p.get("max_shutdown_tail") is None
                                                       or p.get("max_stamp_gap") is None)):
            continue
        proof_timing = {"paths": {path: dict(p, proof_normal_pass=True, proof_control_rejected=True)}}
        normal = one(dev, w, path, proof_timing)
        p["proof_normal"] = normal
        p["proof_normal_pass"] = normal["uptime_check"][0] == PASS
        ctrl = one(dev, w, path, proof_timing, inject=True, mark_planned=True)
        p["proof_control"] = ctrl
        p["proof_control_rejected"] = (ctrl["uptime_check"][0] == FAIL and ctrl["unplanned_fastboot"] == 0)
        unm = one(dev, w, path, proof_timing, inject=True, mark_planned=False)
        p["proof_unmarked"] = unm
        p["proof_unmarked_rejected"] = unm["unplanned_fastboot"] > 0 and unm["uptime_check"][0] == FAIL
        p["proved_at"] = now_iso()
        data["controls"][f"boot-timing-{path}-extra-pass"] = {
            "expected": "FAIL (uptime)", "got": ctrl["uptime_check"], "qualified": p["proof_control_rejected"]}
        data["controls"][f"boot-timing-{path}-extra-pass-unmarked"] = {
            "expected": "FAIL (unplanned enumeration)", "got": unm["uptime_check"],
            "qualified": p["proof_unmarked_rejected"]}
        reboots.save_timing(data)


def summary(data: dict) -> list[str]:
    """The report lines, also for a path that got no A (the recorded reason
    is printed, never a KeyError)."""
    out = ["Boot-timing calibration:"]
    for path in reboots.PATHS:
        p = data.get("paths", {}).get(path) or {}
        chain = (data.get("chain", {}).get(path) or {}).get("survives")
        if p.get("A") is None:
            out.append(f"  {path:8} not qualified: {p.get('why') or 'not measured'}; chain survives: {chain}")
            continue
        tail = ""
        if path == "adb":
            tail = (f"; shutdown-tail bound {p.get('max_shutdown_tail')} s" if p.get("max_shutdown_tail") is not None
                    else f"; no shutdown-tail bound ({p.get('tail_why')})")
            tail += (f"; stamp-gap bound {p.get('max_stamp_gap')} s" if p.get("max_stamp_gap") is not None
                     else "; no stamp-gap bound")
        out.append(f"  {path:8} A={p['A']} ({p.get('why')}); extra pass {p.get('extra_pass')} s (flash path "
                   f"{p.get('extra_pass_flash_path')} s, injected increment {p.get('extra_pass_injected_increment')} s)"
                   f"{tail}; normal boot passes: {p.get('proof_normal_pass')}; extra pass rejected by uptime: "
                   f"{p.get('proof_control_rejected')}; unmarked rejected by enumeration rule: "
                   f"{p.get('proof_unmarked_rejected')}; chain survives: {chain}")
    return out


def qualified(data: dict) -> bool:
    """Every path has all three proofs: a normal boot passed, the marked
    extra pass was rejected by the uptime check and the unmarked one by the
    enumeration rule (review round 3)."""
    return all(all((data.get("paths", {}).get(p) or {}).get(k)
                   for k in ("proof_normal_pass", "proof_control_rejected", "proof_unmarked_rejected"))
               for p in reboots.PATHS)


def boot_timing_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="phone-qa boot-timing")
    ap.add_argument("--normal", type=int, default=4, help="normal reboots per path to set A (default 4)")
    ap.add_argument("--reuse-normals", action="store_true",
                    help="reuse the normal reboots in boot-timing.json if they were measured on this build")
    a = ap.parse_args(argv)
    try:
        with DeviceLock():
            return run(a.normal, a.reuse_normals)
    except LockError as e:
        log(f"error: {e}")
        return 1


def prove_main() -> int:
    """phone-qa controls --boot-timing: run the step-1 proofs again with the
    recorded A and tail bound, and record the four boot-timing controls
    under the harness version that results. Run it after the last change to
    the harness, the thresholds or the seed, and before the other controls
    (README, "Order of qualification")."""
    try:
        with DeviceLock():
            data = reboots.load_timing()
            if not data.get("paths"):
                log("error: no boot-timing.json; run `phone-qa boot-timing` first")
                return 2
            dev = Device(SERIAL)
            dev.guard()
            dev.root()
            data.setdefault("controls", {})
            w = UsbWatcher(dev.serial).start()
            try:
                prove(dev, w, data)
            finally:
                w.stop()
            return finish(data)
    except LockError as e:
        log(f"error: {e}")
        return 1


def finish(data: dict) -> int:
    reboots.save_timing(data)
    from .controls import record_step1
    record_step1(data.get("controls") or {})
    log("")
    for line in summary(data):
        log(line)
    log(f"Saved {bench_path('qa', 'boot-timing.json')}")
    return 0 if qualified(data) else 2


def run(n_normal: int, reuse: bool = False) -> int:
    dev = Device(SERIAL)
    dev.guard()
    dev.root()
    old = reboots.load_timing()
    data = {"measured_at": now_iso(), "build": dev.getprop("ro.lineage.display.version"),
            "build_date_utc": dev.getprop("ro.build.date.utc"), "raw": {p: [] for p in reboots.PATHS},
            "inject_measure": {}, "paths": {}, "chain": {}, "controls": {}}
    timing_none = {}  # no allowance while measuring
    if reuse and old.get("build_date_utc") == data["build_date_utc"] and all(old.get("raw", {}).get(p)
                                                                             for p in reboots.PATHS):
        data["raw"] = old["raw"]
        data["reused_normals_from"] = old.get("measured_at")
        # A flag set before review round 3 (dropbox count only) is not
        # trusted: it carries no tie to the flash record.
        for r in data["raw"].get("adb") or []:
            if not r.get("first_after_wipe_why"):
                r["first_after_wipe"] = False
        log(f"Reusing {sum(len(v) for v in old['raw'].values())} normal reboots from {old.get('measured_at')}")
    from .flashing import qa_home
    from .journal import last_wipe
    wipe = last_wipe(qa_home())
    data["wipe"] = wipe
    w = UsbWatcher(dev.serial).start()
    try:
        if not data.get("reused_normals_from"):
            for i in range(n_normal):
                for path in reboots.PATHS:
                    before = boot_count_now(dev)
                    r = one(dev, w, path, timing_none, count_before=before, measuring=True)
                    # The first adb reboot after a wipe, tied to the flash
                    # record's first boot after it (F1, review round 3).
                    r["boots_before"] = before
                    r["first_after_wipe"], r["first_after_wipe_why"] = first_after_wipe(
                        path, i, r.get("boot_id_before"), before, wipe)
                    data["raw"][path].append(r)
                    write_json(bench_path("qa", "boot-timing.partial.json"), data)
        # One injected extra pass per path, to measure how long an extra
        # bootloader pass really is on this phone (separate from the proofs).
        for path in reboots.PATHS:
            data["inject_measure"][path] = one(dev, w, path, timing_none, inject=True, mark_planned=True,
                                               measuring=True)
            write_json(bench_path("qa", "boot-timing.partial.json"), data)
        compute_paths(data)
        reboots.save_timing(data)
        prove(dev, w, data)
    finally:
        w.stop()
    return finish(data)
