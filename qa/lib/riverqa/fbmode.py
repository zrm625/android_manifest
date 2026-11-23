"""Motorola's ``fastboot oem fb_mode_set`` as a durable obligation (review
round 4), settled only by a verified boot (round 8).

While fb_mode is set, the bootloader stops in fastboot on every boot. Only
the sysrq extra-pass control sets it (reboots.arm_bootloader_stop). If the
host dies while it is set, every later boot stops in fastboot, so:

- ``arm`` writes a host entry (kind ``fb_mode``) to the state journal,
  durably, **before** it sends ``fb_mode_set``. The entry records the
  identity of the phone it was armed on: the slot and both build IDs
  (R8-1). Generic recovery verifies the phone against that identity.
- ``clear`` (and ``recover``) send ``fb_mode_clear`` in fastboot and stamp
  the entry with the time of the clear: the host's CLOCK_BOOTTIME and
  boot_id (R9-7). Nothing else is resolved there.
- **The single rule (R8-1).** ``settle(proof)`` is the only function that
  resolves the obligation. ``proof`` is a ``VerifiedBoot``, which only
  ``verify_boot`` makes: on the booted phone, the adb guard, the boot_id,
  the running slot, both build IDs, and the boot-partition hash when the
  caller asks for it. ``settle`` refuses unless the entry was stamped
  cleared, the verified boot began after that clear, and the phone's
  boot_id is still the proof's. ``journal.Journal.resolve`` refuses an
  fb_mode entry, and a unit test checks that nothing but ``settle`` calls
  the journal's private resolver.
- Callers pass everything they need to ``verify_boot`` (through
  ``recover``'s ``want``), settle, and then use the values on the proof:
  no verification read happens after the settle.

Every fastboot command goes through Device, which guards first.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import journal as jmod
from .core import bench_path, host_boot_id, host_boottime, log, now_iso

KIND = KEY = jmod.FB_MODE
SLOTS = ("a", "b")


def journal() -> jmod.Journal:
    return jmod.Journal(bench_path("qa", "journal.json"), bench_path("qa"))


def pending(j: jmod.Journal | None = None) -> dict | None:
    """The unresolved fb_mode obligation, if any."""
    return (j or journal()).find(jmod.HOST, KIND, KEY)


class ClearFailed(RuntimeError):
    """``fb_mode_clear`` failed while an fb_mode obligation is pending."""


class WrongSlot(RuntimeError):
    """fastboot reports a slot active, read and confirmed, other than the
    one the caller may boot (R6-8). Raised before anything is cleared or
    booted. The caller says what to do: the right advice differs for a
    promotion, a batch preflight and generic recovery (R7-2)."""

    def __init__(self, serial: str, active: str, want: str):
        self.active, self.want = active, want
        super().__init__(f"{serial} is in fastboot with slot {active} active, not slot {want}; not booting it")


class SlotUnreadable(RuntimeError):
    """``getvar current-slot`` gave no slot (R8-2). Never a WrongSlot, never
    rollback advice: raised before anything is cleared or booted."""

    def __init__(self, serial: str, got: str):
        super().__init__(f"cannot read the active slot; recheck with `fastboot -s {serial} getvar current-slot` "
                         f"(fastboot reported {got!r})")


class NotVerified(RuntimeError):
    """verify_boot found a mismatch or could not read something."""


class SlotMismatch(NotVerified):
    """verify_boot read the running slot, and it is another one (R8-2: a
    confirmed slot, unlike an unreadable one)."""


# --- reads on the booted phone ------------------------------------------------

def partition_build_ids(dev) -> tuple[str | None, str | None]:
    """ro.river.build_id and ro.vendor.river.build_id read straight from the
    partitions' build.prop files, as root."""
    dev.root()
    sysp = dev.sh("grep -m1 '^ro.river.build_id=' /system/build.prop").strip()
    venp = dev.sh("grep -m1 '^ro.vendor.river.build_id=' /vendor/build.prop").strip()
    return (sysp.split("=", 1)[1] if "=" in sysp else None, venp.split("=", 1)[1] if "=" in venp else None)


class Unstamped(RuntimeError):
    """The running build has no River build-ID stamps (R9-3)."""

    def __init__(self):
        super().__init__("running build is unstamped; dev-flash a build from this tree first (`flash stripped`)")


