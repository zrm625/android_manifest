"""Motorola's ``fastboot oem fb_mode_set`` as a durable obligation (review
round 4).

While fb_mode is set, the bootloader stops in fastboot on every boot. Only
the sysrq extra-pass control sets it (reboots.arm_bootloader_stop). If the
host dies while it is set, every later boot stops in fastboot, so:

- ``arm`` writes a host entry (kind ``fb_mode``) to the state journal,
  durably, **before** it sends ``fb_mode_set``;
- ``disarm`` sends ``fb_mode_clear`` and only then resolves the entry;
- ``recover`` runs under the device lock before anything else touches the
  phone (calibration, flash, phone-qa): with an entry pending, or with the
  phone found in fastboot, it clears fb_mode, resolves the entry and, when
  asked or when it had to take the phone out of Android, boots Android;
- ``clear_in_fastboot`` is for code that is already in fastboot (flash,
  rescue, rollback): ``fb_mode_clear`` is harmless when nothing is set.

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


def _resolve(j: jmod.Journal) -> None:
    e = pending(j)
    if e is not None:
        j.resolve(e)


def disarm(dev, j: jmod.Journal | None = None) -> None:
    """``fastboot oem fb_mode_clear``, then resolve the obligation. The
    phone must be in fastboot. A failed clear leaves the entry pending."""
    j = j or journal()
    dev.fb_mode_clear()
    _resolve(j)


def clear_in_fastboot(dev, j: jmod.Journal | None = None) -> bool:
    """If the phone is in fastboot: clear fb_mode (harmless when it is not
    set) and resolve any obligation. True if it was in fastboot."""
    if not dev.in_fastboot():
        return False
    disarm(dev, j)
    return True


def recover(dev, boot_timeout: float = 600, *, boot: bool | None = None,
            j: jmod.Journal | None = None) -> str | None:
    """Clear a leftover fb_mode. Call it under the device lock, before the
    adb guard (the phone may be sitting in fastboot).

    - Obligation pending, phone in Android: ``adb reboot bootloader``
      (guarded), clear, resolve, boot Android.
    - Phone in fastboot: clear and resolve; then boot Android if ``boot``
      is True, or if ``boot`` is None and an obligation was pending.
    - Neither: nothing.

    Returns a description of what it did, or None. Raises (GuardError,
    TimeoutError, RuntimeError) if it could not finish; the obligation then
    stays pending for the next recovery."""
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
    disarm(dev, j)
    what = ("cleared a pending fb_mode obligation" if e is not None else
            "found the phone in fastboot; cleared fb_mode")
    if took or boot or (boot is None and e is not None):
        dev.reboot_fastboot()
        dev.wait_for_device(boot_timeout)
        dev.wait_boot_completed(boot_timeout)
        what += " and booted Android"
    log(f"fb_mode recovery: {what}")
    return what
