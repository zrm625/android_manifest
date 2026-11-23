"""scripts/flash: install a build on the test phone, with the slot pair,
rollback and slot rescue from DESIGN.md ("The batch sequence", step 2)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import evidence, journal, reboots, recovery, slots, stage
from .core import (BENCH_HOME, CODENAME, SERIAL, Deadline, batch_dir, bench_path, log, new_id,
                   now_iso, read_json, sha256_file, write_json)
from .device import Device, GuardError, Unreachable
from .lock import DeviceLock, LockError

IMAGES = ("boot", "dtbo", "system", "vendor")
STATE_FILE = "slot-state.json"
FIRST_BOOT_TIMEOUT = 900
BOOT_TIMEOUT = 600

EXIT_OK, EXIT_ERROR, EXIT_HALT, EXIT_RESCUED = 0, 1, 3, 4


class Halt(RuntimeError):
    """Stop the pipeline and notify Zak."""


def state_path() -> Path:
    return bench_path(STATE_FILE)


def load_state() -> dict | None:
    return read_json(state_path())


def save_state(st: dict) -> None:
    write_json(state_path(), st)


def qa_home() -> Path:
    return bench_path("qa")


# --- phone reads ---------------------------------------------------------------

def running_slot(dev: Device) -> str:
    s = dev.getprop("ro.boot.slot_suffix").lstrip("_")
    if s not in slots.SLOTS:
        raise RuntimeError(f"cannot read the running slot ({s!r})")
    return s


def boot_partition_sha(dev: Device, slot: str, nbytes: int | None = None) -> str:
    """SHA-256 of boot_<slot> as root: the whole partition, or its first
    nbytes. A short read raises."""
    dev.root()
    part = f"/dev/block/by-name/boot_{slot}"
    if nbytes is None:
        out = dev.sh(f"sha256sum {part}", timeout=120).split()
    else:
        size = int(dev.sh(f"blockdev --getsize64 {part}").strip() or 0)
        if size < nbytes:
            raise RuntimeError(f"{part} is {size} bytes, shorter than {nbytes}")
        out = dev.sh(f"head -c {int(nbytes)} {part} | sha256sum", timeout=120).split()
    if not out or len(out[0]) != 64:
        raise RuntimeError(f"could not hash {part}")
    return out[0]


def partition_build_ids(dev: Device) -> tuple[str | None, str | None]:
    """ro.river.build_id and ro.vendor.river.build_id read straight from the
    partitions' build.prop files, as root."""
    dev.root()
    sysp = dev.sh("grep -m1 '^ro.river.build_id=' /system/build.prop").strip()
    venp = dev.sh("grep -m1 '^ro.vendor.river.build_id=' /vendor/build.prop").strip()
    return (sysp.split("=", 1)[1] if "=" in sysp else None, venp.split("=", 1)[1] if "=" in venp else None)


# --- getting to fastboot, rollback and rescue -----------------------------------

def to_fastboot(dev: Device, seconds: float = 180) -> None:
    """Get to fastboot. If fastboot answers, use it. If adb answers (booted or
    not), write a marker when possible and `adb reboot bootloader`."""
    d = Deadline(seconds, "fastboot or adb to answer")
    while True:
        if dev.in_fastboot():
            return
        if dev.state() in ("device", "recovery"):
            st = dev.boot_status()
            if st and st.completed:
                reboots.leave_marker(dev)
            dev.reboot_adb("bootloader")
            dev.wait_fastboot(120)
            return
        if d.expired():
            raise Unreachable(f"{dev.serial}: neither adb nor fastboot answers. Someone has to hold the power "
                              "button. Pipeline stopped.")
        import time
        time.sleep(1)


