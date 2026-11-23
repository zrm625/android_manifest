"""Calibration step 1: boot timing (DESIGN.md, "Calibration and bootstrap",
and amendment A2). It flashes nothing itself.

For each gated reboot path (reboots.GATED_PATHS: a reboot through fastboot
with no writes, and the immediate sysrq reboot) this measures the normal
bootloader pass (host time from the USB disconnect to kernel start), sets
the allowance A with the rule in bootsig.compute_allowance, then proves on
fresh reboots that:

- a normal boot passes the uptime check;
- an injected extra bootloader pass, with its enumerations marked planned, is
  rejected by the uptime check itself;
- the same injection without the planned marking is rejected by the
  unplanned-enumeration rule.

On the sysrq path the injected pass is a bootloader stop set up with
``fastboot oem fb_mode_set`` (reboots.arm_bootloader_stop).

"One more bootloader pass" is the smaller of the flash path's normal pass
(reset to kernel start) and the measured increment of an injected pass; see
compute_paths() for why.

The adb path (bench boot 1) has no uptime gate, so it gets no A and no
extra-pass proofs. Its control is the chain: an extra full reboot injected
after ramoops init must FAIL through the kernel-log chain
(prove_adb_chain).

It also records whether the kernel-log chain survives each gated path
(every normal boot on it got a chain PASS) and how often ramoops corrupts
the marker. A path where it does not survive relies on the other two
signals (reboots.combined_verdict). The raw pstore of every reboot whose
chain did not pass is kept under $BENCH_HOME/qa/pstore-nopass/ for
diagnosis. Results: $BENCH_HOME/qa/boot-timing.json. The proofs are part of
the control set (controls.json).

`phone-qa controls --boot-timing` (prove_main) runs only the proofs again,
with the recorded A, so they can be renewed under the final harness version.
"""

from __future__ import annotations

import argparse
import os
import statistics
import time

from . import bootsig, fbmode, reboots
from .core import SERIAL, log, now_iso, write_json, bench_path
from .device import Device, GuardError, UsbWatcher
from .lock import DeviceLock, LockError
from .results import FAIL, PASS

ADB_CHAIN_CONTROL = "boot-timing-adb-extra-reboot"


def one(dev: Device, w: UsbWatcher, path: str, timing: dict, *, inject=False, mark_planned=True,
        early_reboot=False, count_before: int | None = None) -> dict:
    control = ADB_CHAIN_CONTROL if early_reboot else "boot-timing-extra-pass" if inject else None
    if control:
        os.environ["RIVERQA_CONTROL"] = control
    try:
        rec = reboots.planned_reboot(dev, path, boot_timeout=600, inject_extra_pass=inject,
                                     mark_planned=mark_planned, inject_early_reboot=early_reboot, watcher=w)
    finally:
        if control:
            os.environ.pop("RIVERQA_CONTROL", None)
    reboots.judge_signals(dev, rec, timing)
    from .evidence import read_pstore
    _ok, log_bytes, _why = read_pstore(dev, "console-ramoops*")
    exact = log_bytes.count(rec.marker.encode()) if rec.marker else 0
    copies = len(bootsig.marker_offsets(log_bytes, rec.marker)) if rec.marker else 0
    d = rec.to_json()
    d["marker_copies_exact"] = exact
    d["marker_copies_fuzzy"] = copies
    d["marker_found_fuzzy"] = copies > 0
    if rec.chain[0] != PASS and log_bytes:
        d["pstore_saved"] = str(save_nopass_pstore(rec, log_bytes))
    d["pstore_bytes"] = len(log_bytes)
    if count_before is not None:
        st, msg = reboots.read_boot_count(dev, count_before + 1, settle_s=90)
        d["boot_count"] = [st, msg]
    kind = "early reboot" if early_reboot else "inject" if inject else "normal"
    log(f"  {path:8} {kind}{'' if mark_planned else ' unmarked'}: "
        f"boot {rec.host_boot_seconds:.1f} s, uptime {rec.uptime:.1f} s, elapsed {rec.elapsed:.2f} s, "
        f"pass {rec.bootloader_pass:.2f} s, disconnect gap {fmt_s(rec.disconnect_gap)}, status read "
        f"{fmt_s(rec.status_read_s)}, fastboot planned {rec.planned_fastboot} unplanned "
        f"{rec.unplanned_fastboot}, chain {rec.chain[0]} ({exact} exact copies, {copies} within flips), "
        f"uptime-check {rec.uptime_check[0]}{' (' + rec.control + ')' if rec.control else ''}")
    ids = sorted({e['ids'] for e in d['usb_events'] if e['kind'] == 'present'})
    log(f"           usb ids seen: {ids}")
    return d


