"""The test phone over adb and fastboot. Every call names the serial and has a
deadline; nothing here ever talks to another device."""

from __future__ import annotations

import os
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .core import CODENAME, SERIAL, Deadline, log, run

USB_ROOT = Path("/sys/bus/usb/devices")


class GuardError(RuntimeError):
    """The phone is missing, or is not the river test phone."""


class Unreachable(RuntimeError):
    """Neither adb nor fastboot answers."""


@dataclass
class BootStatus:
    boot_id: str
    completed: bool
    uptime: float
    host_mono: float   # time.monotonic() halfway through the read
    host_wall: float


# Test seam: unit tests add fake serials here. Nothing else may; the
# environment cannot widen the set.
TEST_SERIALS: set[str] = set()


def allowed_serial(serial: str) -> str:
    """The serial, if the harness may touch it: only the test phone."""
    env = os.environ.get("BENCH_DEVICE")
    if env and env != SERIAL:
        raise GuardError(f"BENCH_DEVICE={env} is not the test phone {SERIAL}. Refusing.")
    if serial != SERIAL and serial not in TEST_SERIALS:
        raise GuardError(f"serial {serial!r} is not the test phone {SERIAL}. Refusing.")
    return serial


@dataclass
class Read:
    """The outcome of one diagnostic read: callers must check ``ok`` before
    trusting ``out`` (a failed read is UNVERIFIED, never an empty PASS)."""
    rc: int
    out: str
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _is_reboot(args: tuple) -> bool:
    return bool(args) and str(args[0]).startswith("reboot")