def verify_slot_build(dev: Device, slot: str, build_id: str | None, boot_sha: str | None) -> str:
    """Check the running slot and build. Returns a description; raises on mismatch."""
    have = running_slot(dev)
    if have != slot:
        raise RuntimeError(f"running slot {have}, expected {slot}")
    if build_id:
        s, v = partition_build_ids(dev)
        if s != build_id or v != build_id:
            raise RuntimeError(f"build IDs system={s} vendor={v}, expected {build_id}")
        return f"slot {slot}, build ID {build_id}"
    if not boot_sha:
        raise RuntimeError("no build ID and no boot hash to verify the fallback with")
    got = boot_partition_sha(dev, slot)
    if got != boot_sha:
        raise RuntimeError(f"boot_{slot} hash {got[:12]} differs from the saved {boot_sha[:12]}")
    return f"slot {slot}, boot hash {boot_sha[:12]}"


def rollback(dev: Device, st: dict, reason: str, attempt_id: str | None = None) -> dict:
    """The one rollback procedure: --set-active the fallback (always
    known_good_slot), `fastboot erase userdata` (must succeed; recorded),
    reboot, then check the slot and build. Only then is the pair released.
    Works without a pair too (back to known_good_slot after a dev flash)."""
    pair = st.get("pair")
    kg = st["known_good_slot"]
    fallback = pair["fallback"] if pair else kg
    if fallback != kg:
        raise Halt(f"pair fallback {fallback} is not known_good_slot {kg}")
    attempt_id = attempt_id or new_id("rollback")
    log(f"Rollback to slot {fallback}: {reason}")
    to_fastboot(dev)
    dev.fastboot_guard()
    dev.fastboot_ok(f"--set-active={fallback}", timeout=60)
    r = dev.fastboot("erase", "userdata", timeout=300)
    if r.returncode != 0:
        raise Halt(f"rollback: fastboot erase userdata failed: {(r.stderr or r.stdout).strip()[-300:]}")
    seq = journal.record_wipe(qa_home(), attempt_id, "rollback")
    dev.reboot_fastboot()
    try:
        dev.wait_for_device(FIRST_BOOT_TIMEOUT)
        dev.wait_boot_completed(FIRST_BOOT_TIMEOUT)
        kgrec = st.get("known_good") or {}
        how = verify_slot_build(dev, fallback, kgrec.get("build_id"),
                                (pair or {}).get("fallback_boot_sha256") or kgrec.get("boot_sha256"))
    except (TimeoutError, RuntimeError) as e:
        raise Halt(f"rollback to slot {fallback} could not be verified: {e}") from e
    if pair:
        slots.rolled_back(st, fallback, f"{reason}; wipe #{seq}; {how}", now_iso())
    else:
        st.setdefault("history", []).append({"event": "returned-to-known-good", "at": now_iso(),
                                             "how": how, "reason": reason})
    save_state(st)
    log(f"Rolled back: {how}. The phone is wiped and sits in the setup wizard.")
    return {"attempt_id": attempt_id, "wipe_seq": seq, "verified": how}


def rescue(dev: Device, st: dict, reason: str, attempt_id: str | None = None) -> dict:
    """Slot rescue after a failed boot: get to fastboot (adb reboot bootloader
    if adb answers without boot completed), then the rollback procedure."""
    log(f"Slot rescue: {reason}")
    return rollback(dev, st, f"rescue: {reason}", attempt_id)


# --- the flash itself ---------------------------------------------------------------

def check_images(images_dir: Path, expected: dict | None) -> dict:
    """Hash every image before anything is written. Returns {name: sha}."""
    got = {}
    for name in IMAGES:
        p = images_dir / f"{name}.img"
        if not p.exists():
            raise SystemExit(f"error: missing {p}")
        got[name] = sha256_file(p)
        if expected is not None:
            want = (expected.get(name) or {}).get("sha256")
            if got[name] != want:
                raise SystemExit(f"error: {p} hash {got[name][:12]} does not match batch.json {str(want)[:12]}")
    return got


