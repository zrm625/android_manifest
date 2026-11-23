"""Motorola's ``fastboot oem fb_mode_set`` as a durable obligation (review
round 4).

While fb_mode is set, the bootloader stops in fastboot on every boot. Only
the sysrq extra-pass control sets it (reboots.arm_bootloader_stop). If the
host dies while it is set, every later boot stops in fastboot, so:

- ``arm`` writes a host entry (kind ``fb_mode``) to the state journal,
  durably, **before** it sends ``fb_mode_set``;
- ``disarm`` sends ``fb_mode_clear`` and only then resolves the entry;
- ``recover`` runs under the device lock before anything else touches the
  phone (calibration, phone-qa, device-bench, flash builds): with an entry
  pending, or with the phone found in fastboot, it clears fb_mode and, when
  asked or when it had to take the phone out of Android, boots the active
  slot. The entry is resolved only once that boot completed (or, with
  ``boot=False``, once the clear succeeded), so a host that dies between
  the clear and the boot leaves the entry for the next recovery
  (review round 5, R5-1);
- slot recovery (``flash --rollback``, ``flash --recover``, rescue) never
  calls ``recover``: it must not boot the active slot, which may be the
  failed candidate. It calls ``clear_in_fastboot`` while in fastboot, sets
  the fallback slot active, reboots, verifies the slot, and only then calls
  ``settle`` (R5-1).

Every fastboot command goes through Device, which guards first.
"""

from __future__ import annotations

from . import journal as jmod
from .core import bench_path, log, now_iso

KIND = KEY = "fb_mode"


def journal() -> jmod.Journal:
    return jmod.Journal(bench_path("qa", "journal.json"), bench_path("qa"))


def pending(j: jmod.Journal | None = None) -> dict | None:
    """The unresolved fb_mode obligation, if any."""
    return (j or journal()).find(jmod.HOST, KIND, KEY)


def arm(dev, j: jmod.Journal | None = None) -> dict:
    """Journal the obligation (fsynced), then ``fastboot oem fb_mode_set``.
    The phone must be in fastboot."""
    j = j or journal()
    e = j.record(jmod.HOST, KIND, KEY, "clear", extra={"serial": dev.serial, "armed_at": now_iso()})
    dev.fb_mode_set()
    return e


class ClearFailed(RuntimeError):
    """``fb_mode_clear`` failed while an fb_mode obligation is pending."""


def settle(j: jmod.Journal | None = None) -> None:
    """Resolve the obligation, if any. Call it only once fb_mode was cleared
    and the phone is where the caller meant it to be."""
    j = j or journal()
    e = pending(j)
    if e is not None:
        j.resolve(e)


def disarm(dev, j: jmod.Journal | None = None) -> None:
    """``fastboot oem fb_mode_clear``, then resolve the obligation. The
    phone must be in fastboot. A failed clear leaves the entry pending."""
    j = j or journal()
    dev.fb_mode_clear()
    settle(j)


def clear_in_fastboot(dev, j: jmod.Journal | None = None) -> bool:
    """For code already in fastboot (flash, rescue, rollback): send
    ``fb_mode_clear`` (harmless when nothing is set) and **don't** resolve.
    Returns True if an obligation is pending; the caller calls ``settle``
    once the phone is on the slot it meant (R5-1).

    A failed clear with nothing pending is logged and ignored, so it never
    aborts a rescue or rollback (review round 5). With an obligation
    pending it raises ClearFailed: the caller still sets the fallback slot
    active, then halts. A GuardError always propagates."""
    from .device import GuardError
    j = j or journal()
    owed = pending(j) is not None
    try:
        dev.fb_mode_clear()
    except GuardError:
        raise
    except (RuntimeError, TimeoutError) as e:
        if owed:
            raise ClearFailed(f"fastboot oem fb_mode_clear failed with an fb_mode obligation pending: {e}") from e
        log(f"warning: fastboot oem fb_mode_clear failed ({e}); no fb_mode obligation is pending, so going on")
    return owed


def recover(dev, boot_timeout: float = 600, *, boot: bool | None = None,
            j: jmod.Journal | None = None) -> str | None:
    """Clear a leftover fb_mode. Call it under the device lock, before the
    adb guard (the phone may be sitting in fastboot). It boots the
    **active** slot, so slot recovery must not use it (R5-1).

    - Obligation pending, phone in Android: ``adb reboot bootloader``
      (guarded), clear, then boot Android unless ``boot`` is False.
    - Phone in fastboot: clear; then boot Android if ``boot`` is True, or if
      ``boot`` is None and an obligation was pending.
    - Neither: nothing.

    The entry is resolved after the boot completed, or right after the
    clear when the phone stays in fastboot. Returns a description of what it
    did, or None. Raises (GuardError, TimeoutError, RuntimeError) if it
    could not finish; the obligation then stays pending for the next
    recovery."""
    j = j or journal()
    e = pending(j)
    in_fb = dev.in_fastboot()
    if e is None and not in_fb:
        return None
    took = False
    if not in_fb:
        st = dev.state()
        if st not in ("device", "recovery"):
            raise RuntimeError(f"an fb_mode obligation is pending but {dev.serial} answers in neither adb nor "
                               f"fastboot (state: {st or 'none'}); it clears with: fastboot -s {dev.serial} oem "
                               "fb_mode_clear")
        dev.reboot_adb("bootloader")
        dev.wait_fastboot(120)
        took = True
    dev.fb_mode_clear()
    what = ("cleared a pending fb_mode obligation" if e is not None else
            "found the phone in fastboot; cleared fb_mode")
    if boot is True or (boot is None and (took or e is not None)):
        dev.reboot_fastboot()
        dev.wait_for_device(boot_timeout)
        dev.wait_boot_completed(boot_timeout)
        what += " and booted Android"
    else:
        what += "; the phone stays in fastboot"
    settle(j)
    log(f"fb_mode recovery: {what}")
    return what