def fmt_s(v) -> str:
    return "?" if v is None else f"{v:.3f} s"


def save_nopass_pstore(rec, log_bytes: bytes):
    """Keep the raw pstore of a reboot whose chain did not pass:
    $BENCH_HOME/qa/pstore-nopass/<time>-<marker>.bin, with the marker and
    the verdict next to it."""
    name = time.strftime("%Y%m%d-%H%M%S") + "-" + (rec.marker or "nomarker").replace(":", "_")
    out = bench_path("qa", "pstore-nopass", name + ".bin")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(log_bytes)
    write_json(out.with_suffix(".json"), {"path": rec.path, "marker": rec.marker, "chain": list(rec.chain),
                                          "control": rec.control, "boot_id_before": rec.boot_id_before})
    return out


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


MIN_CLEAN_NORMALS = 3   # clean normal reboots a path needs before it gets an A (review round 4)


def sample_problems(r: dict, *, normal: bool) -> list[str]:
    """Signs that one calibration reboot was not a single clean boot, or
    that its timing is not trustworthy (review round 4): an unplanned
    fastboot enumeration, a chain that isn't PASS, a boot count that isn't
    PASS (normal reboots, which record one), or a disconnect or status
    read bracketed more loosely than the bounds. Empty means clean. Pure."""
    why = []
    if r.get("unplanned_fastboot"):
        why.append(f"{r['unplanned_fastboot']} unplanned fastboot enumeration(s)")
    chain = (r.get("chain") or [None])[0]
    if chain != PASS:
        why.append(f"chain {chain}")
    if normal:
        count = (r.get("boot_count") or [None])[0]
        if count != PASS:
            why.append(f"boot count {count}")
    timing = bootsig.timing_bracket_problem(r.get("disconnect_gap"), r.get("status_read_s"))
    if timing:
        why.append(timing)
    return why


def compute_paths(data: dict) -> None:
    """Set A and the chain record for each gated path from data["raw"] and
    data["inject_measure"]. Pure.

    Only clean samples count (review round 4): a normal reboot with any
    sign of an extra boot or a loose timing bracket (sample_problems) is
    excluded and listed, and a path with fewer than MIN_CLEAN_NORMALS clean
    normal reboots, or whose injected-pass measurement is not clean, gets
    no A."""
    clean: dict = {}
    excluded: dict = {}
    for p in reboots.GATED_PATHS:
        clean[p], excluded[p] = [], []
        for i, r in enumerate(data["raw"].get(p) or []):
            probs = sample_problems(r, normal=True)
            if probs:
                excluded[p].append({"sample": i, "bootloader_pass": r.get("bootloader_pass"), "why": probs})
            else:
                clean[p].append(r["bootloader_pass"])
    passes = clean
    # "One more bootloader pass": the flash path's normal pass (reset to
    # kernel start, DESIGN.md round-10 note) or the measured increment of
    # an injected pass, whichever is smaller. On river the injected pass
    # (reset to fastboot and on) adds about 4.4 s, far less than the 10 s
    # reset-to-kernel pass, so the smaller value is the one that lets the
    # uptime check reject the control.
    flash_pass = min(passes["fastboot"]) if len(passes["fastboot"]) >= MIN_CLEAN_NORMALS else None
    for path in reboots.GATED_PATHS:
        raws = data["raw"].get(path) or []
        chains = [r["chain"][0] for r in raws]
        data["chain"][path] = {"survives": bool(chains) and all(c == PASS for c in chains), "verdicts": chains,
                               "exact_copies": [r.get("marker_copies_exact") for r in raws],
                               "fuzzy_found": [r.get("marker_found_fuzzy") for r in raws]}
        inj = data["inject_measure"].get(path)
        inj_probs = sample_problems(inj, normal=False) if inj else []
        empty = {"A": None, "normal_passes": passes[path], "excluded": excluded[path],
                 "all_passes": [r.get("bootloader_pass") for r in raws], "proof_normal_pass": False,
                 "proof_control_rejected": False, "proof_unmarked_rejected": False}
        if not raws:
            why = f"no {path} reboots measured"
        elif len(passes[path]) < MIN_CLEAN_NORMALS:
            why = (f"only {len(passes[path])} clean normal {path} reboots of {len(raws)} (need "
                   f"{MIN_CLEAN_NORMALS}); excluded: " + "; ".join(
                       f"#{e['sample']} ({', '.join(e['why'])})" for e in excluded[path]))
        elif flash_pass is None:
            why = f"fewer than {MIN_CLEAN_NORMALS} clean fastboot reboots, so no flash-path pass"
        elif not inj:
            why = "no injected-pass measurement"
        elif inj_probs:
            why = "the injected-pass measurement is not clean: " + ", ".join(inj_probs)
        else:
            why = None
        if why:
            data["paths"][path] = dict(empty, why=why)
            continue
        inc = inj["bootloader_pass"] - min(passes[path])
        extra = min(flash_pass, inc)
        a_val, why = bootsig.compute_allowance(passes[path], extra)
        data["paths"][path] = dict(empty, A=a_val, why=why, extra_pass=round(extra, 3),
                                   extra_pass_flash_path=flash_pass, extra_pass_injected_increment=round(inc, 3),
                                   mean=round(statistics.mean(passes[path]), 3))