def write_images(dev: Device, images_dir: Path, target: str, wipe: bool, attempt: dict,
                 st: dict | None) -> None:
    dev.fastboot_guard()
    if st is not None:
        slots.mark_written(st, attempt["attempt_id"], now_iso())
        save_state(st)
    for name in IMAGES:
        log(f"  flash {name}_{target}")
        dev.fastboot_ok("flash", f"{name}_{target}", str(images_dir / f"{name}.img"), timeout=600)
    dev.fastboot_ok(f"--set-active={target}", timeout=60)
    if wipe:
        # Not fastboot -w: river's bootloader also tries to erase a metadata
        # partition the phone doesn't have, and fails.
        log("  erase userdata")
        r = dev.fastboot("erase", "userdata", timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"fastboot erase userdata failed: {(r.stderr or r.stdout).strip()[-300:]}")
        seq = journal.record_wipe(qa_home(), attempt["attempt_id"], "flash")
        attempt["wipe"] = {"ok": True, "seq": seq, "at": now_iso()}


def flash_dev(product: Path, wipe: bool, reboot: bool) -> int:
    """Day-to-day flash of an output directory (no batch)."""
    dev = Device(SERIAL)
    check_images(product, None)
    built_for = stage.read_prop(product / "system/build.prop", "ro.product.system.device") or \
        stage.read_prop(product / "system/build.prop", "ro.product.device")
    if built_for and built_for != CODENAME:
        raise SystemExit(f"error: {product} was built for {built_for!r}, not {CODENAME}")
    st = load_state()
    if st and st.get("pair"):
        raise SystemExit(f"error: batch {st['pair']['batch']} holds an unresolved slot pair; "
                         "resolve it first (flash --recover)")
    if dev.state() == "device":
        dev.guard()
        current = running_slot(dev)
    else:
        to_fastboot(dev, 30)
        dev.fastboot_guard()
        current = dev.getvar("current-slot").lstrip("_")
    if st:
        # Never overwrite the known-good slot.
        target = slots.other(st["known_good_slot"])
    else:
        target = slots.other(current)
    attempt = {"attempt_id": new_id("devflash"), "target": target}
    log(f"Flashing {product} to slot {target} (current slot: {current})")

    def in_fb():
        write_images(dev, product, target, wipe, attempt, None)

    if not reboot:
        to_fastboot(dev)
        in_fb()
        log(f"Done. Phone is in the bootloader, slot {target} active.")
        return EXIT_OK
    log("Waiting for boot (the first boot after a wipe takes a few minutes)")
    try:
        rec = reboots.planned_reboot(dev, "fastboot", boot_timeout=FIRST_BOOT_TIMEOUT, in_fastboot=in_fb)
    except TimeoutError as e:
        back = st["known_good_slot"] if st else current
        log(f"error: {e}. Roll back to the {'known-good' if st else 'previous'} slot: "
            f"flash --rollback, or fastboot -s {SERIAL} --set-active={back}")
        return EXIT_ERROR
    if st:
        st.setdefault("history", []).append({"event": "dev-flash", "at": now_iso(), "slot": target})
        save_state(st)
    log(f"Booted {dev.getprop('ro.lineage.display.version')} on slot {target} "
        f"in {rec.host_boot_seconds:.0f} s")
    if st:
        log(f"Note: known_good_slot is {st['known_good_slot']}; a batch flash first returns to it "
            "(flash --rollback).")
    return EXIT_OK