class Device:
    def __init__(self, serial: str = SERIAL):
        if not serial:
            raise GuardError("no serial")
        self.serial = allowed_serial(serial)

    # --- adb -------------------------------------------------------------
    def adb(self, *args: str, timeout: float = 30, input=None, text=True):
        if _is_reboot(args):
            raise GuardError("reboot only through Device.reboot_adb(), which guards first")
        return self._adb(*args, timeout=timeout, input=input, text=text)

    def _adb(self, *args: str, timeout: float = 30, input=None, text=True):
        return run(["adb", "-s", self.serial, *args], timeout=timeout, input=input, text=text)

    def sh(self, cmd: str, timeout: float = 30) -> str:
        """Run a shell command; return stdout with CRs removed. Errors are
        ignored: use read() for anything a verdict depends on."""
        return self.adb("shell", cmd, timeout=timeout).stdout.replace("\r", "")

    def sh_rc(self, cmd: str, timeout: float = 30) -> tuple[int, str, str]:
        r = self.adb("shell", cmd, timeout=timeout)
        return r.returncode, r.stdout.replace("\r", ""), r.stderr.replace("\r", "")

    def read(self, cmd: str, timeout: float = 30) -> Read:
        """A diagnostic read that carries its return code and stderr."""
        rc, out, err = self.sh_rc(cmd, timeout=timeout)
        return Read(rc, out, err)

    def sh_bytes(self, cmd: str, timeout: float = 60) -> bytes:
        return self.sh_bytes_rc(cmd, timeout=timeout)[1]

    def sh_bytes_rc(self, cmd: str, timeout: float = 60) -> tuple[int, bytes, str]:
        r = run(["adb", "-s", self.serial, "exec-out", cmd], timeout=timeout, text=False)
        return r.returncode, r.stdout, (r.stderr or b"").decode(errors="replace")

    def reboot_adb(self, target: str | None = None, timeout: float = 30):
        """``adb reboot [target]``, only after the river guard passes (adb
        device or recovery mode, ro.product.device and ro.serialno)."""
        self.reboot_guard()
        return self._adb(*(["reboot", target] if target else ["reboot"]), timeout=timeout)

    def reboot_guard(self) -> None:
        st = self.state()
        if st not in ("device", "recovery"):
            raise GuardError(f"{self.serial} is not in adb device or recovery mode (state: {st or 'none'})")
        self._check_identity()

    def state(self) -> str:
        try:
            r = self.adb("get-state", timeout=10)
        except TimeoutError:
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""

    def getprop(self, name: str) -> str:
        return self.sh(f"getprop {shlex.quote(name)}").strip()

    def guard(self) -> None:
        """Refuse unless the serial answers in adb device mode and is river."""
        st = self.state()
        if st != "device":
            raise GuardError(f"{self.serial} is not in adb device mode (state: {st or 'none'})")
        self._check_identity()

    def _check_identity(self) -> None:
        codename = self.getprop("ro.product.device")
        if codename != CODENAME:
            raise GuardError(f"{self.serial} is {codename!r}, not {CODENAME}. Refusing.")
        serialno = self.getprop("ro.serialno")
        if serialno and serialno != self.serial:
            raise GuardError(f"adb -s {self.serial} reached a phone with serial {serialno}. Refusing.")

    def wait_for_device(self, seconds: float = 120) -> None:
        d = Deadline(seconds, f"{self.serial} to appear in adb")
        while self.state() != "device":
            d.check()
            time.sleep(0.5)

    def root(self, seconds: float = 60) -> None:
        if self.sh("id -u").strip() == "0":
            return
        self.adb("root", timeout=30)
        time.sleep(1)
        d = Deadline(seconds, "adbd to restart as root")
        while True:
            if self.state() == "device":
                try:
                    if self.sh("id -u", timeout=10).strip() == "0":
                        return
                except TimeoutError:
                    pass
            d.check()
            time.sleep(0.5)

    def boot_status(self) -> BootStatus | None:
        """boot_id, sys.boot_completed and uptime in one shell command."""
        t0 = time.monotonic()
        w0 = time.time()
        try:
            r = self.adb("shell", "cat /proc/sys/kernel/random/boot_id; getprop sys.boot_completed; cat /proc/uptime",
                         timeout=10)
        except TimeoutError:
            return None
        t1 = time.monotonic()
        if r.returncode != 0:
            return None
        lines = r.stdout.replace("\r", "").split("\n")
        if len(lines) < 3 or not lines[0].strip():
            return None
        try:
            uptime = float(lines[2].split()[0])
        except (IndexError, ValueError):
            return None
        return BootStatus(lines[0].strip(), lines[1].strip() == "1", uptime,
                          (t0 + t1) / 2, w0 + (t1 - t0) / 2)

    def boot_id(self) -> str:
        return self.sh("cat /proc/sys/kernel/random/boot_id").strip()

    def wait_boot_completed(self, seconds: float) -> BootStatus:
        d = Deadline(seconds, "boot to complete")
        while True:
            if self.state() == "device":
                st = self.boot_status()
                if st and st.completed:
                    return st
            d.check()
            time.sleep(0.5)

    def kmsg(self, text: str, copies: int = 4) -> None:
        """Write a marker to the kernel log (needs root)."""
        safe = "".join(c for c in text if c.isalnum() or c in ":-_")
        cmd = "; ".join([f"echo '<6>{safe}' > /dev/kmsg"] * copies)
        self.sh(cmd, timeout=15)

    def read_file_root(self, path: str, timeout: float = 30) -> bytes:
        return self.sh_bytes(f"cat {shlex.quote(path)}", timeout=timeout)

    # --- fastboot --------------------------------------------------------
    def fastboot(self, *args: str, timeout: float = 120):
        if _is_reboot(args):
            raise GuardError("reboot only through Device.reboot_fastboot(), which guards first")
        return self._fastboot(*args, timeout=timeout)

    def _fastboot(self, *args: str, timeout: float = 120):
        return run(["fastboot", "-s", self.serial, *args], timeout=timeout)

    def reboot_fastboot(self, target: str | None = None, timeout: float = 60) -> None:
        """``fastboot reboot [target]``, only after the bootloader reports
        product river (fastboot_guard)."""
        self.fastboot_guard()
        args = ["reboot", target] if target else ["reboot"]
        r = self._fastboot(*args, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"fastboot {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[-400:]}")

    def in_fastboot(self) -> bool:
        try:
            r = run(["fastboot", "devices"], timeout=10)
        except TimeoutError:
            return False
        return any(line.split()[:1] == [self.serial] for line in r.stdout.splitlines())

    def getvar(self, name: str) -> str:
        r = self.fastboot("getvar", name, timeout=20)
        for line in (r.stdout + r.stderr).splitlines():
            k, sep, v = line.partition(":")
            if sep and k.strip() == name:
                return v.strip()
        return ""

    def wait_fastboot(self, seconds: float) -> None:
        d = Deadline(seconds, f"{self.serial} to appear in fastboot")
        while not self.in_fastboot():
            d.check()
            time.sleep(0.5)

    def fastboot_guard(self) -> None:
        product = self.getvar("product")
        if product != CODENAME:
            raise GuardError(f"bootloader reports product {product!r}, not {CODENAME}. Refusing.")
        unlocked, secure = self.getvar("unlocked"), self.getvar("securestate")
        if not (unlocked == "yes" or secure in ("flashing_unlocked", "unlocked")):
            raise GuardError(f"bootloader is locked (unlocked: {unlocked!r}, securestate: {secure!r})")

    def fastboot_ok(self, *args: str, timeout: float = 300) -> None:
        r = self.fastboot(*args, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"fastboot {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[-400:]}")