def running_identity(dev) -> tuple[str, tuple[str, str]]:
    """In Android: the adb guard, then the running slot and both build IDs.
    Raises RuntimeError on an unreadable slot (also a getprop that gives
    nothing, R9-9) and Unstamped on a build without both stamps. It
    changes nothing on the phone."""
    dev.guard()
    slot = (dev.getprop("ro.boot.slot_suffix") or "").lstrip("_")
    if slot not in SLOTS:
        raise RuntimeError(f"cannot read the running slot ({slot!r})")
    ids = partition_build_ids(dev)
    if not all(ids):
        raise Unstamped()
    return slot, (ids[0], ids[1])


def boot_partition_sha(dev, slot: str, nbytes: int | None = None) -> str:
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


# --- the proof ----------------------------------------------------------------

@dataclass(frozen=True)
class Want:
    """What the booted phone must be. ``build_ids`` is (system, vendor);
    ``boot_sha`` is compared with the boot partition (its first
    ``boot_bytes`` bytes, if given); ``hash_boot`` hashes it even with
    nothing to compare, so the caller can use the hash from the proof. At
    least one of ``build_ids`` and ``boot_sha`` is required."""
    slot: str
    build_ids: tuple[str, str] | None
    boot_sha: str | None = None
    boot_bytes: int | None = None
    hash_boot: bool = False

    def __post_init__(self):
        if self.slot not in SLOTS:
            raise ValueError(f"bad slot {self.slot!r}")
        if self.build_ids is not None and (len(self.build_ids) != 2 or not all(self.build_ids)):
            raise ValueError(f"bad build IDs {self.build_ids!r}")
        if self.build_ids is None and not self.boot_sha:
            raise ValueError("no build IDs and no boot hash: nothing to verify the build with")

    def describe(self) -> str:
        ids = "" if self.build_ids is None else f", build IDs {'/'.join(self.build_ids)}"
        return f"slot {self.slot}{ids}" + (f", boot {self.boot_sha[:12]}" if self.boot_sha else "")


_SEAL = object()


@dataclass(frozen=True)
class VerifiedBoot:
    """Proof that the phone booted Android and passed verify_boot. Only
    verify_boot seals one; settle accepts nothing else. The seal is not an
    ``__init__`` argument (R9-8), so neither a direct construction nor
    ``dataclasses.replace`` of a sealed proof gives a sealed object."""
    serial: str
    boot_id: str
    booted_at: float          # host CLOCK_BOOTTIME at which this boot's kernel started, at the latest (R9-7)
    host_boot_id: str         # the host boot that booted_at is measured in
    slot: str
    build_ids: tuple[str | None, str | None]
    boot_sha: str | None
    dev: object = field(repr=False, compare=False)
    _seal: object = field(init=False, repr=False, compare=False, default=None)

    @property
    def sealed(self) -> bool:
        return self._seal is _SEAL

    def describe(self) -> str:
        return (f"slot {self.slot}, build IDs {self.build_ids[0]}/{self.build_ids[1]}"
                + (f", boot {self.boot_sha[:12]}" if self.boot_sha else "") + f", boot_id {self.boot_id[:8]}")


def verify_boot(dev, want: Want) -> VerifiedBoot:
    """Verify the booted phone, in this order: the adb guard (serial and
    product); the boot status (boot completed, boot_id, uptime), read
    first so that settle's boot_id check covers every read below; the
    running slot; both build IDs; the boot-partition hash when ``want``
    asks for it. Any failed read or mismatch raises (NotVerified,
    GuardError, TimeoutError); then nothing can be settled."""
    dev.guard()
    st = dev.boot_status()
    if st is None or not st.completed or not st.boot_id:
        raise NotVerified(f"{dev.serial}: cannot read a completed boot (boot_id, sys.boot_completed, uptime)")
    slot = (dev.getprop("ro.boot.slot_suffix") or "").lstrip("_")
    if slot not in SLOTS:
        raise NotVerified(f"cannot read the running slot ({slot!r})")
    if slot != want.slot:
        raise SlotMismatch(f"the phone runs slot {slot}, not slot {want.slot}")
    ids = partition_build_ids(dev)
    if want.build_ids is not None and tuple(ids) != tuple(want.build_ids):
        raise NotVerified(f"build IDs system={ids[0]} vendor={ids[1]}, expected {'/'.join(want.build_ids)}")
    sha = None
    if want.boot_sha or want.hash_boot:
        sha = boot_partition_sha(dev, slot, want.boot_bytes)
        if want.boot_sha and sha != want.boot_sha:
            raise NotVerified(f"boot_{slot} hash {sha[:12]} differs from the expected {want.boot_sha[:12]}")
    if st.host_boottime is None:
        raise NotVerified(f"{dev.serial}: the boot status read has no host boot clock")
    proof = VerifiedBoot(dev.serial, st.boot_id, st.host_boottime - st.uptime, host_boot_id(), slot, tuple(ids),
                         sha, dev)
    object.__setattr__(proof, "_seal", _SEAL)      # R9-8: the only place a proof is sealed
    return proof