def flash_batch(batch_id: str, images_dir: Path | None, wipe: bool) -> int:
    batch = stage.load(batch_id)
    bdir = batch_dir(batch_id)
    images_dir = images_dir or bdir / "images"
    kind = batch["kind"]
    if not wipe:
        raise SystemExit("error: a batch flash always wipes (--wipe)")
    control = reboots.control_active()
    if control and kind == "batch":
        raise SystemExit(f"error: RIVERQA_CONTROL={control} on a kind 'batch' flash; controls use a "
                         "calibration or experiment batch")
    hashes = check_images(images_dir, batch["images"])
    dev = Device(SERIAL)
    dev.guard()
    current = running_slot(dev)
    st = load_state()
    if st is None and kind == "baseline":
        # First-run transition: the running slot becomes a provisional
        # known_good_slot, its boot hash checked live.
        sha = boot_partition_sha(dev, current)
        st = slots.first_run(current, sha, now_iso())
        save_state(st)
        log(f"First run: slot {current} is the provisional known-good slot (boot {sha[:12]})")
    errs = slots.preflight(st, batch_id, kind, current)
    if errs:
        for e in errs:
            log(f"error: {e}")
        return EXIT_ERROR
    kg = st["known_good_slot"]
    live = boot_partition_sha(dev, kg)
    saved = (st.get("known_good") or {}).get("boot_sha256")
    if saved and live != saved:
        log(f"error: boot_{kg} hash {live[:12]} differs from the known-good record {saved[:12]}; "
            "the known-good slot was changed outside the harness")
        return EXIT_HALT
    pair = slots.reserve(st, batch_id, kind, live, now_iso())
    save_state(st)
    attempt_id = new_id("flash")
    batch["pair"] = dict(pair)
    # A new flash attempt starts over (B1): earlier bench runs are archived
    # and never count toward this attempt's boots or evidence.
    if batch.get("bench_runs"):
        batch.setdefault("prior_bench_runs", []).append({"flash_attempt": batch.get("flash_attempt"),
                                                         "runs": batch["bench_runs"]})
    batch["bench_runs"] = []
    batch["flash_attempt"] = attempt_id
    stage.save(batch)
    target = pair["target"]

    flash_rec_path = bdir / "flash.json"
    frec = read_json(flash_rec_path, {"attempts": []})
    attempt = {"attempt_id": attempt_id, "started_at": now_iso(), "target": target,
               "fallback": pair["fallback"], "kind": kind, "image_sha256": hashes, "hashes_checked": True,
               "wipe": None, "first_boot": None, "result": "started"}
    frec["attempts"].append(attempt)
    write_json(flash_rec_path, frec)

    def save_attempt():
        write_json(flash_rec_path, frec)

    def in_fb():
        write_images(dev, images_dir, target, True, attempt, st)
        save_attempt()

    log(f"Batch {batch_id} ({kind}): flashing slot {target}; fallback {pair['fallback']}")
    try:
        rec = reboots.planned_reboot(dev, "fastboot", boot_timeout=FIRST_BOOT_TIMEOUT, in_fastboot=in_fb,
                                     inject_early_reboot=control == "extra-reboot-first-boot")
    except (TimeoutError, RuntimeError) as e:
        attempt["result"] = f"boot failed: {e}"
        save_attempt()
        try:
            r = rescue(dev, st, f"batch {batch_id}: {e}", attempt["attempt_id"])
            attempt["rescue"] = r
            attempt["result"] = "rescued; batch failed"
            save_attempt()
            return EXIT_RESCUED
        except (Halt, Unreachable, GuardError) as h:   # C7: a guard failure in rescue halts
            attempt["result"] = f"halt: {h}"
            save_attempt()
            log(f"HALT: {h}")
            return EXIT_HALT
    # Record the first boot's boot_id at once.
    attempt["first_boot"] = {"boot_id": rec.boot_id, "completed_at": now_iso(),
                             "host_boot_seconds": rec.host_boot_seconds}
    save_attempt()
    batch["first_boot_seconds"] = rec.host_boot_seconds
    if rec.control in ("early-reboot", "early-reboot-missed"):
        batch.setdefault("control_injections", []).append(
            {"control": control, "flash_attempt": attempt_id, "where": "first boot", "at": now_iso(),
             "injected": rec.control == "early-reboot"})
        if rec.control != "early-reboot":
            batch["control_injections"][-1].pop("flash_attempt")   # missed: nothing for QA to find
    stage.save(batch)
    # A bootloader that fell back to the old slot by itself fails the batch.
    slot_now = running_slot(dev)
    if slot_now != target:
        attempt["result"] = f"bootloader fell back to slot {slot_now}; batch failed"
        save_attempt()
        try:
            attempt["rescue"] = rollback(dev, st, f"bootloader fell back to slot {slot_now}", attempt["attempt_id"])
            save_attempt()
            return EXIT_RESCUED
        except (Halt, Unreachable, GuardError) as h:
            log(f"HALT: {h}")
            return EXIT_HALT
    reboots.judge_signals(dev, rec)
    ev_dir = bdir / "boots" / f"firstboot-{attempt['attempt_id']}"
    count = reboots.read_boot_count(dev, expected=1)
    signals = {"path": "fastboot", "chain": list(rec.chain), "uptime": list(rec.uptime_check),
               "boot_count": list(count), "reboot": rec.to_json()}
    meta = evidence.collect(dev, ev_dir, expected_boot_id=rec.boot_id,
                            context={"flash_attempt": attempt["attempt_id"], "batch": batch_id,
                                     "kind": "first-boot"})
    write_json(ev_dir / "signals.json", signals)
    attempt["first_boot"]["evidence"] = str(ev_dir.relative_to(bdir))
    attempt["first_boot"]["signals"] = {k: signals[k] for k in ("chain", "uptime", "boot_count")}
    attempt["result"] = "booted"
    save_attempt()
    log(f"Booted slot {target} in {rec.host_boot_seconds:.0f} s (boot_id {rec.boot_id[:8]}). "
        f"Signals: chain {rec.chain[0]}, uptime {rec.uptime_check[0]}, count {count[0]}. "
        f"Evidence boot_id {'matches' if meta['boot_id_at_collection'] == rec.boot_id else 'MISMATCH'}.")
    return EXIT_OK