# --- USB enumeration watcher ------------------------------------------------

ADB_IDS = {"18d1:4ee7", "18d1:4ee2", "18d1:4ee1", "22b8:2e76", "22b8:2e81"}


def usb_presence(serial: str = SERIAL) -> str | None:
    """'vid:pid' of the USB device with this serial, or None."""
    try:
        entries = list(USB_ROOT.iterdir())
    except OSError:
        return None
    for d in entries:
        try:
            if (d / "serial").read_text().strip() != serial:
                continue
            return f"{(d / 'idVendor').read_text().strip()}:{(d / 'idProduct').read_text().strip()}"
        except OSError:
            continue
    return None


@dataclass
class UsbEvent:
    t: float          # monotonic
    wall: float
    kind: str         # "gone" or "present"
    ids: str | None   # vid:pid when present


@dataclass
class UsbWatcher:
    """Polls sysfs for the phone's USB device and records every change.

    An enumeration whose ids are not in ADB_IDS (and not MTP/adb combos seen
    while Android runs) is classed by the caller; this only records.
    """
    serial: str = SERIAL
    interval: float = 0.05
    events: list = field(default_factory=list)

    def __post_init__(self):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._last = "unset"

    def start(self) -> "UsbWatcher":
        self._last = usb_presence(self.serial)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            cur = usb_presence(self.serial)
            if cur != self._last:
                with self._lock:
                    self.events.append(UsbEvent(time.monotonic(), time.time(),
                                                "present" if cur else "gone", cur))
                self._last = cur
            time.sleep(self.interval)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)

    def mark(self) -> int:
        with self._lock:
            return len(self.events)

    def since(self, mark: int) -> list:
        with self._lock:
            return list(self.events[mark:])

    def wait_gone(self, mark: int, seconds: float) -> UsbEvent:
        d = Deadline(seconds, "the phone to drop off USB")
        while True:
            for e in self.since(mark):
                if e.kind == "gone":
                    return e
            d.check()
            time.sleep(0.02)


def fastboot_ids_seen(events: list) -> list:
    """Enumerations that are not Android's adb/MTP gadget: the bootloader."""
    return [e for e in events if e.kind == "present" and e.ids and e.ids not in ADB_IDS
            and not e.ids.startswith("18d1:4e")]


def env_flag(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v else None


def note(msg: str) -> None:
    log(msg)
