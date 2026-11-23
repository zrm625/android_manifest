"""The test phone over adb and fastboot. Every call names the serial and has a
deadline; nothing here ever talks to another device."""

from __future__ import annotations

import base64
import binascii
import os
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .core import CODENAME, SERIAL, Deadline, host_boottime, log, run

USB_ROOT = Path("/sys/bus/usb/devices")


class GuardError(RuntimeError):
    """The phone is missing, or is not the river test phone."""


class Unreachable(RuntimeError):
    """Neither adb nor fastboot answers."""


@dataclass
class BootStatus:
    """One read of boot_id, sys.boot_completed and /proc/uptime, all from
    one shell command, with the host clock just before and just after it
    (review round 4: the read's latency bounds when the uptime was taken)."""
    boot_id: str
    completed: bool
    uptime: float
    t0: float          # time.monotonic() just before the read
    t1: float          # time.monotonic() just after it
    host_wall: float   # time.time() halfway through the read
    host_boottime: float | None = None   # core.host_boottime() just before the read (R9-7)

    @property
    def read_s(self) -> float:
        """How long the read took: the uptime was taken somewhere in it."""
        return self.t1 - self.t0

    @property
    def host_mono(self) -> float:
        return (self.t0 + self.t1) / 2


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


@dataclass
class FilesRead:
    """The outcome of Device.read_files: (path, bytes) per file, in glob
    order. Callers must check ``ok``."""
    ok: bool
    files: list
    why: str = ""
    no_match: bool = False      # the directory was listed and the glob matched nothing

    @property
    def data(self) -> bytes:
        return b"".join(b for _p, b in self.files)


B64_EXIT = {3: "a listed file vanished or could not be stat'ed", 4: "base64 failed",
            5: "the directory is missing or unreadable", 6: "listing the directory failed",
            7: "no file matches"}


def b64_script(glob: str) -> str:
    """The remote side of read_files. ``glob`` is a harness constant, never
    outside input; wildcards are allowed only in its last component.

    Review round 3: the directory must exist, be readable and list (exit
    5 or 6), and a pattern that matches nothing exits 7, so neither reads
    as an empty result. Review round 4: the directory is enumerated once,
    by ``ls``, and the read set is exactly the listed names that match the
    pattern (a shell ``case``, with globbing off). A listed file that
    vanishes or can't be read before it is read makes stat or base64 fail
    (exit 3 or 4), never a quiet skip."""
    if not re.fullmatch(r"/[A-Za-z0-9_./*-]+", glob):
        raise ValueError(f"unsafe glob {glob!r}")
    d, pat = glob.rsplit("/", 1)
    d = d or "/"
    if "*" in d:
        raise ValueError(f"wildcards only in the last component: {glob!r}")
    if not pat:
        raise ValueError(f"no file pattern in {glob!r}")
    return (f'd={d}; [ -d "$d" ] && [ -r "$d" ] || exit 5; l=$(ls -1 "$d") || exit 6; '
            "set -f; IFS='\n'; n=0; m=0; "
            f'for b in $l; do case "$b" in {pat}) ;; *) continue ;; esac; m=$((m+1)); f="$d/$b"; '
            's=$(stat -c %s "$f") || exit 3; echo "__FILE__ $s $f"; base64 "$f" || exit 4; echo "__EOF__"; '
            'n=$((n+1)); done; [ $m -gt 0 ] || exit 7; echo "__RC__ $n"')