def known_good_path() -> Path:
    return bench_path("qa", "known-good.json")


def finish_checkpoint(st: dict) -> bool:
    """Write known-good.json from a closed baseline's seed if the checkpoint
    was interrupted between the slot-state write and this one (C2). Returns
    True if it wrote anything."""
    seed = slots.checkpoint_pending(st)
    if seed is None:
        return False
    write_json(known_good_path(), seed)
    slots.seed_written(st, now_iso())
    save_state(st)
    log(f"Finished the baseline checkpoint of {st['baseline_closed']['batch']}: known-good.json written.")
    return True


def promote_batch(batch_id: str, runner=None, dev: Device | None = None) -> int:
    """flash --promote: make a merged and tagged batch's slot known-good.

    The fallback is released only with proof, from the same lookup and the
    same decision recovery uses, that every PR is merged and every repo has
    the tag at the merge commit (C1). Baseline pairs never come through here:
    they close only through phone-qa --baseline. Unstamped builds and builds
    not tied to the batch are refused (A4)."""
    st = load_state()
    if not st or not st.get("pair") or st["pair"]["batch"] != batch_id:
        log(f"error: no reserved pair for {batch_id}")
        return EXIT_ERROR
    pair = st["pair"]
    if pair["kind"] != "batch":
        log(f"error: {batch_id} holds a {pair['kind']} pair, which is never promoted. A baseline closes only "
            "through phone-qa --baseline; the other kinds roll back (flash --rollback).")
        return EXIT_ERROR
    batch = stage.load(batch_id)
    errs = stage.batch_identity_errors(batch)
    if errs:
        log(f"error: {batch_id}'s build is not tied to the batch: " + "; ".join(errs))
        return EXIT_ERROR
    look = recovery.lookup(batch, runner)
    decision, why = slots.decide_recovery(pair, look, batch, stage.repo_slugs())
    if decision != slots.PROMOTE:
        log(f"error: {batch_id} is not proven merged and tagged ({why}). The fallback slot stays reserved.")
        return EXIT_HALT if decision == slots.HALT else EXIT_ERROR
    dev = dev or Device(SERIAL)
    dev.guard()
    target = pair["target"]
    if running_slot(dev) != target:
        log(f"error: the phone is not running the candidate slot {target}")
        return EXIT_ERROR
    s, v = partition_build_ids(dev)
    if s != batch["build_id"] or v != batch["build_id"]:
        log(f"error: candidate build IDs {s}/{v} differ from batch.json {batch['build_id']}")
        return EXIT_ERROR
    sha = boot_partition_sha(dev, target)
    slots.promote(st, batch_id, s, sha, now_iso())
    save_state(st)
    log(f"Promoted {batch_id} ({why}): known_good_slot is now {target}")
    return EXIT_OK


