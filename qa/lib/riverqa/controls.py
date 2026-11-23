"""The step-5 control set: deliberately broken runs that must give their
expected result before any run can be merge-eligible.

Results live in $BENCH_HOME/qa/controls.json keyed by harness version, so
changing the harness, the thresholds, A or the seed clears them. Each result
records which check produced the verdict, not only the verdict.

Kinds:
- ``qa``: run phone-qa with RIVERQA_CONTROL=<name>; the named checks inject
  the fault (see ``control_active()`` in the check code) and the control is
  judged by the target check's state.
- ``step1``: proved by ``phone-qa boot-timing`` (the extra-pass controls).
- ``host``: pure host logic with stubbed gh / git ls-remote.
- ``recovery``: kill a run mid-check, then run again; the journal must restore
  the phone and the next run must be clean.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess

from .core import bench_path, now_iso, read_json, write_json
from .results import FAIL, PASS, UNVERIFIED

CONTROLS_FILE = bench_path("qa", "controls.json")

# name: (kind, target check IDs, expected state, description)
CONTROLS: dict[str, tuple[str, list[str], str, str]] = {
    "audio-volume-0": ("qa", ["hardware.audio"], FAIL, "STREAM_MUSIC at 0"),
    "location-off": ("qa", ["radios.gps"], FAIL, "location left off"),
    "software-decoder": ("qa", ["system.video"], FAIL, "software AVC decoder forced"),
    "input-text": ("qa", ["system.keyboard"], FAIL, "`input text` instead of key taps"),
    "torch-disabled": ("qa", ["hardware.cameras"], FAIL, "torch LED disabled below the HAL"),
    "bad-ntp": ("qa", ["system.time"], FAIL, "bad NTP server set after a good sync"),
    "wifi-off": ("qa", ["radios.wifi"], FAIL, "Wi-Fi off"),
    "bt-rfkill": ("qa", ["radios.bluetooth"], UNVERIFIED, "host adapter rfkilled (fixture problem)"),
    "wrong-batch-json": ("qa", ["identity.build_id", "identity.props"], FAIL, "batch.json for another build"),
    "stale-csv-row": ("qa", ["identity.bench_rows"], FAIL, "a device.csv row from another build"),
    "wrong-build-id": ("qa", ["identity.build_id"], FAIL, "wrong ro.river.build_id"),
    "boot-hash-mismatch": ("qa", ["identity.boot_hash"], FAIL, "staged boot.img differs"),
    "low-ram-flag": ("qa", ["identity.low_ram"], FAIL, "ro.config.low_ram=true with experiment false"),
    "planted-crash": ("qa", ["health.boot_evidence"], FAIL, "crash planted in an earlier boot's evidence"),
    "wifi-unbound": ("qa", ["radios.wifi"], FAIL, "LAN page fetched without binding to Wi-Fi"),
    "missing-median": ("qa", ["health.regression"], UNVERIFIED, "3-boot median required but missing"),
    "beacon-blocked": ("qa", ["system.webview"], FAIL, "page beacon blocked"),
    "extra-dim-stub": ("qa", ["hardware.screen"], FAIL, "Extra dim matrix read from a stub"),
    "killed-mid-check": ("recovery", [], PASS, "run killed mid-check, then run again"),
    "boot-id-changed": ("qa", ["final.boot_id"], FAIL, "boot_id changed during the run"),
    "alarm-early": ("qa", ["system.alarms"], FAIL, "alarm checked 1 minute early"),
    "nav-mode-0": ("qa", ["system.gestures"], FAIL, "navigation_mode 0"),
    "camera-set-changed": ("qa", ["hardware.cameras"], UNVERIFIED, "camera set changed in batch.json"),
    "extra-reboot-first-boot": ("qa", ["health.boot_signals"], FAIL, "extra reboot before the first boot completes"),
    "extra-reboot-bench": ("qa", ["health.boot_signals"], FAIL, "extra reboot before a bench boot completes"),
    "boot-timing-adb-extra-pass": ("step1", [], FAIL, "extra bootloader pass, planned marking (adb path)"),
    "boot-timing-fastboot-extra-pass": ("step1", [], FAIL, "extra bootloader pass, planned marking (fastboot path)"),
    "boot-timing-adb-extra-pass-unmarked": ("step1", [], FAIL, "extra pass without planned marking (adb path)"),
    "boot-timing-fastboot-extra-pass-unmarked": ("step1", [], FAIL,
                                                 "extra pass without planned marking (fastboot path)"),
    "tag-pushed-host-killed": ("host", [], PASS, "tag pushed, host killed before slot-state update: promote"),
}


def active() -> str | None:
    return os.environ.get("RIVERQA_CONTROL") or None


def _store(version: str, name: str, rec: dict) -> None:
    data = read_json(CONTROLS_FILE, {}) or {}
    data.setdefault(version, {"results": {}})["results"][name] = rec
    write_json(CONTROLS_FILE, data)


def record_step1(results: dict) -> None:
    from .qa import harness_version
    v = harness_version()
    for name, r in results.items():
        _store(v, name, {"at": now_iso(), "expected": r["expected"], "got": r["got"],
                         "qualified": bool(r["qualified"]), "check": "uptime/enumeration (boot-timing)"})


def show_controls(version: str) -> int:
    data = (read_json(CONTROLS_FILE, {}) or {}).get(version, {}).get("results", {})
    print(f"Controls for harness {version}:")
    for name, (kind, targets, expected, desc) in CONTROLS.items():
        r = data.get(name)
        st = "qualified" if r and r.get("qualified") else ("FAILED" if r else "not run")
        print(f"  {st:10} {name:42} {kind:8} expect {expected:10} {desc}")
    n = sum(1 for n in CONTROLS if (data.get(n) or {}).get("qualified"))
    print(f"{n}/{len(CONTROLS)} qualified")
    return 0 if n == len(CONTROLS) else 2


# --- in-run fault injection for identity/health controls ---------------------------------

def apply_pre(run, name: str) -> None:
    """Mutate the run's view of its inputs for controls that break evidence
    rather than the phone. Never touches files outside the run directory."""
    if name == "wrong-batch-json":
        run.batch = copy.deepcopy(run.batch)
        run.batch["build_id"] = (run.batch.get("build_id") or "x") + "-other"
        run.batch["dates"]["build"] = str(int(run.batch["dates"]["build"] or 0) - 86400)
    elif name == "wrong-build-id":
        run.batch = copy.deepcopy(run.batch)
        run.batch["build_id"] = "wrong-build-id"
        run.batch["vendor_build_id"] = "wrong-build-id"
    elif name == "boot-hash-mismatch":
        run.batch = copy.deepcopy(run.batch)
        run.batch["images"]["boot"]["sha256"] = "0" * 64
    elif name == "missing-median":
        run.control_drop_rows_after = 1
    elif name == "camera-set-changed":
        run.batch = copy.deepcopy(run.batch)
        run.batch["cameras"]["set"] = [0, 1]


def run_control(run, name: str, lock_result) -> int:
    from . import qa
    kind, targets, expected, _desc = CONTROLS[name]
    if kind == "host":
        ok, got = host_control(name)
        _store(run.version, name, {"at": now_iso(), "expected": expected, "got": got, "qualified": ok,
                                   "check": "recovery decision (stubbed gh, git ls-remote)"})
        print(f"control {name}: {got} -> {'qualified' if ok else 'NOT qualified'}")
        return 0 if ok else 1
    if kind == "step1":
        print("This control is proved by `phone-qa boot-timing`.")
        return 2
    if kind == "recovery":
        print("killed-mid-check: run `phone-qa --calibrate --batch ID`, kill it during a feature check, then run "
              "`phone-qa --control killed-mid-check --batch ID` again; this records the second run.")
        run.only = []
    else:
        groups = sorted({t.split(".")[0] for t in targets})
        run.only = ["preflight"] + groups
    apply_pre(run, name)
    return qa.full_run(run, lock_result)


def record_control(run, code: int) -> None:
    name = active()
    if not name:
        return
    kind, targets, expected, _ = CONTROLS[name]
    if kind == "recovery":
        j_ok = run.results.get("preflight.journal")
        ok = code == 0 and j_ok is not None and j_ok.state == PASS
        got = {"exit": code, "journal": j_ok.summary if j_ok else None}
        check = "preflight.journal + clean rerun"
    else:
        got = {t: run.results[t].state if t in run.results else "missing" for t in targets}
        ok = any(s == expected for s in got.values())
        check = ", ".join(t for t, s in got.items() if s == expected) or "none"
    _store(run.version, name, {"at": now_iso(), "expected": expected, "got": got, "qualified": ok,
                               "check": check, "run_id": run.run_id})
    print(f"control {name}: expected {expected}, got {json.dumps(got)} -> {'qualified' if ok else 'NOT qualified'}")


def host_control(name: str) -> tuple[bool, str]:
    from . import recovery, slots
    batch = {"prs": [{"repo": "zrm625/android_manifest", "number": 1}], "tag": "known-good-x",
             "tag_repos": ["https://github.com/zrm625/android_manifest"]}

    def stub(cmd):
        if cmd[0] == "gh":
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"state": "MERGED", "url": "u"}), "")
        return subprocess.CompletedProcess(cmd, 0, "abc\trefs/tags/known-good-x\n", "")

    look = recovery.lookup(batch, stub)
    decision, why = slots.decide_recovery({"kind": "batch", "batch": "b"}, look)
    return decision == slots.PROMOTE, f"{decision}: {why}"
