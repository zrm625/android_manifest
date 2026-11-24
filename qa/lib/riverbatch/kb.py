"""The known-good build check (KB) and checked phone reads (section 1, I1, I2).

KB never elevates: no ``dev.root()``, no ``adb root``, no
``fbmode.running_identity`` (which roots the phone and restarts adbd), and
no ``dev.guard()`` (I2). With non-root adbd, SELinux keeps
``ro.vendor.river.build_id`` (vendor_default_prop) and ``/vendor/build.prop``
from the ``shell`` domain, and from ``vendor_shell`` too (checked live on the
test phone, 2026-11-23). So KB matches the vendor partition by
``ro.vendor.build.date.utc``, which the shell can read, against the staged
vendor date of the known-good batch (``batches/<batch>/batch.json``
``dates.vendor``), and the system partition by ``ro.river.build_id`` (I1).
Root-level verification still happens in ``fbmode.verify_boot`` before any
flash.

The read is one ``dev.sh_rc`` call for ``ro.product.device``,
``ro.serialno``, ``ro.boot.slot_suffix``, ``sys.boot_completed``,
``ro.river.build_id`` and ``ro.vendor.build.date.utc``. HALT only when that
call returned 0 with a non-empty product or serial that is not this phone's.
A failed call or empty values re-check the USB state: fastboot, unauthorized
or recovery block; no answer pauses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from riverqa import fbmode
from riverqa.core import CODENAME, SERIAL, read_json

from . import paths

OK, BLOCKED, PAUSE, HALT = "ok", "blocked", "pause", "halt"
NOT_ANSWERING = "phone not answering on USB: hold the power button"

PROPS = {"D": "ro.product.device", "N": "ro.serialno", "S": "ro.boot.slot_suffix", "C": "sys.boot_completed",
         "R": "ro.river.build_id", "V": "ro.vendor.build.date.utc"}
READ_CMD = "; ".join(f'echo "{k}=$(getprop {v})"' for k, v in PROPS.items())


@dataclass
class Check:
    kind: str
    message: str = ""
    props: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.kind == OK


def usb_state(dev) -> Check:
    """Steps 1 and 2: what the phone is on USB, when it is not usable."""
    st = dev.state()
    if st == "":
        if dev.in_fastboot():
            return Check(BLOCKED, "phone in fastboot")
        return Check(PAUSE, NOT_ANSWERING)
    if st in ("unauthorized", "recovery"):
        return Check(BLOCKED, f"phone is {st} on USB")
    if st != "device":
        return Check(BLOCKED, f"phone is {st} on USB")
    return Check(OK)


def read_props(dev) -> Check:
    """One checked, non-elevating read of the identity and boot props (I2)."""
    u = usb_state(dev)
    if not u.ok:
        return u
    try:
        rc, out, err = dev.sh_rc(READ_CMD, timeout=20)
    except (TimeoutError, OSError) as e:
        rc, out, err = 124, "", str(e)
    vals = {}
    for line in out.splitlines():
        k, sep, v = line.partition("=")
        if sep and k in PROPS:
            vals[k] = v.strip()
    if rc == 0:
        if vals.get("D") and vals["D"] != CODENAME:
            return Check(HALT, f"{SERIAL} reports product {vals['D']!r}, not {CODENAME}")
        if vals.get("N") and vals["N"] != SERIAL:
            return Check(HALT, f"adb -s {SERIAL} reached a phone with serial {vals['N']!r}")
    if rc != 0 or not all(vals.get(k) for k in ("D", "N", "S", "C")):
        again = usb_state(dev)
        if not again.ok:
            return again
        return Check(PAUSE, f"{NOT_ANSWERING} (read failed, rc {rc}: {(err or out).strip()[-120:]})")
    return Check(OK, props={PROPS[k]: v for k, v in vals.items()})


def known_good_vendor_date(batch_id: str | None) -> str | None:
    if not batch_id:
        return None
    b = read_json(paths.batch_dir(batch_id) / "batch.json", {}) or {}
    return ((b.get("dates") or {}).get("vendor")) or None


def check(dev, slot_state: dict | None) -> Check:
    """KB (section 1, I1, I2)."""
    r = read_props(dev)
    if not r.ok:
        return r
    p = r.props
    if p.get("sys.boot_completed") != "1":
        return Check(BLOCKED, "phone still booting", p)
    slot = p.get("ro.boot.slot_suffix", "").lstrip("_")
    sys_id = fbmode.stamp(p.get("ro.river.build_id"))
    vdate = p.get("ro.vendor.build.date.utc") or None
    if sys_id is None or vdate is None:
        return Check(BLOCKED, "phone runs an unstamped dev build: return it with `flash --rollback`", p)
    st = slot_state or {}
    kg = st.get("known_good") or {}
    want_date = known_good_vendor_date(kg.get("batch"))
    if want_date is None:
        return Check(BLOCKED, f"known-good batch {kg.get('batch')}: no staged vendor date to match "
                              "(batches/<id>/batch.json dates.vendor)", p)
    if slot != st.get("known_good_slot") or sys_id != kg.get("build_id") or vdate != want_date:
        return Check(BLOCKED, f"phone runs a dev build on slot {slot or '?'}: return it with `flash --rollback`", p)
    return Check(OK, "", p)


def checked_read(dev, cmd: str, timeout: float = 20) -> tuple[Check, str]:
    """A non-elevating read checked as in KB: (Check, stdout)."""
    u = usb_state(dev)
    if not u.ok:
        return u, ""
    try:
        rc, out, err = dev.sh_rc(cmd, timeout=timeout)
    except (TimeoutError, OSError) as e:
        rc, out, err = 124, "", str(e)
    if rc != 0:
        again = usb_state(dev)
        return (again if not again.ok else Check(PAUSE, f"{NOT_ANSWERING} (rc {rc}: {(err or out).strip()[-120:]})")), ""
    return Check(OK), out