def merge_started(batch_id: str) -> int:
    """flash --merge-started: the runner calls this, and waits for it to
    succeed, before any merge or tag action for the batch. It records
    merge_started in batch.json with an atomic, fsynced write, so recovery
    can tell a run that stopped before merging (roll back) from one
    interrupted mid-merge (halt). Idempotent: the first time is kept."""
    path = batch_dir(batch_id) / "batch.json"
    batch = read_json(path)
    if batch is None:
        log(f"error: no batch.json for {batch_id}")
        return EXIT_ERROR
    if batch.get("kind") != "batch":
        log(f"error: {batch_id} is a {batch.get('kind')} run, which is never merged")
        return EXIT_ERROR
    if not batch.get("merge_started"):
        batch["merge_started"] = {"at": now_iso()}
        write_json(path, batch)
    log(f"merge_started for {batch_id} at {batch['merge_started'].get('at')}")
    return EXIT_OK


def recover(runner=None, dev: Device | None = None) -> int:
    st = load_state()
    if st and finish_checkpoint(st):
        st = load_state()
    if not st or not st.get("pair"):
        log("No unresolved pair.")
        return EXIT_OK
    pair = st["pair"]
    batch = read_json(batch_dir(pair["batch"]) / "batch.json", {}) or {}
    look = None if pair["kind"] != "batch" else recovery.lookup(batch, runner)
    decision, why = slots.decide_recovery(pair, look, batch, stage.repo_slugs() if pair["kind"] == "batch" else {})
    log(f"Recovery for {pair['batch']} ({pair['kind']}): {decision}: {why}")
    st.setdefault("history", []).append({"event": "recovery-decision", "at": now_iso(),
                                         "batch": pair["batch"], "decision": decision, "why": why})
    save_state(st)
    if decision == slots.HALT:
        log("HALT: notify Zak.")
        return EXIT_HALT
    dev = dev or Device(SERIAL)
    try:
        if decision == slots.PROMOTE:
            return promote_batch(pair["batch"], runner, dev)
        dev.guard() if dev.state() == "device" else None
        rollback(dev, st, f"recovery: {why}")
        return EXIT_OK
    except (Halt, Unreachable, GuardError) as h:
        log(f"HALT: {h}")
        return EXIT_HALT


def show_status() -> int:
    st = load_state()
    if st is None:
        print("No slot-state.json yet (the first --baseline flash creates it).")
        return EXIT_OK
    kg = st.get("known_good") or {}
    print(f"known_good_slot: {st['known_good_slot']} ({'provisional' if kg.get('provisional') else 'confirmed'}; "
          f"build {kg.get('build_id')}; boot {str(kg.get('boot_sha256'))[:12]})")
    bc = st.get("baseline_closed")
    print("baseline closed: no" if not bc else f"baseline closed: {bc['batch']} at {bc['at']}"
          + ("" if bc.get("seed_written", True) else " (checkpoint interrupted: run flash --recover)"))
    p = st.get("pair")
    print("pair: none" if not p else f"pair: batch {p['batch']} ({p['kind']}), target {p['target']}, "
          f"fallback {p['fallback']}, written {p['written']}")
    return EXIT_OK