def prove(dev: Device, w: UsbWatcher, data: dict) -> None:
    """The step-1 proofs on fresh reboots, judged with the recorded A, then
    the adb chain control. allowance_for() only uses A once both uptime
    proofs are recorded."""
    for path in reboots.GATED_PATHS:
        p = data["paths"].get(path) or {}
        if p.get("A") is None:
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
            "expected": "FAIL (uptime)", "got": ctrl["uptime_check"], "qualified": p["proof_control_rejected"],
            "check": "uptime (boot-timing)"}
        data["controls"][f"boot-timing-{path}-extra-pass-unmarked"] = {
            "expected": "FAIL (unplanned enumeration)", "got": unm["uptime_check"],
            "qualified": p["proof_unmarked_rejected"], "check": "unplanned enumeration (boot-timing)"}
        reboots.save_timing(data)
    prove_adb_chain(dev, w, data)
    reboots.save_timing(data)


def prove_adb_chain(dev: Device, w: UsbWatcher, data: dict) -> None:
    """The bench-boot-1 control (amendment A2 rule 6): an ``adb reboot``
    with an extra full reboot injected as soon as adb answers and before
    boot completes, so well after ramoops init. The extra kernel's log
    replaces pstore, so the chain must FAIL. It qualifies only if the fault
    went in (the phone dropped off USB again) and the chain itself is FAIL."""
    rec = one(dev, w, "adb", data, early_reboot=True)
    data["adb_chain_control"] = rec
    data["controls"][ADB_CHAIN_CONTROL] = {
        "expected": "FAIL (chain)", "got": rec["chain"], "injected": rec["control"] == "early-reboot",
        "qualified": rec["control"] == "early-reboot" and rec["chain"][0] == FAIL, "check": "chain (boot-timing)"}


def summary(data: dict) -> list[str]:
    """The report lines, also for a path that got no A (the recorded reason
    is printed, never a KeyError)."""
    out = ["Boot-timing calibration:"]
    for path in reboots.GATED_PATHS:
        p = data.get("paths", {}).get(path) or {}
        chain = (data.get("chain", {}).get(path) or {}).get("survives")
        if p.get("A") is None:
            out.append(f"  {path:8} not qualified: {p.get('why') or 'not measured'}; chain survives: {chain}")
            continue
        out.append(f"  {path:8} A={p['A']} ({p.get('why')}); extra pass {p.get('extra_pass')} s (flash path "
                   f"{p.get('extra_pass_flash_path')} s, injected increment {p.get('extra_pass_injected_increment')} s)"
                   f"; normal boot passes: {p.get('proof_normal_pass')}; extra pass rejected by uptime: "
                   f"{p.get('proof_control_rejected')}; unmarked rejected by enumeration rule: "
                   f"{p.get('proof_unmarked_rejected')}; chain survives: {chain}")
    c = (data.get("controls") or {}).get(ADB_CHAIN_CONTROL)
    out.append(f"  adb      no uptime gate; extra reboot after ramoops init rejected by the chain: "
               f"{c.get('qualified') if c else 'not run'}{' (' + str(c.get('got')) + ')' if c else ''}")
    return out


