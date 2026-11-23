"""Calibration step 1: boot timing, with no flash (DESIGN.md, "Calibration
and bootstrap").

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

It also records whether the kernel-log chain survives each path and how
often ramoops corrupts the marker. Results: $BENCH_HOME/qa/boot-timing.json.
The proofs are part of the control set (controls.json).
"""

from __future__ import annotations

import argparse
import os
import statistics

from . import bootsig, reboots
from .core import SERIAL, log, now_iso, write_json, bench_path
from .device import Device, UsbWatcher
from .lock import DeviceLock, LockError
from .results import FAIL, PASS


def one(dev: Device, w: UsbWatcher, path: str, timing: dict, *, inject=False, mark_planned=True,
        count_before: int | None = None) -> dict:
    if inject:
        os.environ["RIVERQA_CONTROL"] = "boot-timing-extra-pass"
    try:
        rec = reboots.planned_reboot(dev, path, boot_timeout=600, inject_extra_pass=inject,
                                     mark_planned=mark_planned, watcher=w)
    finally:
        if inject:
            os.environ.pop("RIVERQA_CONTROL", None)
    reboots.judge_signals(dev, rec, timing)
    log_bytes = dev.sh_bytes("cat /sys/fs/pstore/console-ramoops* 2>/dev/null", timeout=60)
    exact = log_bytes.count(rec.marker.encode()) if rec.marker else 0
    fuzzy = bootsig.find_marker(log_bytes, rec.marker) is not None if rec.marker else False
    d = rec.to_json()
    d["marker_copies_exact"] = exact
    d["marker_found_fuzzy"] = fuzzy
    d["pstore_bytes"] = len(log_bytes)
    d["pstore_last_ts"] = bootsig.last_timestamp(log_bytes)
    if count_before is not None:
        st, msg = reboots.read_boot_count(dev, count_before + 1, settle_s=90)
        d["boot_count"] = [st, msg]
    log(f"  {path:8} {'inject' if inject else 'normal'}{'' if mark_planned else ' unmarked'}: "
        f"boot {rec.host_boot_seconds:.1f} s, uptime {rec.uptime:.1f} s, elapsed {rec.elapsed:.2f} s, "
        f"pass {rec.bootloader_pass:.2f} s, fastboot planned {rec.planned_fastboot} unplanned "
        f"{rec.unplanned_fastboot}, chain {rec.chain[0]} ({exact} exact copies), uptime-check {rec.uptime_check[0]}")
    ids = sorted({e['ids'] for e in d['usb_events'] if e['kind'] == 'present'})
    log(f"           usb ids seen: {ids}")
    return d


def boot_count_now(dev: Device) -> int:
    return bootsig.dropbox_tags(dev.sh("dumpsys dropbox", timeout=60)).count("SYSTEM_BOOT")


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
        log(f"Reusing {sum(len(v) for v in old['raw'].values())} normal reboots from {old.get('measured_at')}")
    w = UsbWatcher(dev.serial).start()
    try:
        if not data.get("reused_normals_from"):
            for i in range(n_normal):
                for path in reboots.PATHS:
                    before = boot_count_now(dev)
                    data["raw"][path].append(one(dev, w, path, timing_none, count_before=before))
                    write_json(bench_path("qa", "boot-timing.partial.json"), data)
        # One injected extra pass per path, to measure how long an extra
        # bootloader pass really is on this phone (separate from the proofs).
        for path in reboots.PATHS:
            data["inject_measure"][path] = one(dev, w, path, timing_none, inject=True, mark_planned=True)
            write_json(bench_path("qa", "boot-timing.partial.json"), data)
        passes = {p: [r["bootloader_pass"] for r in data["raw"][p]] for p in reboots.PATHS}
        # "One more bootloader pass": the flash path's normal pass (reset to
        # kernel start, DESIGN.md round-10 note) or the measured increment of
        # an injected pass, whichever is smaller. On river the injected pass
        # (reset to fastboot and on) adds about 4 s, far less than the 10 s
        # reset-to-kernel pass, so the smaller value is the one that lets the
        # uptime check reject the control.
        flash_pass = min(passes["fastboot"])
        for path in reboots.PATHS:
            inc = data["inject_measure"][path]["bootloader_pass"] - min(passes[path])
            extra = min(flash_pass, inc)
            a_val, why = bootsig.compute_allowance(passes[path], extra)
            data["paths"][path] = {"A": a_val, "why": why, "normal_passes": passes[path], "extra_pass": round(extra, 3),
                                   "extra_pass_flash_path": flash_pass, "extra_pass_injected_increment": round(inc, 3),
                                   "mean": round(statistics.mean(passes[path]), 3),
                                   "proof_normal_pass": False, "proof_control_rejected": False,
                                   "proof_unmarked_rejected": False}
            chains = [r["chain"][0] for r in data["raw"][path]]
            data["chain"][path] = {"survives": all(c == PASS for c in chains), "verdicts": chains,
                                   "exact_copies": [r["marker_copies_exact"] for r in data["raw"][path]],
                                   "fuzzy_found": [r["marker_found_fuzzy"] for r in data["raw"][path]]}
        # Proofs on fresh reboots, judged with the new A. allowance_for() only
        # uses A once both proofs are recorded.
        reboots.save_timing(data)
        for path in reboots.PATHS:
            p = data["paths"][path]
            if p["A"] is None:
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
            data["controls"][f"boot-timing-{path}-extra-pass"] = {
                "expected": "FAIL (uptime)", "got": ctrl["uptime_check"], "qualified": p["proof_control_rejected"]}
            data["controls"][f"boot-timing-{path}-extra-pass-unmarked"] = {
                "expected": "FAIL (unplanned enumeration)", "got": unm["uptime_check"],
                "qualified": p["proof_unmarked_rejected"]}
            reboots.save_timing(data)
    finally:
        w.stop()
    reboots.save_timing(data)
    from .controls import record_step1
    record_step1(data["controls"])
    log("\nBoot-timing calibration:")
    for path in reboots.PATHS:
        p = data["paths"][path]
        log(f"  {path:8} A={p['A']} ({p['why']}); extra pass {p['extra_pass']} s (flash path "
            f"{p['extra_pass_flash_path']} s, injected increment {p['extra_pass_injected_increment']} s); "
            f"normal boot passes: {p['proof_normal_pass']}; extra pass rejected by uptime: "
            f"{p['proof_control_rejected']}; unmarked rejected by enumeration rule: {p['proof_unmarked_rejected']}; "
            f"chain survives: {data['chain'][path]['survives']}")
    ok = all(data["paths"][p]["proof_normal_pass"] and data["paths"][p]["proof_control_rejected"]
             for p in reboots.PATHS)
    log(f"Saved {bench_path('qa', 'boot-timing.json')}")
    return 0 if ok else 2
