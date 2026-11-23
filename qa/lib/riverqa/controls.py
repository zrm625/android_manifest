"""The step-5 control set: deliberately broken runs that must give their
expected result before any run can be merge-eligible.

Results live in $BENCH_HOME/qa/controls.json keyed by harness version, so
changing the harness, the thresholds, A or the seed clears them. Each result
records which check produced the verdict, not only the verdict.

Kinds:
- ``qa``: run phone-qa with RIVERQA_CONTROL=<name>; the named checks inject
  the fault (see ``control_active()`` in the check code) and set
  ``run.control_injected``. A control qualifies only if that marker names it
  and a target check reached the expected state (control_verdict). The two
  extra-reboot controls inject during flash or device-bench (run them with
  RIVERQA_CONTROL set); batch.json's ``control_injections`` records it.
- ``step1``: proved by ``phone-qa boot-timing`` (the extra-pass controls).
- ``host``: flash --recover against a scratch $BENCH_HOME with stubbed gh /
  git ls-remote, judged by the slot-state.json it persists.
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
    rather than the phone. Never touches files outside the run directory.
    Each mutation sets the injection marker."""
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
    elif name == "camera-set-changed":
        run.batch = copy.deepcopy(run.batch)
        run.batch["cameras"]["set"] = [0, 1]
    else:
        return
    run.control_injected = name


def run_control(run, name: str, lock_result) -> int:
    from . import qa
    kind, targets, expected, _desc = CONTROLS[name]
    if kind == "host":
        ok, got = host_control(name)
        _store(run.version, name, {"at": now_iso(), "expected": expected, "got": got, "qualified": ok,
                                   "injected": True,
                                   "check": "flash --recover, judged by the persisted slot-state.json "
                                            "(stubbed gh, git ls-remote)"})
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


def control_verdict(name: str, results: dict, injected: str | None, code: int) -> tuple[bool, dict]:
    """Did this control qualify? Only if its fault was really injected (the
    run's marker names it) and a target check reached the expected state
    (A1). Returns (qualified, record). Pure."""
    kind, targets, expected, _ = CONTROLS[name]
    marked = injected == name
    if kind == "recovery":
        j = results.get("preflight.journal")
        det = (j.details if j else {}) or {}
        restored = int(det.get("restored") or 0)
        killed = det.get("killed_runs") or []
        ok = marked and code == 0 and j is not None and j.state == PASS and restored > 0 and bool(killed)
        return ok, {"expected": expected, "injected": marked, "exit": code, "restored": restored,
                    "killed_runs": killed, "check": "preflight.journal + clean rerun",
                    "targets": {"preflight.journal": {"state": j.state if j else "missing",
                                                      "summary": j.summary if j else None}}}
    tgt = {t: {"state": results[t].state if t in results else "missing",
               "summary": results[t].summary if t in results else None} for t in targets}
    hit = [t for t, v in tgt.items() if v["state"] == expected]
    return marked and bool(hit), {"expected": expected, "injected": marked, "targets": tgt,
                                  "check": ", ".join(hit) or "none"}


def record_control(run, code: int) -> None:
    name = active()
    if not name:
        return
    ok, rec = control_verdict(name, run.results, getattr(run, "control_injected", None), code)
    _store(run.version, name, {"at": now_iso(), "qualified": ok, "run_id": run.run_id, **rec})
    why = "" if rec["injected"] else " (fault not injected)"
    print(f"control {name}: expected {rec['expected']}, got "
          f"{json.dumps({t: v['state'] for t, v in rec['targets'].items()})}{why} -> "
          f"{'qualified' if ok else 'NOT qualified'}")


class _PromotePhone:
    """The phone as flash --promote sees it, for the host control: running
    the candidate slot with the batch's build and a boot hash. Never adb."""

    serial = "control"

    def __init__(self, slot: str, build_id: str):
        self.slot, self.build_id = slot, build_id

    def guard(self):
        pass

    def state(self):
        return "device"

    def root(self, seconds=60):
        pass

    def getprop(self, name):
        return f"_{self.slot}" if name == "ro.boot.slot_suffix" else ""

    def sh(self, cmd, timeout=30):
        if "ro.vendor.river.build_id" in cmd:
            return f"ro.vendor.river.build_id={self.build_id}\n"
        if "ro.river.build_id" in cmd:
            return f"ro.river.build_id={self.build_id}\n"
        if "sha256sum" in cmd:
            return "c" * 64 + "  boot\n"
        return "0\n" if cmd == "id -u" else ""