def qualified(data: dict) -> bool:
    """Every gated path has all three proofs (a normal boot passed, the
    marked extra pass was rejected by the uptime check and the unmarked one
    by the enumeration rule), and the adb chain control qualified."""
    return all(all((data.get("paths", {}).get(p) or {}).get(k)
                   for k in ("proof_normal_pass", "proof_control_rejected", "proof_unmarked_rejected"))
               for p in reboots.GATED_PATHS) and bool(
        ((data.get("controls") or {}).get(ADB_CHAIN_CONTROL) or {}).get("qualified"))


def boot_timing_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="phone-qa boot-timing")
    ap.add_argument("--normal", type=int, default=4, help="normal reboots per path to set A (default 4)")
    ap.add_argument("--reuse-normals", action="store_true",
                    help="reuse the normal reboots in boot-timing.json if they were measured on this build")
    a = ap.parse_args(argv)
    try:
        with DeviceLock():
            return run(a.normal, a.reuse_normals)
    except (LockError, GuardError) as e:
        log(f"error: {e}")
        return 1


def prove_main() -> int:
    """phone-qa controls --boot-timing: run the step-1 proofs again with the
    recorded A, and record the five boot-timing controls under the harness
    version that results. Run it after the last change to
    the harness, the thresholds or the seed, and before the other controls
    (README, "Order of qualification")."""
    try:
        with DeviceLock():
            data = reboots.load_timing()
            if not data.get("paths"):
                log("error: no boot-timing.json; run `phone-qa boot-timing` first")
                return 2
            dev = Device(SERIAL)
            fbmode.recover(dev, 600, boot=True)    # resume from fastboot or a leftover fb_mode (R4-3)
            dev.guard()
            dev.root()
            data.setdefault("controls", {})
            w = UsbWatcher(dev.serial).start()
            try:
                prove(dev, w, data)
            finally:
                w.stop()
            return finish(data)
    except (LockError, GuardError) as e:
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
    # Calibration resumes from a phone sitting in fastboot or left with
    # fb_mode set by a killed run (review round 4).
    fbmode.recover(dev, 600, boot=True)
    dev.guard()
    dev.root()
    old = reboots.load_timing()
    data = {"measured_at": now_iso(), "build": dev.getprop("ro.lineage.display.version"),
            "build_date_utc": dev.getprop("ro.build.date.utc"), "raw": {p: [] for p in reboots.GATED_PATHS},
            "inject_measure": {}, "paths": {}, "chain": {}, "controls": {}}
    timing_none = {}  # no allowance while measuring
    if reuse and old.get("build_date_utc") == data["build_date_utc"] and all(old.get("raw", {}).get(p)
                                                                             for p in reboots.GATED_PATHS):
        data["raw"] = {p: old["raw"][p] for p in reboots.GATED_PATHS}
        data["reused_normals_from"] = old.get("measured_at")
        log(f"Reusing {sum(len(v) for v in data['raw'].values())} normal reboots from {old.get('measured_at')}")
    w = UsbWatcher(dev.serial).start()
    try:
        if not data.get("reused_normals_from"):
            for _ in range(n_normal):
                for path in reboots.GATED_PATHS:
                    before = boot_count_now(dev)
                    r = one(dev, w, path, timing_none, count_before=before)
                    r["boots_before"] = before
                    data["raw"][path].append(r)
                    write_json(bench_path("qa", "boot-timing.partial.json"), data)
        # One injected extra pass per path, to measure how long an extra
        # bootloader pass really is on this phone (separate from the proofs).
        for path in reboots.GATED_PATHS:
            data["inject_measure"][path] = one(dev, w, path, timing_none, inject=True, mark_planned=True)
            write_json(bench_path("qa", "boot-timing.partial.json"), data)
        compute_paths(data)
        reboots.save_timing(data)
        prove(dev, w, data)
    finally:
        w.stop()
    return finish(data)