# --- the obligation -----------------------------------------------------------

def armed_want(e: dict) -> Want:
    """The identity the phone had when this obligation was armed (R8-1)."""
    x = e.get("extra") or {}
    ids = x.get("build_ids")
    if x.get("slot") not in SLOTS or not ids or len(ids) != 2 or not all(ids):
        raise RuntimeError("the fb_mode obligation has no recorded slot and build IDs (armed before round 8), "
                           "so no boot can be verified against it. A dev flash (flash stripped) settles it with "
                           "the verified boot of the slot it writes; with a known-good slot, flash --rollback or a "
                           "batch flash does too")
    return Want(x["slot"], (ids[0], ids[1]))


def arm(dev, slot: str, build_ids: tuple[str, str], j: jmod.Journal | None = None) -> dict:
    """Journal the obligation with the identity of the phone it is armed
    on (fsynced), then ``fastboot oem fb_mode_set``. The phone must be in
    fastboot. Refuses with an obligation already pending: that one must be
    settled first."""
    j = j or journal()
    Want(slot, tuple(build_ids))                    # validates
    if pending(j) is not None:
        raise RuntimeError("an fb_mode obligation is already pending; settle it first (phone-qa recover)")
    e = j.record(jmod.HOST, KIND, KEY, "clear",
                 extra={"serial": dev.serial, "armed_at": now_iso(), "slot": slot,
                        "build_ids": list(build_ids), "cleared_at": None})
    dev.fb_mode_set()
    return e


def _mark_cleared(j: jmod.Journal) -> bool:
    """After a successful fb_mode_clear: stamp the pending entry with the
    host's CLOCK_BOOTTIME and boot_id (R9-7; the wall time is only for
    people). Returns True if an obligation is pending."""
    e = pending(j)
    if e is None:
        return False
    j.annotate(e, cleared_at={"boottime": host_boottime(), "host_boot_id": host_boot_id(), "wall": time.time()})
    return True


def clear(dev, j: jmod.Journal | None = None) -> bool:
    """For code already in fastboot (flash, rescue, rollback, the sysrq
    control): send ``fb_mode_clear`` (harmless when nothing is set) and
    stamp the entry cleared; **never** resolve. Returns True if an
    obligation is pending; the caller settles it with a VerifiedBoot of
    the boot that follows.

    A failed clear with nothing pending is logged and ignored, so it never
    aborts a rescue or rollback (review round 5). With an obligation
    pending it raises ClearFailed. A GuardError always propagates."""
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
        return False
    return _mark_cleared(j)


class NotSettled(RuntimeError):
    """settle refused: the proof doesn't show a boot after the clear."""


def settle(proof: VerifiedBoot, j: jmod.Journal | None = None) -> bool:
    """The one way an fb_mode obligation is resolved (R8-1). Returns True if
    one was. Refuses (TypeError, NotSettled) unless ``proof`` is a
    VerifiedBoot of this phone, the entry was stamped cleared, the
    verified boot began after that clear (both on the host's
    CLOCK_BOOTTIME, in the host boot that is running now: R9-7), and the
    phone's boot_id, read again now, is still the proof's (no reboot
    since, so every read of verify_boot was of this boot)."""
    if not isinstance(proof, VerifiedBoot) or not proof.sealed:
        raise TypeError("fbmode.settle needs a VerifiedBoot from fbmode.verify_boot")
    j = j or journal()
    e = pending(j)
    if e is None:
        return False
    x = e.get("extra") or {}
    if x.get("serial") not in (None, proof.serial):
        raise NotSettled(f"the obligation is for {x.get('serial')}, the proof for {proof.serial}")
    cleared = x.get("cleared_at")
    if cleared is None:
        raise NotSettled("fb_mode_clear was never confirmed for this obligation; not resolving it")
    if not isinstance(cleared, dict) or not isinstance(cleared.get("boottime"), (int, float)) \
            or not cleared.get("host_boot_id"):
        raise NotSettled("the clear stamp has no host boot clock (written before round 9); not resolving the "
                         "obligation. Recovery clears and stamps it again.")
    # R9-7: both times are the host's CLOCK_BOOTTIME, which a wall-clock
    # step doesn't move; they compare only within one host boot.
    host = host_boot_id()
    if cleared["host_boot_id"] != host or proof.host_boot_id != host:
        raise NotSettled("the host rebooted after fb_mode was cleared, so the clear and the boot can't be "
                         "compared; not resolving the obligation. Recovery clears and stamps it again.")
    if proof.booted_at < cleared["boottime"]:
        raise NotSettled("the verified boot began before fb_mode was cleared; not resolving the obligation")
    now = proof.dev.boot_id()
    if now != proof.boot_id:
        raise NotSettled(f"boot_id is {now[:8] or '?'}, not the verified boot's {proof.boot_id[:8]}: the phone "
                         "rebooted after the check; not resolving the obligation")
    j._settle_fb_mode(e)
    log(f"fb_mode obligation settled: verified {proof.describe()}")
    return True