def host_control(name: str) -> tuple[bool, str]:
    """tag-pushed-host-killed: a batch whose PRs (heads at the tested
    commits) are merged and whose tag is pushed in every repo of the tested
    commit set (at the merge commit, or the tested commit where there is no
    PR), with the host killed before slot-state.json was updated. Drive flash --recover itself, against a scratch $BENCH_HOME
    with stubbed gh and git ls-remote, and judge the persisted slot-state.json:
    the batch must be promoted, never rolled back."""
    import tempfile
    from pathlib import Path
    from . import core, flashing, slots, stage
    from .core import write_json as wj
    saved = core.BENCH_HOME
    try:
        with tempfile.TemporaryDirectory() as tmp:
            core.BENCH_HOME = Path(tmp)
            repos = sorted(stage.expected_repos())
            slugs = stage.repo_slugs()
            commits = {p: f"{i + 1:040x}" for i, p in enumerate(repos)}
            h = stage.commit_set_hash(commits)
            bid = f"ctl-{h}"
            st = slots.first_run("a", "f" * 64, "t0")
            st["baseline_closed"] = {"batch": "base", "build_id": None, "at": "t0", "seed": None,
                                     "seed_written": True}
            slots.reserve(st, "ctl", "batch", "f" * 64, "t1")
            slots.mark_written(st, "flash-ctl", "t2")
            flashing.save_state(st)
            prs = [{"repo": "zrm625/android_manifest", "number": 1},
                   {"repo": "zrm625/android_device_motorola_river", "number": 2}]
            by_slug = {slugs[p]: p for p in repos}
            batch = {"batch": "ctl", "kind": "batch", "build_id": bid, "vendor_build_id": bid,
                     "commit_set": commits, "commit_set_hash": h, "prs": prs,
                     "no_pr": [p for p in repos if slugs[p] not in {x["repo"] for x in prs}],
                     "tag": "known-good-ctl",
                     "tag_repos": [f"https://github.com/{slugs[p]}" for p in repos]}
            expected, _ = slots.expected_pr_set(batch, slugs)
            batch["merge_started"] = {"at": "t3", "expected_prs": expected}
            wj(core.batch_dir("ctl") / "batch.json", batch)
            merge = {p["repo"]: f"{0xabc0 + p['number']:040x}" for p in prs}

            def stub(cmd):
                if cmd[:2] == ["gh", "api"]:
                    # Each merge went into an unmoved river-22.2: the merge
                    # commit's tree is the tested commit's tree.
                    repo = cmd[2].split("/")[1] + "/" + cmd[2].split("/")[2]
                    return subprocess.CompletedProcess(cmd, 0, f"tree-{repo}\n", "")
                if cmd[0] == "gh":
                    repo = cmd[cmd.index("--repo") + 1]
                    return subprocess.CompletedProcess(cmd, 0, json.dumps(
                        {"state": "MERGED", "url": f"https://github.com/{repo}/pull/x", "baseRefName": "river-22.2",
                         "headRefOid": commits[by_slug[repo]], "mergeCommit": {"oid": merge[repo]}}), "")
                repo = cmd[3].removeprefix("https://github.com/")
                at = merge.get(repo) or commits[by_slug[repo]]
                return subprocess.CompletedProcess(
                    cmd, 0, f"{'9' * 40}\trefs/tags/known-good-ctl\n{at}\trefs/tags/known-good-ctl^{{}}\n", "")
            rc = flashing.recover(runner=stub, dev=_PromotePhone("b", bid))
            after = flashing.load_state()
    finally:
        core.BENCH_HOME = saved
    events = [e["event"] for e in after.get("history", [])]
    ok = (rc == flashing.EXIT_OK and after["known_good_slot"] == "b" and after["pair"] is None
          and "promote" in events and "rolled-back" not in events)
    return ok, (f"flash --recover exit {rc}; persisted known_good_slot {after['known_good_slot']}, "
                f"pair {'released' if after['pair'] is None else 'still reserved'}, events {events[-3:]}")