USAGE = """\
Install a build on the Moto G7 test phone with fastboot.

Usage:
  flash <baseline|stripped|PRODUCT_DIR> [--wipe] [--no-reboot]
  flash --batch ID [--images DIR] --wipe
  flash --rollback        Return to known_good_slot: --set-active, fastboot erase
                          userdata, reboot, verify slot and build; releases the pair.
  flash --recover         Resolve an unresolved pair: look up the batch's merge and
                          tag state (gh, git ls-remote) and promote, roll back or halt.
  flash --promote ID      The batch was merged and tagged: make its slot known-good.
                          Checks, for every repo of the tested commit set, that its
                          PR's head is the tested commit, the PR is merged and the
                          tag is at the merge commit, or with no PR that the tag is
                          at the tested commit (same lookup as --recover).
                          Never for a baseline (phone-qa --baseline closes those).
  flash --merge-started ID
                          Record in batch.json, durably, that merging has begun.
                          The runner runs this before any merge or tag action.
  flash --status          Show slot-state.json.

  baseline|stripped  Use $BENCH_HOME/out-<env>/target/product/river.
  PRODUCT_DIR        Any directory with boot.img, dtbo.img, system.img and vendor.img.
  --wipe       Factory reset (fastboot erase userdata; river's bootloader rejects -w).
  --no-reboot  Stay in the bootloader after flashing.
  --batch ID   Flash a staged batch ($BENCH_HOME/batches/ID). Checks every image hash
               before writing, fixes the slot pair in slot-state.json and batch.json
               before the first write, records the first boot's boot_id, signals and
               health evidence, and runs slot rescue if the boot fails.

The images go to the slot that is not the known-good one (or, before the first
baseline, the inactive slot), so the known-good slot stays as the fallback.
Every adb and fastboot wait has a deadline. Only the phone with serial
ZY225DPT7F is touched (fixed in the code; BENCH_DEVICE may only repeat it),
and only if it reports itself as river.
Every reboot command is guarded first.

Exit codes: 0 done, 1 error, 3 halted (needs Zak), 4 boot failed and the phone
was rescued back to the known-good slot (the batch failed).
"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="flash", usage=USAGE, add_help=False)
    ap.add_argument("src", nargs="?")
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--no-reboot", action="store_true")
    ap.add_argument("--batch")
    ap.add_argument("--images", type=Path)
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--recover", action="store_true")
    ap.add_argument("--promote")
    ap.add_argument("--merge-started")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    a = ap.parse_args(argv)
    if a.help:
        print(USAGE)
        return 0
    if a.status:
        return show_status()
    try:
        with DeviceLock():
            if a.rollback:
                st = load_state()
                if not st:
                    log("error: no slot-state.json")
                    return EXIT_ERROR
                dev = Device(SERIAL)
                try:
                    if dev.state() == "device":
                        dev.guard()
                    rollback(dev, st, "requested (flash --rollback)")
                except GuardError as e:   # mid-rollback: the pipeline halts (C7)
                    log(f"HALT: {e}")
                    return EXIT_HALT
                return EXIT_OK
            if a.recover:
                return recover()
            if a.promote:
                return promote_batch(a.promote)
            if a.merge_started:
                return merge_started(a.merge_started)
            if a.batch:
                return flash_batch(a.batch, a.images, a.wipe)
            if a.images and not a.src:
                return flash_dev(a.images, a.wipe, not a.no_reboot)
            if a.src in ("baseline", "stripped"):
                product = BENCH_HOME / f"out-{a.src}" / "target/product" / CODENAME
            elif a.src:
                product = Path(a.src)
            else:
                print(USAGE)
                return EXIT_ERROR
            return flash_dev(product, a.wipe, not a.no_reboot)
    except (LockError, GuardError) as e:
        log(f"error: {e}")
        return EXIT_ERROR
    except Halt as e:
        log(f"HALT: {e}")
        return EXIT_HALT
    except Unreachable as e:
        log(f"HALT: {e}")
        return EXIT_HALT
    except TimeoutError as e:
        log(f"error: {e}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