def parse_b64_dump(rc: int, text: str, err: str = "") -> FilesRead:
    """Parse b64_script's output. Pure."""
    if rc == 7:
        return FilesRead(False, [], B64_EXIT[7], no_match=True)
    if rc != 0:
        return FilesRead(False, [], f"remote exit {rc} ({B64_EXIT.get(rc, 'failed')}): {err.strip()[-200:]}")
    lines = text.replace("\r", "").split("\n")
    files, cur, size, chunks = [], None, 0, []
    for line in lines:
        if line.startswith("__FILE__ "):
            if cur is not None:
                return FilesRead(False, files, f"{cur}: no end sentinel")
            parts = line.split(" ", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                return FilesRead(False, files, f"bad file header {line[:80]!r}")
            size, cur, chunks = int(parts[1]), parts[2], []
        elif line == "__EOF__":
            if cur is None:
                return FilesRead(False, files, "end sentinel without a file")
            try:
                data = base64.b64decode("".join(chunks), validate=True)
            except (binascii.Error, ValueError) as e:
                return FilesRead(False, files, f"{cur}: bad base64 ({e})")
            if len(data) != size:
                return FilesRead(False, files, f"{cur}: read {len(data)} bytes, stat says {size} (partial read)")
            files.append((cur, data))
            cur = None
        elif line.startswith("__RC__ "):
            if cur is not None:
                return FilesRead(False, files, f"{cur}: no end sentinel")
            n = line.split(" ", 1)[1].strip()
            if not n.isdigit() or int(n) != len(files):
                return FilesRead(False, files, f"closing line says {n} files, read {len(files)}")
            return FilesRead(True, files, "")
        elif cur is not None:
            chunks.append(line.strip())
    return FilesRead(False, files, "no closing line: the read did not finish")


def _is_reboot(args: tuple) -> bool:
    return bool(args) and str(args[0]).startswith("reboot")


SYSRQ_REBOOT = "sync; echo b > /proc/sysrq-trigger"


# The sysrq guard fails closed (R7-4): a command that mentions sysrq-trigger
# is refused unless it is one of a few read-only forms. reboot_sysrq() is
# the only way to write the trigger.
_SYSRQ_META = set("<>|&;#`$(){}*?~\\\n\r!")
_SYSRQ_READERS = ("ls", "stat", "cat", "grep")
TRIGGER = "/proc/sysrq-trigger"


def _sysrq_read_only(cmd: str, depth: int = 0) -> bool:
    """Is this shell command one of the allowed read-only forms that
    mention sysrq-trigger (R7-4)? Pure.

    No shell syntax at all: no redirection, comment, ``;``, ``&``, ``|``,
    backtick, ``$``, brackets of a subshell or group, glob or ``~``. Then
    one of:

    - ``ls``, ``stat``, ``cat`` or ``grep`` with any options and operands
      (none of them can write a file without a redirection);
    - ``test -e PATH`` or ``[ -e PATH ]``;
    - exactly ``cp /proc/sysrq-trigger DEST`` with no options (R8-5),
      where DEST neither mentions sysrq-trigger nor may be /proc
      (``proc``, ``.``, ``..``, ``/``);
    - ``su [user] -c CMD`` with CMD one of the above.

    Everything else that mentions sysrq-trigger is refused, including
    anything shlex can't split."""
    if any(c in _SYSRQ_META for c in cmd):
        return False
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return False
    if not argv:
        return False
    name = argv[0]
    if name == "su" and depth == 0:
        rest = argv[1:]
        if rest and not rest[0].startswith("-"):
            rest = rest[1:]            # su root -c ...
        if len(rest) == 2 and rest[0] == "-c":
            return _sysrq_read_only(rest[1], depth + 1)
        return False
    if name == "[":
        return len(argv) == 4 and argv[1] == "-e" and argv[3] == "]"
    if any("[" in t or "]" in t for t in argv):
        return False
    if name == "test":
        return len(argv) == 3 and argv[1] == "-e"
    if name in _SYSRQ_READERS:
        return True
    if name != "cp":
        return False
    # R8-5: exactly `cp /proc/sysrq-trigger DEST`, no options at all (no
    # -t cluster, no Toybox -s symlink), DEST free of the trigger and not
    # /proc, ., .. or /.
    if len(argv) != 3 or argv[1] != TRIGGER:
        return False
    dest = argv[2]
    tail = Path(dest.rstrip("/") or "/").name
    return not (dest.startswith("-") or "sysrq-trigger" in dest or tail in ("proc", ".", "..", ""))


def _is_sysrq(args: tuple) -> bool:
    """Could this adb call write the sysrq trigger? It fails closed (R7-4):
    a call that mentions sysrq-trigger anywhere is refused unless it is
    ``adb shell`` (or ``exec-out``) with a command _sysrq_read_only allows.
    Other mentions of the word, such as /proc/sys/kernel/sysrq, are not
    affected. Indirect targets (a variable, a glob, a symlink written
    later) are out of scope: the threat model is accidental breakage, not
    a crafted command (DESIGN, round 6)."""
    cmd = " ".join(str(a) for a in args)
    if "sysrq-trigger" not in cmd:
        return False
    if not args or str(args[0]) not in ("shell", "exec-out"):
        return True
    return not _sysrq_read_only(" ".join(str(a) for a in args[1:]))


class Device:
    def __init__(self, serial: str = SERIAL):
        if not serial:
            raise GuardError("no serial")
        self.serial = allowed_serial(serial)

    # --- adb -------------------------------------------------------------
    def adb(self, *args: str, timeout: float = 30, input=None, text=True):
        if _is_reboot(args) or _is_sysrq(args):
            raise GuardError("reboot only through Device.reboot_adb() or reboot_sysrq(), which guard first")
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

    def read_files(self, glob: str, timeout: float = 60) -> "FilesRead":
        """Every file matching ``glob``, read binary-safe (review round 2).
        ``adb exec-out`` reports 0 even when the remote command failed, so
        each file goes over the shell protocol as base64 between sentinels,
        with its size from stat and a closing line that carries the file
        count. FilesRead.ok is False on any remote failure, a missing
        sentinel, or a decoded size that differs from stat (a partial read)."""
        try:
            rc, out, err = self.sh_rc(b64_script(glob), timeout=timeout)
        except TimeoutError:
            return FilesRead(False, [], "timed out")
        return parse_b64_dump(rc, out, err)

    def reboot_adb(self, target: str | None = None, timeout: float = 30):
        """``adb reboot [target]``, only after the river guard passes (adb
        device or recovery mode, ro.product.device and ro.serialno)."""
        self.reboot_guard()
        return self._adb(*(["reboot", target] if target else ["reboot"]), timeout=timeout)

    def reboot_sysrq(self, timeout: float = 15) -> None:
        """The immediate kernel reboot (amendment A2): ``sync``, then ``echo b
        > /proc/sysrq-trigger`` as root, only after the river guard. The
        kernel resets at once, with no Android shutdown, and USB drops about
        1.2 s later. The proc trigger works even with kernel.sysrq=0. The
        shell never returns cleanly, so its exit status is not checked."""
        self.guard()
        if self.sh("id -u").strip() != "0":
            raise GuardError("the sysrq reboot needs adb root")
        try:
            self._adb("shell", SYSRQ_REBOOT, timeout=timeout)
        except TimeoutError:
            pass

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
        if not serialno:
            raise GuardError(f"adb -s {self.serial}: ro.serialno is empty or unreadable. Refusing.")
        if serialno != self.serial:
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
        b0 = host_boottime()
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
        return BootStatus(lines[0].strip(), lines[1].strip() == "1", uptime, t0, t1, w0 + (t1 - t0) / 2, b0)

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

    def kmsg(self, text: str, copies: int = 4) -> bool:
        """Write a marker to the kernel log (needs root). True only if every
        copy was written: the copies are chained with && and the remote
        status is checked (review round 2)."""
        safe = "".join(c for c in text if c.isalnum() or c in ":-_")
        cmd = " && ".join([f"echo '<6>{safe}' > /dev/kmsg"] * copies)
        try:
            rc, _, _ = self.sh_rc(cmd, timeout=15)
        except TimeoutError:
            return False
        return rc == 0

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

    def fb_mode_set(self) -> None:
        """Motorola's ``fastboot oem fb_mode_set``: the bootloader stops in
        fastboot on every boot until it is cleared. Only fbmode.arm may call
        this, after it journals the obligation (review round 4)."""
        self.fastboot_guard()
        self.fastboot_ok("oem", "fb_mode_set", timeout=30)

    def fb_mode_clear(self) -> None:
        """``fastboot oem fb_mode_clear``; harmless when nothing is set."""
        self.fastboot_guard()
        self.fastboot_ok("oem", "fb_mode_clear", timeout=30)

    def fastboot_continue(self) -> None:
        """``fastboot continue``: boot the current slot from the bootloader
        with no reset."""
        self.fastboot_guard()
        self.fastboot_ok("continue", timeout=30)

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
    """One change seen by the watcher. ``t`` is the end of the poll that saw
    the change: for "gone", the latest time the disconnect can have
    happened (first_gone). ``last_present`` (gone events only) is the start
    of the last poll that still saw the phone: the earliest time the
    disconnect can have happened. None if no poll saw it present."""
    t: float          # monotonic
    wall: float
    kind: str         # "gone" or "present"
    ids: str | None   # vid:pid when present
    last_present: float | None = None

    @property
    def gap(self) -> float | None:
        """first_gone - last_present: how loosely the disconnect is bracketed."""
        return None if self.last_present is None else self.t - self.last_present


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
        self._last_present: float | None = None

    def start(self) -> "UsbWatcher":
        self._poll(record=False)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _poll(self, record: bool = True) -> None:
        """One sysfs poll, bracketed by the clock (review round 4): a gone
        event carries the start of the last poll that saw the phone, so a
        host too busy to poll widens the bracket instead of moving the
        clock."""
        before = time.monotonic()
        cur = usb_presence(self.serial)
        after = time.monotonic()
        if cur != self._last and record:
            with self._lock:
                self.events.append(UsbEvent(after, time.time(), "present" if cur else "gone", cur,
                                            None if cur else self._last_present))
        self._last = cur
        if cur:
            self._last_present = before

    def _loop(self):
        while not self._stop.is_set():
            self._poll()
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
