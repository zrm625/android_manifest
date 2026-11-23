"""scripts/flash: install a build on the test phone, with the slot pair,
rollback and slot rescue from DESIGN.md ("The batch sequence", step 2)."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

from . import evidence, fbmode, journal, reboots, recovery, slots, stage
from .core import (BENCH_HOME, CODENAME, SERIAL, Deadline, batch_dir, bench_path, log, new_id,
                   now_iso, read_json, sha256_file, write_json)
from .device import Device, GuardError, Unreachable
from .lock import DeviceLock, LockError

IMAGES = ("boot", "dtbo", "system", "vendor")
STATE_FILE = "slot-state.json"
FIRST_BOOT_TIMEOUT = 900
BOOT_TIMEOUT = 600

EXIT_OK, EXIT_ERROR, EXIT_HALT, EXIT_RESCUED, EXIT_RESUMABLE = 0, 1, 3, 4, 75


class Halt(RuntimeError):
    """Stop the pipeline and notify Zak. ``advice``, if given, is what
    recovery_halt prints instead of its generic note (R7-2)."""

    def __init__(self, msg: str, advice: str | None = None):
        super().__init__(msg)
        self.advice = advice


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


# --- getting to fastboot, rollback and rescue -----------------------------------

def to_fastboot(dev: Device, seconds: float = 180) -> None:
    """Get to fastboot. If fastboot answers, use it. If adb answers (booted or
    not), write a marker when possible and `adb reboot bootloader`. It never
    boots a slot. The caller clears fb_mode once there (rollback,
    write_images, recover_fb_mode_to_known_good), before it sets a slot
    active (review round 5, R5-1)."""
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


def known_good_want(st: dict, *, boot_sha: str | None = None, hash_boot: bool = False) -> fbmode.Want:
    """What known_good_slot must run: its build IDs (both equal to the
    record's), or for a provisional record without one its boot hash."""
    kg = st["known_good_slot"]
    kgrec = st.get("known_good") or {}
    bid = fbmode.stamp(kgrec.get("build_id"))       # R10-1: the sentinel is no stamp
    return fbmode.Want(kg, (bid, bid) if bid else None,
                       boot_sha=boot_sha if boot_sha is not None else (None if bid else kgrec.get("boot_sha256")),
                       hash_boot=hash_boot)


def clear_then_set_active(dev: Device, slot: str, then: str = "flash --rollback") -> bool:
    """In fastboot: clear fb_mode, then set ``slot`` active (R5-1). Returns
    True if an fb_mode obligation is pending; the caller settles it with a
    VerifiedBoot of ``slot`` (R8-1). If the clear fails with an obligation pending, it
    still sets ``slot`` active, then halts (review round 5); ``then`` is the
    command the halt message says to run after clearing by hand (R7-2). A
    failed ``--set-active`` halts too (R6-1); the obligation stays pending."""
    try:
        owed = fbmode.clear(dev)
    except fbmode.ClearFailed as e:
        try:
            dev.fastboot_ok(f"--set-active={slot}", timeout=60)
            done = f"slot {slot} is set active"
        except (RuntimeError, TimeoutError) as e2:
            done = f"setting slot {slot} active failed too ({e2})"
        # R8-4: the note names the same command as the message.
        raise Halt(f"{e}; {done}. Clear it by hand (fastboot -s {dev.serial} oem fb_mode_clear), then run "
                   f"{then}.", advice=f"The fb_mode obligation stays pending in the journal; {then} finishes "
                   "it.") from e
    try:
        dev.fastboot_ok(f"--set-active={slot}", timeout=60)
    except Halt:
        raise
    except (RuntimeError, TimeoutError) as e:
        raise Halt(f"fastboot --set-active={slot} failed: {e}", advice=owed_note(then)) from e   # R9-11
    return owed


def recovery_halt(e: BaseException, advice: str | None = None) -> int:
    """The one way slot recovery stops on a failure (R6-1): a clean
    ``error:`` line, what to do next, and exit 3. Nothing is resolved
    here. ``advice`` replaces the generic note on a pending fb_mode
    obligation where ``flash --recover`` is not the right next step (a
    promotion, R7-2). An empty ``advice`` (here or on a Halt) means the
    message already said everything."""
    log(f"error: {e}")
    if advice is None:
        advice = getattr(e, "advice", None)
    if advice:
        log(advice)
    elif advice is None and fbmode.pending() is not None:
        log("The fb_mode obligation stays pending in the journal; flash --recover finishes it.")
    log("HALT: notify Zak.")
    return EXIT_HALT


def owed_note(then: str) -> str | None:
    """The note on a pending obligation, naming the command that finishes
    it (R8-4), or None with nothing pending."""
    if fbmode.pending() is None:
        return None
    return f"The fb_mode obligation stays pending in the journal; {then} finishes it."


def promote_halt(e: BaseException, batch_id: str, target: str, fallback: str) -> int:
    """A promotion stopped on the phone (R7-1, R7-2): the batch is already
    proven merged and tagged, so the pair must survive. Never advise
    ``flash --rollback`` here: it wipes userdata and releases the pair."""
    owed = fbmode.pending() is not None
    return recovery_halt(e, advice=(
        f"Promotion of {batch_id} stopped: the phone must run candidate slot {target} with the batch's build. "
        f"The slot pair stays reserved (fallback {fallback})"
        + (" and the fb_mode obligation stays pending" if owed else "") + ". The batch is already merged and "
        "tagged: do not run flash --rollback (it wipes userdata and releases the pair). Zak decides how to bring "
        f"the phone back to slot {target}, then runs flash --promote {batch_id} again."))


def rollback(dev: Device, st: dict, reason: str, attempt_id: str | None = None,
             then: str = "flash --rollback") -> dict:
    """The one rollback procedure: in fastboot, clear fb_mode, --set-active
    the fallback (always known_good_slot), `fastboot erase userdata` (must
    succeed; recorded), reboot, then verify_boot the slot and build. Only
    then is any fb_mode obligation settled (with that proof, R8-1) and the
    pair released. It never boots another slot first (R5-1). Works without
    a pair too (back to known_good_slot after a dev flash). ``then`` is the
    command a halt names to finish it."""
    pair = st.get("pair")
    kg = st["known_good_slot"]
    fallback = pair["fallback"] if pair else kg
    if fallback != kg:
        raise Halt(f"pair fallback {fallback} is not known_good_slot {kg}")
    attempt_id = attempt_id or new_id("rollback")
    log(f"Rollback to slot {fallback}: {reason}")
    # Every failure before the reboot halts (R6-1): a fastboot error, a
    # timeout, a guard failure or an unreachable phone. The pair and any
    # fb_mode obligation stay as they are.
    try:
        to_fastboot(dev)
        dev.fastboot_guard()
        clear_then_set_active(dev, fallback, then=then)
        r = dev.fastboot("erase", "userdata", timeout=300)
        if r.returncode != 0:
            raise Halt(f"rollback: fastboot erase userdata failed: {(r.stderr or r.stdout).strip()[-300:]}",
                       advice=owed_note(then))                                           # R9-11
        seq = journal.record_wipe(qa_home(), attempt_id, "rollback")
        dev.reboot_fastboot()
    except Halt:
        raise
    except (RuntimeError, TimeoutError) as e:
        raise Halt(f"rollback to slot {fallback} stopped before the reboot: {e}", advice=owed_note(then)) from e
    try:
        dev.wait_for_device(FIRST_BOOT_TIMEOUT)
        dev.wait_boot_completed(FIRST_BOOT_TIMEOUT)
        want = known_good_want(st)
        if not want.build_ids:
            want = known_good_want(st, boot_sha=(pair or {}).get("fallback_boot_sha256") or want.boot_sha)
        proof = fbmode.verify_boot(dev, want)
        fbmode.settle(proof)
    except (TimeoutError, RuntimeError) as e:
        raise Halt(f"rollback to slot {fallback} could not be verified: {e}", advice=owed_note(then)) from e
    how = proof.describe()
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


class WipeFailed(RuntimeError):
    """``fastboot erase userdata`` failed or timed out after
    ``--set-active`` (R10-5), or ``--set-active`` itself timed out (R11-2):
    the target slot is, or may be, active and its wipe is not confirmed."""


def write_images(dev: Device, images_dir: Path, target: str, wipe: bool, attempt: dict,
                 st: dict | None) -> None:
    dev.fastboot_guard()
    # In fastboot: clear any fb_mode, so the new build boots (review round
    # 4). Never settled here (R8-1): a pending obligation is settled by the
    # caller with a VerifiedBoot of the slot written. A failed clear with
    # none pending is logged and the flash goes on (R5-1).
    fbmode.clear(dev)
    if st is not None:
        slots.mark_written(st, attempt["attempt_id"], now_iso())
        save_state(st)
    for name in IMAGES:
        log(f"  flash {name}_{target}")
        dev.fastboot_ok("flash", f"{name}_{target}", str(images_dir / f"{name}.img"), timeout=600)
    try:
        dev.fastboot_ok(f"--set-active={target}", timeout=60)
    except TimeoutError as e:
        # R11-2: a timeout leaves it unknown whether slot target is active.
        raise WipeFailed(f"fastboot --set-active={target} timed out; slot {target} may be active"
                         + ("" if not wipe else " and userdata was not erased") + f": {e}") from e
    if wipe:
        # Not fastboot -w: river's bootloader also tries to erase a metadata
        # partition the phone doesn't have, and fails.
        log("  erase userdata")
        try:
            r = dev.fastboot("erase", "userdata", timeout=300)
        except TimeoutError as e:
            # R11-2: slot target is active; whether userdata was erased is unknown.
            raise WipeFailed(f"fastboot erase userdata timed out; the wipe status is unknown: {e}") from e
        if r.returncode != 0:
            raise WipeFailed(f"fastboot erase userdata failed: {(r.stderr or r.stdout).strip()[-300:]}")
        seq = journal.record_wipe(qa_home(), attempt["attempt_id"], "flash")
        attempt["wipe"] = {"ok": True, "seq": seq, "at": now_iso()}


def product_want(product: Path, slot: str) -> fbmode.Want:
    """What a dev flash of ``product`` to ``slot`` must boot (R8-1): both
    build IDs from the images' build.prop files, or for an unstamped build
    (no stamp, or the ``unstamped`` sentinel, on either partition: R10-1)
    the hash of boot.img against the start of boot_<slot>."""
    sid = fbmode.stamp(stage.read_prop(product / "system/build.prop", "ro.river.build_id"))
    vid = fbmode.stamp(stage.read_prop(product / "vendor/build.prop", "ro.vendor.river.build_id"))
    if sid and vid:
        return fbmode.Want(slot, (sid, vid))
    img = product / "boot.img"
    return fbmode.Want(slot, None, boot_sha=sha256_file(img), boot_bytes=img.stat().st_size)


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
    owed = fbmode.pending()
    # The flash to name in advice, keeping the caller's --wipe (Sol, round 13).
    again = "flash stripped --wipe" if wipe else "flash stripped"
    if st is None and owed is not None and not reboot:
        # R10-3: before the first baseline, a flash that leaves the obligation
        # pending also leaves the slot it was armed on (the only slot known to
        # boot) as the next flash's target. Refused before the phone is
        # touched.
        log("error: an fb_mode obligation is pending and there is no known-good slot yet (no slot-state.json), "
            "so flash --no-reboot is refused: it would leave the obligation pending with another slot active, "
            "and the next flash could write the only slot known to boot.")
        log(armed_way_back(owed, None, again))
        return EXIT_ERROR
    # Only now, with the inputs and the pair checked (R7-3), is the phone
    # touched. R9-4: the active slot is read first, before anything is
    # cleared; before the first baseline it chooses the write target, so an
    # unreadable one halts with nothing changed. R10-4: a failed read is a
    # clean stop: HALT 3 with an obligation pending or before the first
    # baseline (a guard failure only with an obligation pending), else exit
    # 1; Unreachable goes to main, which halts.
    found = dev.state()
    if found == "unauthorized":
        # Sol round 14: adb answers but the phone hasn't authorized this
        # host. Nothing can read or reboot it, so ask for authorization.
        msg = (f"{SERIAL} is connected but adb is unauthorized; accept the USB-debugging prompt on the phone "
               "(tick \"Always allow\"), then run the flash again")
        if owed is not None:
            return recovery_halt(RuntimeError(msg), advice=pre_advice_for(owed, st, again))
        log(f"error: {msg}.")
        return EXIT_ERROR
    source = "getprop" if found == "device" else "fastboot"
    # R12-1: before the first baseline, what may be overwritten is decided
    # by the active slot alone, so it must be shown to boot: the phone found
    # in Android with sys.boot_completed=1.
    booted = False
    # Before the first baseline with an obligation pending, every halt gives
    # the R10-3/R11-1 way back, never flash --recover (Sol, round 12).
    pre_advice = pre_advice_for(owed, st, again)
    try:
        if source == "getprop":
            dev.guard()
            got, booted = dev.slot_boot_state()     # R13-1: one call, one boot
        else:
            to_fastboot(dev, 30)
            dev.fastboot_guard()
            got = dev.getvar("current-slot")
    except Unreachable:
        raise
    except (RuntimeError, TimeoutError) as e:
        if owed is not None or (st is None and not isinstance(e, GuardError)):
            return recovery_halt(RuntimeError(f"cannot read the active slot: {e}"), advice=pre_advice)
        log(f"error: cannot read the active slot: {e}")
        return EXIT_ERROR
    current = got.lstrip("_")
    if st:
        # Never overwrite the known-good slot.
        target = slots.other(st["known_good_slot"])
    elif current in slots.SLOTS:
        target = slots.other(current)
    else:
        return recovery_halt(fbmode.SlotUnreadable(dev.serial, got, source), advice=pre_advice)
    if st is None and not booted:
        # R11-1, R12-1: with no known-good slot yet, slot {current} is kept
        # only because it is active, so it must be shown to boot before the
        # other slot is written. adb answering is not enough: the phone must
        # have completed boot. This also stops a second --no-reboot flash
        # from writing the original slot before the first one has booted.
        where = {"device": "in Android but has not completed boot",
                 "recovery": "now in fastboot (found in recovery, moved to fastboot to read the slot)",
                 "unauthorized": "now in fastboot (found with adb unauthorized, moved to fastboot to read the slot)"}.get(
            found, "in fastboot")
        msg = (f"slot {current} is active and the phone is {where}; with no known-good slot yet, nothing "
               f"shows that slot {current} boots, so this flash would not write slot {target}")
        if owed is not None:
            return recovery_halt(RuntimeError(msg), advice=armed_way_back(owed, current, again))
        log(f"error: {msg}.")
        log(("If the phone is still booting, wait until it finishes and run the flash again. Otherwise b"
             if source == "getprop" else "B")
            + f"oot the phone to Android on a slot that works and let it finish booting (for slot X: "
            + (f"adb -s {SERIAL} reboot bootloader, then " if source == "getprop" else "")
            + f"fastboot -s {SERIAL} --set-active=X, then fastboot -s {SERIAL} reboot), then run the flash again.")
        return EXIT_ERROR
    if st is None and owed is not None and fbmode.armed_slot(owed) == target:
        # R10-3: before the first baseline the armed slot is the only slot
        # known to boot. Refused before anything is cleared or written.
        return recovery_halt(RuntimeError(
            f"slot {current} is active, so this flash would write slot {target}, where the pending fb_mode "
            f"obligation was armed. With no known-good slot yet, slot {target} is the only slot known to boot; "
            "not writing it"), advice=armed_way_back(owed, current, again))
    # A leftover fb_mode would stop every boot in fastboot. A dev flash
    # only needs fastboot, so this never boots (R5-16, R6-4): the active
    # slot may be one that doesn't boot. It clears and never settles: the
    # obligation stays pending until the boot of the slot written here is
    # verified below (R8-1).
    try:
        fbmode.recover(dev, BOOT_TIMEOUT, boot=False)
    except (RuntimeError, TimeoutError) as e:     # R5-11, R6-1: HALT, not EXIT_ERROR
        return recovery_halt(RuntimeError(f"fb_mode recovery failed: {e}"), advice=pre_advice)
    want = product_want(product, target)
    attempt = {"attempt_id": new_id("devflash"), "target": target}
    log(f"Flashing {product} to slot {target} (current slot: {current or '?'})")
    back = st["known_good_slot"] if st else current

    def in_fb():
        write_images(dev, product, target, wipe, attempt, None)

    def way_back(active: str | None) -> str:
        """Where to go with the obligation pending (R10-3): before the first
        baseline never a flash or flash --rollback."""
        if st is None:
            return armed_way_back(fbmode.pending(), active, again)
        return ("The fb_mode obligation stays pending. Flash again, or return with flash --rollback (it wipes "
                "userdata).")

    def write_failed(e: RuntimeError) -> int:
        # R9-5: write_images clears again, and a failed clear with the
        # obligation pending raises ClearFailed before any image is written;
        # that, or any other fastboot failure on the way, halts cleanly with
        # the obligation pending instead of escaping as a traceback.
        if isinstance(e, fbmode.ClearFailed):
            if st is None:
                # Gemini round 13: before the first baseline a retry needs a
                # booted phone (R12-1); nothing was written, so the R10-3 way
                # back applies.
                return recovery_halt(e, advice=f"Clear fb_mode by hand (fastboot -s {SERIAL} oem fb_mode_clear). "
                                     + way_back(current))
            return recovery_halt(e, advice=f"The fb_mode obligation stays pending. Clear it by hand (fastboot -s "
                                 f"{SERIAL} oem fb_mode_clear), then run the flash again.")
        if isinstance(e, WipeFailed):
            # R10-5, R11-2: slot target is (or may be) active and its wipe is
            # not confirmed; always a halt.
            # R12-2: the advice keeps the caller's --wipe choice and starts
            # by confirming which slot is active.
            check = f"Check which slot is active first (fastboot -s {SERIAL} getvar current-slot). "
            if wipe:
                state = f"Slot {target} is, or may be, active, and its userdata wipe is not confirmed. "
                own = (f"If slot {target} is active, erase userdata by hand (fastboot -s {SERIAL} erase userdata) "
                       f"and reboot (fastboot -s {SERIAL} reboot), or go back with fastboot -s {SERIAL} "
                       f"--set-active={back}.")
            else:
                state = f"Slot {target} may be active (this flash was run without --wipe). "
                own = (f"If slot {target} is active, reboot it (fastboot -s {SERIAL} reboot) or go back with "
                       f"fastboot -s {SERIAL} --set-active={back}; if slot {back} is still active, reboot it.")
            return recovery_halt(RuntimeError(f"the dev flash of slot {target} stopped at --set-active or the "
                                              f"wipe: {e}"),
                                 advice=state + check + (way_back(target) if fbmode.pending() is not None else own))
        if fbmode.pending() is not None:
            return recovery_halt(RuntimeError(f"the dev flash of slot {target} stopped: {e}"), advice=way_back(None))
        log(f"error: the dev flash of slot {target} stopped: {e}")
        return EXIT_ERROR

    if not reboot:
        try:
            to_fastboot(dev)
            in_fb()
        except Unreachable:            # R10-4: main halts (3)
            raise
        except RuntimeError as e:
            return write_failed(e)
        log(f"Done. Phone is in the bootloader, slot {target} active.")
        e = fbmode.pending()
        if e is not None:
            log(no_reboot_note(e, want))
        return EXIT_OK
    log("Waiting for boot (the first boot after a wipe takes a few minutes)")
    try:
        rec = reboots.planned_reboot(dev, "fastboot", boot_timeout=FIRST_BOOT_TIMEOUT, in_fastboot=in_fb)
    except Unreachable:                # R10-4
        raise
    except RuntimeError as e:
        return write_failed(e)
    except TimeoutError as e:
        if fbmode.pending() is not None:
            if st is None:
                return recovery_halt(e, advice=way_back(None))
            return recovery_halt(e, advice=f"The fb_mode obligation stays pending. Roll back with flash --rollback "
                                 f"(it wipes userdata), or fastboot -s {SERIAL} --set-active={back} and phone-qa "
                                 "recover.")
        log(f"error: {e}. Roll back to the {'known-good' if st else 'previous'} slot: "
            + (f"flash --rollback, or fastboot -s {SERIAL} --set-active={back}" if st else
               f"fastboot -s {SERIAL} --set-active={back}"))
        return EXIT_ERROR
    # R8-1: the boot of the slot written must be verified, and that proof
    # alone settles a pending obligation. R9-5: the version shown below is
    # read before the settle; nothing is read from the phone after it.
    try:
        proof = fbmode.verify_boot(dev, want)
        version = dev.getprop("ro.lineage.display.version") or "?"
        fbmode.settle(proof)
    except (RuntimeError, TimeoutError) as e:
        if fbmode.pending() is not None:
            return recovery_halt(RuntimeError(f"the dev flash of slot {target} could not be verified: {e}"),
                                 advice=way_back(None))
        log(f"error: the dev flash of slot {target} could not be verified: {e}. "
            + (f"Flash again, or return to slot {back} with flash --rollback." if st else
               f"Return to slot {back} with fastboot -s {SERIAL} --set-active={back}."))
        return EXIT_ERROR
    if st:
        st.setdefault("history", []).append({"event": "dev-flash", "at": now_iso(), "slot": target})
        save_state(st)
    log(f"Booted {version} on slot {target} in {rec.host_boot_seconds:.0f} s ({proof.describe()})")
    if st:
        log(f"Note: known_good_slot is {st['known_good_slot']}; a batch flash first returns to it "
            "(flash --rollback).")
    return EXIT_OK


def pre_advice_for(owed: dict | None, st: dict | None, again: str) -> str | None:
    """The advice for a halt before any write: before the first baseline
    with an obligation pending, the R10-3 way back; else recovery_halt's
    default."""
    return armed_way_back(owed, None, again) if st is None and owed is not None else None


def armed_way_back(e: dict | None, active: str | None, again: str = "flash stripped") -> str:
    """Before the first baseline, with an obligation pending: the way back
    (R10-3). The slot it was armed on is the only slot known to boot, so the
    advice never names a flash that could write it, nor flash --rollback
    (it needs slot-state.json). ``active`` is the active slot if known."""
    slot = fbmode.armed_slot(e or {})
    if slot is None:
        # R11-1: the entry doesn't say which slot still boots, so no flash
        # is advised until the operator has found one by hand and left it
        # active (a dev flash then writes the other slot).
        return ("The fb_mode obligation stays pending. It records no slot (armed before round 8), so the harness "
                "can't tell which slot still boots, and it advises no flash until you have found one. In fastboot: "
                f"fastboot -s {SERIAL} oem fb_mode_clear, then for a slot X, fastboot -s {SERIAL} --set-active=X "
                f"and fastboot -s {SERIAL} reboot, and check that X reaches Android. If one does, leave it active "
                f"and run {again} (without --no-reboot): it writes the other slot and settles the "
                "obligation once that boot is verified. If no slot boots, stop and tell Zak: don't flash.")
    if active == slot:
        first = ""
    elif active:
        first = (f"set slot {slot} active by hand (from Android, adb -s {SERIAL} reboot bootloader first; then "
                 f"fastboot -s {SERIAL} --set-active={slot}), then ")
    else:
        first = (f"from Android, adb -s {SERIAL} reboot bootloader first; then if fastboot -s {SERIAL} getvar "
                 f"current-slot reports another slot, set slot {slot} active by hand (fastboot -s {SERIAL} "
                 f"--set-active={slot}), then ")
    try:
        fbmode.armed_want(e)
        then = (f"run phone-qa recover: it boots slot {slot}, verifies it against the armed identity and settles "
                "the obligation. Then flash again.")
    except RuntimeError:
        # R10-1: an identity generic recovery can't verify (the unstamped
        # sentinel): the verified boot of a dev flash of the other slot
        # settles it.
        # R13 (Opus m1, Sol 2): before the first baseline a dev flash needs
        # the phone booted (R12-1). phone-qa recover clears fb_mode and boots
        # the recorded slot to completion, leaving the obligation pending.
        then = (f"run phone-qa recover: it clears fb_mode, boots slot {slot} to completion and leaves the "
                f"obligation pending. Then run {again}: it writes slot {slots.other(slot)} and settles the "
                f"obligation once that boot is verified. If slot {slot} doesn't reach Android, stop and tell Zak: "
                "don't flash.")
    how = first + then
    return (f"The fb_mode obligation stays pending. With no known-good slot yet, slot {slot} (where it was armed) "
            f"is the only slot known to boot. {how[:1].upper()}{how[1:]}")


def no_reboot_note(e: dict, want: fbmode.Want) -> str:
    """After flash --no-reboot with an obligation pending (R10-7): whether
    generic recovery can settle it, which it can only if the slot written,
    now active, and its build IDs are the armed identity."""
    try:
        armed = fbmode.armed_want(e)
    except RuntimeError:
        armed = None
    if armed is not None and armed.slot == want.slot and armed.build_ids == want.build_ids:
        return (f"The fb_mode obligation stays pending: nothing was booted. Slot {want.slot} with build IDs "
                f"{'/'.join(want.build_ids)} is what it was armed on, so generic recovery (phone-qa recover, or "
                "the preflight of phone-qa --quick) boots it, verifies it and settles the obligation.")
    return ("The fb_mode obligation stays pending: nothing was booted. A flash that boots (without --no-reboot) "
            "or flash --rollback settles it once it has verified the slot it boots. Until then phone-qa --quick "
            "halts (exit 3): the phone no longer runs the build the obligation was armed on.")


def first_run_proof(dev: Device) -> fbmode.VerifiedBoot:
    """Before the first baseline (no slot-state.json): a VerifiedBoot of the
    running slot with its whole boot partition hashed (R9-1). With an
    obligation pending, recovery verifies the armed identity and settles
    with this proof. With none, a phone found in fastboot is booted
    (nothing to settle), and the running build is verified as it is. It
    must be stamped: the bootstrap starts with a dev flash of a build from
    this tree (R9-3)."""
    e = fbmode.pending()
    if e is not None:
        want = dataclasses.replace(fbmode.armed_want(e), hash_boot=True)
        return fbmode.recover(dev, BOOT_TIMEOUT, boot=True, want=want).proof
    fbmode.recover(dev, BOOT_TIMEOUT, boot=True)
    slot, ids = fbmode.running_identity(dev)
    return fbmode.verify_boot(dev, fbmode.Want(slot, ids, hash_boot=True))


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
    # R8-3: everything that can be decided from disk is decided before the
    # phone is touched: the pair, a missing or unclosed baseline, an
    # interrupted baseline checkpoint.
    st = load_state()
    errs = slots.preflight(st, batch_id, kind)
    if errs:
        for e in errs:
            log(f"error: {e}")
        return EXIT_ERROR
    dev = Device(SERIAL)
    # Only now is the phone touched (R7-3). A leftover fb_mode would stop
    # every boot in fastboot. A batch flash needs Android on known_good_slot
    # with its build, so recovery boots that slot, also from a phone found
    # in fastboot with nothing pending (R6-8), and halts rather than boot
    # another active slot. The proof of that boot (slot, build IDs, the live
    # boot hash checked against the known-good record) settles any
    # obligation and is what the pair is reserved with (R8-1).
    if st is None:
        # First-run transition (a baseline, before any slot-state.json): the
        # running slot becomes a provisional known_good_slot. R9-1: its slot
        # and boot hash come from the proof, never from a read after the
        # settle. A hash timeout halts with the obligation pending and no
        # slot-state.json written.
        try:
            proof = first_run_proof(dev)
        except fbmode.Unstamped as e:      # R9-3: nothing pending, nothing written
            log(f"error: {e}")
            return EXIT_ERROR
        except (RuntimeError, TimeoutError) as e:
            return recovery_halt(RuntimeError(f"the running slot could not be verified: {e}"))
        live = proof.boot_sha
        st = slots.first_run(proof.slot, live, now_iso())
        save_state(st)
        log(f"First run: slot {proof.slot} is the provisional known-good slot ({proof.describe()})")
    else:
        kg = st["known_good_slot"]
        saved = (st.get("known_good") or {}).get("boot_sha256")
        try:
            proof = fbmode.recover(dev, BOOT_TIMEOUT, boot=True,
                                   want=known_good_want(st, boot_sha=saved, hash_boot=True)).proof
        except (fbmode.WrongSlot, fbmode.SlotMismatch) as e:
            # R8-2: a confirmed other slot only (in fastboot or in Android);
            # an unreadable one gets the generic halt below.
            return recovery_halt(e, advice=(
                f"No slot pair is reserved, so flash --rollback returns the phone to known_good_slot {kg} (it "
                "wipes userdata)" + ("; the fb_mode obligation stays pending until then" if fbmode.pending()
                                     else "") + "."))
        except (RuntimeError, TimeoutError) as e:     # R5-11, R6-1: HALT, not EXIT_ERROR
            return recovery_halt(RuntimeError(f"known_good_slot {kg} could not be verified: {e}"))
        live = proof.boot_sha
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
            return recovery_halt(h)                     # R7-5: the error: line and the obligation note
    # Record the first boot's boot_id at once.
    attempt["first_boot"] = {"boot_id": rec.boot_id, "completed_at": now_iso(),
                             "host_boot_seconds": rec.host_boot_seconds}
    save_attempt()
    batch["first_boot_seconds"] = rec.host_boot_seconds
    if rec.control in ("early-reboot", "early-reboot-missed"):
        batch.setdefault("control_injections", []).append(
            {"control": control, "flash_attempt": attempt_id, "where": "first boot", "at": now_iso(),
             "injected": rec.control == "early-reboot", "boot_id": rec.boot_id})
        if rec.control != "early-reboot":
            batch["control_injections"][-1].pop("flash_attempt")   # missed: nothing for QA to find
    stage.save(batch)
    # A bootloader that fell back to the old slot by itself fails the batch.
    slot_now = running_slot(dev)
    if slot_now != target:
        attempt["result"] = f"bootloader fell back to slot {slot_now}; batch failed"
        save_attempt()
        try:
            # I3: the same rescue path as a failed boot, so both exit-4 paths
            # leave the same evidence (a "rescue:" rolled-back event whose wipe
            # carries this attempt ID).
            attempt["rescue"] = rescue(dev, st, f"batch {batch_id}: bootloader fell back to slot {slot_now}",
                                       attempt["attempt_id"])
            attempt["result"] = "rescued; batch failed"
            save_attempt()
            return EXIT_RESCUED
        except (Halt, Unreachable, GuardError) as h:
            attempt["result"] = f"halt: {h}"
            save_attempt()
            return recovery_halt(h)                     # R7-5
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


def github_lag(look: dict, batch: dict, runner=None) -> list[str] | None:
    """Expected PRs that GitHub still reports OPEN at the tested head on
    river-22.2 although their merge has landed (I4): the repo's tag is
    present (the runner tags only after it saw the PR MERGED) and the tagged
    commit is a merge whose parents include the tested head. Returns the
    lagging PRs, or None if a parent read failed (also a transport case)."""
    frozen = slots.frozen_pr_set(batch) or {}
    cs = batch.get("commit_set") or {}
    want = {(e["repo"], e["number"]): cs.get(path) for path, e in frozen.items() if e.get("number") is not None}
    tags = {t.get("repo"): t for t in look.get("tags") or []}
    runner = runner or recovery.default_runner
    out = []
    for p in look.get("prs") or []:
        sha = want.get((p.get("repo"), p.get("number")))
        t = tags.get(p.get("repo")) or {}
        if not (sha and p.get("state") == "OPEN" and p.get("head") == sha and p.get("base") == slots.BRANCH
                and t.get("present") and t.get("commit")):
            continue
        try:
            r = runner(["gh", "api", f"repos/{p['repo']}/git/commits/{t['commit']}", "--jq", "[.parents[].sha]"])
            parents = json.loads(r.stdout) if r.returncode == 0 else None
        except (OSError, TimeoutError, ValueError):
            parents = None
        if not isinstance(parents, list):
            return None
        if len(parents) == 2 and parents[1] == sha:
            out.append(f"{p['repo']}#{p['number']}")
    return out


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
    slugs = stage.repo_slugs()
    look = recovery.lookup(batch, runner, slugs)
    # H11: a lookup that could not finish never touches the phone.
    if look.outcome == recovery.TRANSPORT:
        log(f"error: lookup failed in transport ({look.evidence}); nothing touched the phone. Run flash --promote "
            f"{batch_id} again.")
        return EXIT_RESUMABLE
    if look.outcome != recovery.OK:
        log(f"error: lookup {look.outcome} ({look.evidence}); nothing touched the phone.")
        print(json.dumps({"lookup": look.outcome, "repo": (look.evidence or {}).get("repo"),
                          "evidence": look.evidence}), flush=True)
        return EXIT_ERROR
    decision, why = slots.decide_recovery(pair, look.data, batch, slugs)
    if decision != slots.PROMOTE:
        lag = github_lag(look.data, batch, runner)
        if lag is None:
            log(f"error: {batch_id}: a PR is still reported open and the tag's parents could not be read; "
                "nothing touched the phone. Run flash --promote again later.")
            return EXIT_RESUMABLE
        if lag:
            # I4: GitHub still reports a PR open whose merge the runner saw
            # land: a lagging read, retried later, never a halt.
            log(f"error: {batch_id}: GitHub still reports {', '.join(lag)} open at the tested head; not proven "
                "yet. Nothing touched the phone; run flash --promote again later.")
            return EXIT_RESUMABLE
        log(f"error: {batch_id} is not proven merged and tagged ({why}). The fallback slot stays reserved.")
        return EXIT_HALT if decision == slots.HALT else EXIT_ERROR
    dev = dev or Device(SERIAL)
    target = pair["target"]

    # The phone is touched only now, after the host-side proof. A leftover
    # fb_mode, or a phone sitting in fastboot, is brought to Android on the
    # candidate slot and nowhere else (R6-8). verify_boot checks the guard,
    # the candidate slot, both build IDs and hashes boot_<target>; only that
    # proof settles an obligation, and the promotion uses the values on it
    # (R8-1). Every failure, including a timeout on any of those reads,
    # halts with the pair and any fb_mode obligation intact (R6-1, R7-8),
    # with promotion advice (R7-2).
    bid = batch["build_id"]
    try:
        proof = fbmode.recover(dev, BOOT_TIMEOUT, boot=True,
                               want=fbmode.Want(target, (bid, bid), hash_boot=True)).proof
    except (RuntimeError, TimeoutError) as e:
        return promote_halt(e, batch_id, target, pair["fallback"])
    slots.promote(st, batch_id, proof.build_ids[0], proof.boot_sha, now_iso())
    save_state(st)
    log(f"Promoted {batch_id} ({why}): known_good_slot is now {target}")
    return EXIT_OK


def merge_started(batch_id: str) -> int:
    """flash --merge-started: the runner calls this, and waits for it to
    succeed, before any merge or tag action for the batch. It records
    merge_started in batch.json with an atomic, fsynced write, so recovery
    can tell a run that stopped before merging (roll back) from one
    interrupted mid-merge (halt).

    It also freezes the complete expected PR set (review round 3): every
    repo of the commit set with its PR number from ``prs`` or an explicit
    entry in ``no_pr``. It refuses (and the runner must not merge) if any
    repo has neither. Idempotent: the first record is kept, and a later
    call whose ``prs``/``no_pr`` give a different set is refused."""
    path = batch_dir(batch_id) / "batch.json"
    batch = read_json(path)
    if batch is None:
        log(f"error: no batch.json for {batch_id}")
        return EXIT_ERROR
    if batch.get("kind") != "batch":
        log(f"error: {batch_id} is a {batch.get('kind')} run, which is never merged")
        return EXIT_ERROR
    expected, errs = slots.expected_pr_set(batch, stage.repo_slugs())
    ms = batch.get("merge_started")
    if ms:
        frozen = slots.frozen_pr_set(batch)
        if not frozen:
            log(f"error: {batch_id}'s merge_started has no expected PR set; recovery will halt")
            return EXIT_ERROR
        if expected != frozen:
            log(f"error: {batch_id}'s PR record changed after merge_started: "
                + ("; ".join(errs) if errs else "a different PR set"))
            return EXIT_ERROR
    else:
        if expected is None:
            log(f"error: {batch_id} has no complete PR set; record every repo of the commit set in prs "
                "or no_pr before merging: " + "; ".join(errs[:8]))
            return EXIT_ERROR
        batch["merge_started"] = {"at": now_iso(), "expected_prs": expected}
        write_json(path, batch)
    n = sum(1 for e in batch["merge_started"]["expected_prs"].values() if e["number"] is not None)
    log(f"merge_started for {batch_id} at {batch['merge_started'].get('at')} ({n} PRs expected)")
    return EXIT_OK


def recover_fb_mode_to_known_good(dev: Device, st: dict) -> int:
    """flash --recover with no pair, while an fb_mode obligation is pending
    or the phone sits in fastboot (R5-1). It never boots another slot:
    get to fastboot, read the active slot, and only if it is
    known_good_slot clear fb_mode, set it active, reboot, verify_boot slot
    and build, and settle with that proof (R8-1). An unreadable active
    slot halts before anything is cleared (R8-2). A confirmed other slot
    (a dev flash) is cleared and halts with the phone in fastboot and the
    obligation pending; it never switches slots without the wipe of flash
    --rollback."""
    kg = st["known_good_slot"]
    if dev.state() == "device":
        dev.guard()
    to_fastboot(dev)
    dev.fastboot_guard()
    got = dev.getvar("current-slot")
    active = got.lstrip("_")
    if active not in slots.SLOTS:
        raise fbmode.SlotUnreadable(dev.serial, got)
    if active != kg:
        way_back = (f"Return to known_good_slot {kg} with flash --rollback (it wipes userdata), or flash a dev "
                    "build again (flash stripped), which settles the obligation without a wipe once it has "
                    "verified the slot it boots.")
        try:
            owed = fbmode.clear(dev)
        except fbmode.ClearFailed as e:
            # R6-1: stay in fastboot, switch nothing, wipe nothing, halt.
            raise Halt(f"{e}. The phone stays in fastboot with slot {active} active; no slot was switched and "
                       f"nothing was wiped. Clear it by hand (fastboot -s {dev.serial} oem fb_mode_clear). "
                       + way_back, advice="The fb_mode obligation stays pending in the journal.") from e
        # R7-1, R8-2: nothing was booted or verified, so any obligation
        # stays pending, and the confirmed dev slot halts.
        raise Halt(f"fb_mode is cleared, but the active slot is {active} (a dev flash), not known_good_slot {kg}. "
                   "The phone stays in fastboot" + ("; the fb_mode obligation stays pending" if owed else "")
                   + ". " + way_back, advice="")
    owed = clear_then_set_active(dev, kg, then="flash --recover")
    dev.reboot_fastboot()
    try:
        dev.wait_for_device(BOOT_TIMEOUT)
        dev.wait_boot_completed(BOOT_TIMEOUT)
        proof = fbmode.verify_boot(dev, known_good_want(st))
        fbmode.settle(proof)
    except (TimeoutError, RuntimeError) as e:
        raise Halt(f"fb_mode recovery: slot {kg} could not be verified: {e}",
                   advice=owed_note("flash --recover")) from e
    how = proof.describe()
    st.setdefault("history", []).append({"event": "fb-mode-recovered", "at": now_iso(), "how": how,
                                         "obligation": owed})
    save_state(st)
    log(f"fb_mode recovery: cleared fb_mode in fastboot and booted known_good_slot {kg} ({how})")
    return EXIT_OK


def recover(runner=None, dev: Device | None = None) -> int:
    """flash --recover. Slot recovery decides first; fb_mode is handled on
    the way to the slot it picked, never by booting the active slot first
    (R5-1)."""
    st = load_state()
    if st and finish_checkpoint(st):
        st = load_state()
    if not st or not st.get("pair"):
        dev = dev or Device(SERIAL)
        # R6-1: every failure on the phone below halts (exit 3) with a clean
        # error line; nothing resolves the obligation on the way out.
        try:
            if fbmode.pending() is not None or dev.in_fastboot():
                if not st:
                    # No known-good slot yet (before the first baseline):
                    # only the generic recovery, which boots the active slot.
                    fbmode.recover(dev, BOOT_TIMEOUT, boot=True)
                    return EXIT_OK
                return recover_fb_mode_to_known_good(dev, st)
        except (RuntimeError, TimeoutError) as e:
            return recovery_halt(e)
        log("No unresolved pair.")
        return EXIT_OK
    pair = st["pair"]
    batch = read_json(batch_dir(pair["batch"]) / "batch.json", {}) or {}
    slugs = stage.repo_slugs() if pair["kind"] == "batch" else {}
    look = None
    if pair["kind"] == "batch":
        typed = recovery.lookup(batch, runner, slugs)
        # Any outcome but ok is today's failed lookup (H11): it halts.
        look = typed.data if typed.outcome == recovery.OK else None
    decision, why = slots.decide_recovery(pair, look, batch, slugs)
    log(f"Recovery for {pair['batch']} ({pair['kind']}): {decision}: {why}")
    st.setdefault("history", []).append({"event": "recovery-decision", "at": now_iso(),
                                         "batch": pair["batch"], "decision": decision, "why": why})
    save_state(st)
    if decision == slots.HALT:
        if fbmode.pending() is not None:
            log("An fb_mode obligation is also pending; it was left alone.")
        log("HALT: notify Zak.")
        return EXIT_HALT
    dev = dev or Device(SERIAL)
    try:
        if decision == slots.PROMOTE:
            # promote_batch brings the phone to the candidate slot (fb_mode
            # recovery, booting only that slot) before it reads it (R6-8).
            return promote_batch(pair["batch"], runner, dev)
        dev.guard() if dev.state() == "device" else None
        rollback(dev, st, f"recovery: {why}", then="flash --recover")
        return EXIT_OK
    except (RuntimeError, TimeoutError) as e:    # R6-1: every failure halts, obligation intact
        return recovery_halt(e)


def runner_owned_pair(lock: DeviceLock, zak: bool) -> str | None:
    """H12: why flash --rollback or --recover must refuse, or None. While
    slot-state.json holds a pair whose batch is the batch runner's
    running.id, only the runner may resolve it: RIVER_BATCH_ID names that
    batch and the device lock was reused through a matching token (the
    runner's holder holds it, so ``not lock.owned``). ``--zak`` overrides.
    An unreadable runner state file refuses (fail closed)."""
    if zak:
        return None
    st = load_state()
    pair = (st or {}).get("pair")
    if not pair:
        return None
    from . import runnerstate
    try:
        rid = runnerstate.running_id(bench_path())
    except runnerstate.Unreadable as e:
        return f"{e}; the slot pair of {pair['batch']} may belong to a running batch (pass --zak to override)"
    if rid != pair["batch"]:
        return None
    if os.environ.get("RIVER_BATCH_ID") == rid and not lock.owned:
        return None
    return (f"the slot pair of {rid} belongs to the running batch {rid}; the batch runner resolves it "
            "(batch tick), or Zak with --zak")


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

  --rollback and --recover refuse while the slot pair belongs to the batch the
  runner is running ($BENCH_HOME/runner/state.json running.id), unless the
  runner itself calls them (RIVER_BATCH_ID and its lock token), or --zak.

  baseline|stripped  Use $BENCH_HOME/out-<env>/target/product/river.
  PRODUCT_DIR        Any directory with boot.img, dtbo.img, system.img and vendor.img.
  --wipe       Factory reset (fastboot erase userdata; river's bootloader rejects -w).
  --no-reboot  Stay in the bootloader after flashing. Refused before the first
               baseline while an fb_mode obligation is pending: run
               phone-qa recover first (for an obligation with no usable build
               IDs it boots the slot and leaves the obligation pending; a dev
               flash that boots then settles it).
  --batch ID   Flash a staged batch ($BENCH_HOME/batches/ID). Checks every image hash
               before writing, fixes the slot pair in slot-state.json and batch.json
               before the first write, records the first boot's boot_id, signals and
               health evidence, and runs slot rescue if the boot fails.

The images go to the slot that is not the known-good one (or, before the first
baseline, the inactive slot), so the known-good slot stays as the fallback.
Before the first baseline, a flash that would write the slot a pending fb_mode
obligation was armed on is refused: that is the only slot known to boot.
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
    ap.add_argument("--zak", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    a = ap.parse_args(argv)
    if a.help:
        print(USAGE)
        return 0
    if a.status:
        return show_status()
    try:
        # --merge-started and --promote time nothing, so they may run while a
        # build holds the build lock (R6-5).
        with DeviceLock(check_build=not (a.merge_started or a.promote)) as lock:
            # Slot recovery dispatches first (R5-1): --rollback and --recover
            # handle fb_mode themselves, in fastboot, on the way to the slot
            # they pick, and never boot the active slot (it may be the failed
            # candidate). The other device modes run the generic preflight.
            # A flash refuses an unresolved pair before anything else. The
            # fb_mode recovery runs inside flash_dev and flash_batch, after
            # they have checked their inputs (R7-3); --promote does its own,
            # after its host-side proof.
            if a.batch or a.images or a.src:
                st = load_state()
                if st and st.get("pair"):
                    log(f"error: batch {st['pair']['batch']} holds an unresolved slot pair; resolve it first "
                        "(flash --recover)")
                    return EXIT_ERROR
            if a.rollback or a.recover:
                why = runner_owned_pair(lock, a.zak)
                if why:
                    log(f"error: {why}")
                    return EXIT_ERROR
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
                except (RuntimeError, TimeoutError) as e:   # mid-rollback: the pipeline halts (C7, R6-1)
                    return recovery_halt(e)
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
        if a.rollback or a.recover or fbmode.pending() is not None:
            return recovery_halt(e)       # R6-1: never exit 1 from recovery or with an obligation pending
        log(f"error: {e}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