@dataclass(frozen=True)
class Recovery:
    """What recover did (``what``, None if nothing) and the proof of the
    boot it verified, if any."""
    what: str | None
    proof: VerifiedBoot | None


def recover(dev, boot_timeout: float = 600, *, boot: bool | None = None, want: Want | None = None,
            j: jmod.Journal | None = None) -> Recovery:
    """Clear a leftover fb_mode. Call it under the device lock, before the
    adb guard (the phone may be sitting in fastboot). It boots the
    **active** slot, so slot recovery must not use it (R5-1).

    - Obligation pending, phone in Android: ``adb reboot bootloader``
      (guarded), clear, then boot Android unless ``boot`` is False.
    - Phone in fastboot: clear; then boot Android if ``boot`` is True, or
      if ``boot`` is None and an obligation was pending.
    - Neither: nothing, except that with ``want`` the phone is verified
      as it is.

    The phone must match ``want``, or with no ``want`` the identity the
    obligation was armed with (R8-1). Before a boot it reads the active
    slot: an unreadable slot raises SlotUnreadable and another slot
    WrongSlot, both before anything is cleared (R6-8, R8-2). After the
    boot, verify_boot checks the phone, and only then does settle resolve
    the obligation. With ``boot`` False (a dev flash) it clears and never
    settles: the flash settles with a proof of the slot it writes. Only a
    path that boots reads the armed identity (R9-2). Raises if it could
    not finish; the obligation then stays pending."""
    j = j or journal()
    e = pending(j)
    in_fb = dev.in_fastboot()
    if e is None and not in_fb:
        return Recovery(None, verify_boot(dev, want) if want is not None else None)
    # Not in fastboot means an obligation is pending (above), so a phone
    # taken from Android always boots again unless ``boot`` is False.
    will_boot = boot is True or (boot is None and e is not None)
    # R9-2: the armed identity is needed only to verify a boot. A path that
    # never boots (a dev flash) only clears and stamps, so an entry armed
    # before round 8, with no identity, never stops it.
    expect = want
    if expect is None and will_boot and e is not None:
        expect = armed_want(e)
    if not in_fb:
        st = dev.state()
        if st not in ("device", "recovery"):
            raise RuntimeError(f"an fb_mode obligation is pending but {dev.serial} answers in neither adb nor "
                               f"fastboot (state: {st or 'none'}); it clears with: fastboot -s {dev.serial} oem "
                               "fb_mode_clear")
        dev.reboot_adb("bootloader")
        dev.wait_fastboot(120)
    active = None
    if will_boot:
        got = dev.getvar("current-slot")
        active = got.lstrip("_")
        if active not in SLOTS:
            raise SlotUnreadable(dev.serial, got)
        if expect is not None and active != expect.slot:
            raise WrongSlot(dev.serial, active, expect.slot)
    dev.fb_mode_clear()
    _mark_cleared(j)
    what = ("cleared a pending fb_mode obligation" if e is not None else
            "found the phone in fastboot; cleared fb_mode")
    if not will_boot:
        what += "; the phone stays in fastboot" + ("; the obligation stays pending until a verified boot"
                                                   if e is not None else "")
        log(f"fb_mode recovery: {what}")
        return Recovery(what, None)
    dev.reboot_fastboot()
    dev.wait_for_device(boot_timeout)
    dev.wait_boot_completed(boot_timeout)
    proof = None
    if expect is not None:
        proof = verify_boot(dev, expect)
        settle(proof, j)
        what += f" and booted Android, verified: {proof.describe()}"
    else:
        # Nothing was pending and the caller wants no proof: only check the
        # phone booted the slot fastboot reported.
        dev.guard()
        running = (dev.getprop("ro.boot.slot_suffix") or "").lstrip("_")
        if running != active:
            raise RuntimeError(f"fastboot booted slot {active}, but the phone runs slot {running or '?'}")
        what += f" and booted Android on slot {active}"
    log(f"fb_mode recovery: {what}")
    return Recovery(what, proof)
