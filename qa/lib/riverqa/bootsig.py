"""The three signals that catch a reset before boot completes. Pure logic.

1. Kernel-log chain: before every planned reboot the harness writes a marker
   token to /dev/kmsg (several copies, because ramoops here has no ECC and
   flips bits). At the next boot completion, /sys/fs/pstore/console-ramoops*
   holds the previous kernel's log, which must contain the latest leaving
   marker with no new kernel session after it.
2. Uptime: /proc/uptime at boot completion must be at least the host time
   since the first USB disconnect after the reboot command, minus a
   bootloader allowance A set by calibration. Any unplanned fastboot
   enumeration counts as an extra boot.
3. Boot count: dropbox SYSTEM_BOOT entries since the wipe equal the boots
   expected since the wipe.
"""

from __future__ import annotations

import re
import secrets

from .results import FAIL, PASS, UNVERIFIED

MARKER_PREFIX = "RQM"
BANNER_RE = re.compile(rb"Linux version \d+\.\d+")


def make_marker(boot_id: str, nonce: str | None = None) -> str:
    nonce = nonce or secrets.token_hex(4)
    return f"{MARKER_PREFIX}:{nonce}:{boot_id.replace('-', '')[:8]}"


def hamming(a: bytes, b: bytes) -> int:
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b))


def find_marker(log: bytes, marker: str, max_flips: int = 3) -> int | None:
    """Offset of the last copy of ``marker`` in ``log``, tolerating up to
    ``max_flips`` wrong characters (ramoops bit flips). None if absent."""
    m = marker.encode()
    n = len(m)
    exact = log.rfind(m)
    if exact >= 0:
        return exact
    # Anchor on any 6-character slice of the token to keep the scan cheap.
    best = None
    for k in range(0, n - 5, 3):
        piece = m[k:k + 6]
        start = 0
        while True:
            i = log.find(piece, start)
            if i < 0:
                break
            off = i - k
            if off >= 0 and hamming(log[off:off + n], m) <= max_flips:
                if best is None or off > best:
                    best = off
            start = i + 1
    return best


TS_RE = re.compile(rb"\[\s*(\d{1,6})\.(\d{6})\]")
EARLY_END_S = 30.0


def last_timestamp(log: bytes) -> float | None:
    """The latest kernel timestamp near the end of a console log, or None.
    Bit flips can corrupt single timestamps, so take the largest of the last
    few that parse."""
    stamps = [int(a) + int(b) / 1e6 for a, b in TS_RE.findall(log[-8192:])]
    if not stamps:
        return None
    return max(stamps[-20:])


def chain_verdict(pstore_log: bytes | None, marker: str | None) -> tuple[str, str]:
    """Judge the kernel-log chain for one boot."""
    if not marker:
        return UNVERIFIED, "no leaving marker was written before this boot"
    if pstore_log is None or len(pstore_log) == 0:
        return UNVERIFIED, "pstore console-ramoops missing or empty"
    off = find_marker(pstore_log, marker)
    if off is None:
        end = last_timestamp(pstore_log)
        if end is not None and end < EARLY_END_S:
            return FAIL, (f"previous kernel log ends {end:.1f} s into a boot with no leaving marker: "
                          "another kernel session ran in between")
        if BANNER_RE.search(pstore_log) and marker_density(pstore_log) == 0:
            return FAIL, "previous kernel log has no leaving marker: another kernel session ran in between"
        return UNVERIFIED, "leaving marker not found (log unreadable?)"
    after = pstore_log[off:]
    if BANNER_RE.search(after):
        return FAIL, "a new kernel session started after the leaving marker"
    return PASS, "previous kernel log ends with the leaving marker"


def marker_density(log: bytes) -> int:
    return log.count(MARKER_PREFIX.encode() + b":")


def uptime_verdict(uptime_s: float, elapsed_since_disconnect_s: float, allowance_s: float | None,
                   unplanned_fastboot: int) -> tuple[str, str]:
    if unplanned_fastboot:
        return FAIL, f"{unplanned_fastboot} unplanned fastboot enumeration(s): extra boot"
    if allowance_s is None:
        return UNVERIFIED, "no calibrated bootloader allowance for this reboot path"
    floor = elapsed_since_disconnect_s - allowance_s
    if uptime_s >= floor:
        return PASS, f"uptime {uptime_s:.1f} s >= {elapsed_since_disconnect_s:.1f} s - A {allowance_s:.1f} s"
    return FAIL, (f"uptime {uptime_s:.1f} s < {elapsed_since_disconnect_s:.1f} s - A {allowance_s:.1f} s: "
                  "an extra bootloader pass or reset")


def compute_allowance(normal_passes: list[float], extra_pass: float | None) -> tuple[float | None, str]:
    """A for one reboot path.

    A >= the longest normal bootloader pass, and A < the shortest normal pass
    plus one more pass. ``extra_pass`` is one more bootloader pass measured as
    reset to kernel start only (the flash path's normal pass). A is placed in
    the middle of that window. None if no such A exists.
    """
    if len(normal_passes) < 2 or extra_pass is None or extra_pass <= 0:
        return None, "not enough normal passes or no extra-pass length"
    lo, hi = max(normal_passes), min(normal_passes) + extra_pass
    if lo >= hi:
        return None, f"no workable A: longest normal pass {lo:.2f} s >= shortest {min(normal_passes):.2f} + extra {extra_pass:.2f}"
    a = (lo + hi) / 2
    return round(a, 2), f"A in [{lo:.2f}, {hi:.2f}) s; chosen {a:.2f} s"


def bootloader_pass(uptime_s: float, elapsed_since_disconnect_s: float) -> float:
    """Host time from the USB disconnect to kernel start."""
    return elapsed_since_disconnect_s - uptime_s




def dropbox_tags(dump: str) -> list[str]:
    """Tags of every entry listed by `dumpsys dropbox` (no time filter)."""
    tags = []
    for line in dump.splitlines():
        m = re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)? (\S+)", line.strip())
        if m:
            tags.append(m.group(1))
    return tags


def boot_count_verdict(tags: list[str], expected: int) -> tuple[str, str]:
    n = tags.count("SYSTEM_BOOT")
    if n == expected:
        return PASS, f"{n} SYSTEM_BOOT entries, as expected since the wipe"
    if n > expected:
        return FAIL, f"{n} SYSTEM_BOOT entries, expected {expected}: an extra boot"
    return UNVERIFIED, f"{n} SYSTEM_BOOT entries, expected {expected} (not settled or dropbox lost entries)"
