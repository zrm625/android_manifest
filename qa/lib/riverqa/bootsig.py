"""The three signals that catch a reset before boot completes. Pure logic.

1. Kernel-log chain: before every planned reboot the harness writes a marker
   token to /dev/kmsg (several copies, because ramoops here has no ECC and
   flips bits). At the next boot completion, /sys/fs/pstore/console-ramoops*
   holds the previous kernel's log, which must contain a copy of this
   attempt's leaving marker with no new kernel session after it. The
   marker's 64-bit random nonce ties the log to this attempt's write
   (amendment A2.1, chain_verdict).
2. Uptime: /proc/uptime at boot completion must be at least the host time
   since the USB disconnect, minus a bootloader allowance A set by
   calibration. Only the fastboot and sysrq paths have an A: on both the
   reset happens at the disconnect. On the adb path (bench boot 1) the USB
   gadget drops seconds before the reset, so its uptime is INFO only
   (amendment A2). Any unplanned fastboot enumeration counts as an extra
   boot on every path.
3. Boot count: dropbox SYSTEM_BOOT entries since the wipe equal the boots
   expected since the wipe.
"""

from __future__ import annotations

import re
import secrets

from .results import FAIL, PASS, UNVERIFIED

MARKER_PREFIX = "RQM"
BANNER = b"Linux version "   # a kernel session's first line; matched within MAX_MARKER_FLIPS (R4-7)


NONCE_HEX = 16           # 64 random bits (amendment A2.1)


def make_marker(boot_id: str, nonce: str | None = None) -> str:
    """``RQM:<16 hex nonce>:<8 hex boot_id>``. A copy of another write
    differs in about 15 of the 16 nonce characters, so it can only match
    within MAX_MARKER_FLIPS by chance (about 1e-13; DESIGN.md, A2.1)."""
    nonce = nonce or secrets.token_hex(NONCE_HEX // 2)
    return f"{MARKER_PREFIX}:{nonce}:{boot_id.replace('-', '')[:8]}"


def hamming(a: bytes, b: bytes) -> int:
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b))


MAX_MARKER_FLIPS = 3     # a copy matches within this many wrong characters (A1′ rule 1)


def marker_offsets(log: bytes, marker: str, max_flips: int = MAX_MARKER_FLIPS) -> list[int]:
    """Offsets of every copy of ``marker`` in ``log``, exact or within
    ``max_flips`` wrong characters (ramoops bit flips), in log order."""
    m = marker.encode()
    n = len(m)
    hits = set()
    # Anchor on 6-character slices of the token, every 3 characters, to keep
    # the scan cheap. A character sits in at most 2 slices, so max_flips
    # flips break at most 2 * max_flips of them; the 29-character marker
    # has 8 slices, so a copy within 3 flips always keeps one intact.
    for k in range(0, n - 5, 3):
        piece = m[k:k + 6]
        start = 0
        while True:
            i = log.find(piece, start)
            if i < 0:
                break
            off = i - k
            if off >= 0 and hamming(log[off:off + n], m) <= max_flips:
                hits.add(off)
            start = i + 1
    return sorted(hits)


def fuzzy_offsets(log: bytes, pattern: bytes, max_flips: int = MAX_MARKER_FLIPS) -> list[int]:
    """Offsets of every copy of ``pattern`` in ``log`` within ``max_flips``
    wrong characters. The pattern is cut into max_flips + 1 disjoint
    pieces; a copy with at most max_flips flips keeps at least one piece
    intact, so every such copy is found. Pure."""
    n = len(pattern)
    k = max_flips + 1
    if n < k:
        raise ValueError("pattern too short for the flip budget")
    bounds = [round(i * n / k) for i in range(k + 1)]
    hits = set()
    for a, b in zip(bounds, bounds[1:]):
        piece, start = pattern[a:b], 0
        while True:
            i = log.find(piece, start)
            if i < 0:
                break
            off = i - a
            if off >= 0 and off + n <= len(log) and hamming(log[off:off + n], pattern) <= max_flips:
                hits.add(off)
            start = i + 1
    return sorted(hits)


def banner_offsets(log: bytes) -> list[int]:
    """Kernel session banners (``Linux version ``) within MAX_MARKER_FLIPS
    wrong characters, like the marker (review round 4: on river the banner
    at the start of every saved pstore log reads ``Linu\xf8 ver\xf3ion``,
    which an exact match never found)."""
    return fuzzy_offsets(log, BANNER)


def chain_verdict(pstore_log: bytes | None, marker: str | None) -> tuple[str, str]:
    """Judge the kernel-log chain for one boot (amendment A2.1).

    PASS: the previous kernel's log holds at least one copy of this
    attempt's marker (within MAX_MARKER_FLIPS) and no new kernel session
    banner (also within MAX_MARKER_FLIPS) after the first copy. The nonce alone shows the log belongs to the
    kernel that wrote this marker; printk stamps play no part. An extra
    kernel that ran past ramoops init replaces the log, so a readable log
    with no copy, or a new session after one, is a FAIL. No marker, or a
    missing or empty log, is UNVERIFIED."""
    if not marker:
        return UNVERIFIED, "no leaving marker was written before this boot"
    if pstore_log is None or len(pstore_log) == 0:
        return UNVERIFIED, "pstore console-ramoops missing or empty"
    offs = marker_offsets(pstore_log, marker)
    if not offs:
        return FAIL, ("the previous kernel's log holds no copy of this attempt's leaving marker: another kernel "
                      "ran after ramoops init and replaced it")
    if any(b > offs[0] for b in banner_offsets(pstore_log)):
        return FAIL, "a new kernel session started after the leaving marker"
    exact = pstore_log.count(marker.encode())
    return PASS, (f"previous kernel log holds {len(offs)} copies of the leaving marker ({exact} exact) "
                  "and no new session after them")


DISCONNECT_BOUND_S = 0.5    # first_gone - last_present above this: the disconnect is not bracketed (R4)
STATUS_READ_BOUND_S = 1.0   # a completion status read slower than this: the uptime is not pinned (R4)


def timing_bracket_problem(disconnect_gap_s: float | None, status_read_s: float | None) -> str | None:
    """Why the host clock of one gated boot can't be trusted, or None.

    Review round 4: the clock starts at the earliest possible disconnect
    (the last poll that still saw the phone), which only makes elapsed
    longer. But if the watcher saw the phone gone much later than it last
    saw it present (a busy host), or the status read that carried uptime
    was slow, the boot is UNVERIFIED rather than judged."""
    if disconnect_gap_s is None:
        return "the USB disconnect has no last-present poll to bracket it"
    if disconnect_gap_s > DISCONNECT_BOUND_S:
        return (f"the USB disconnect is bracketed only to {disconnect_gap_s:.4f} s "
                f"(first_gone - last_present > {DISCONNECT_BOUND_S} s): the host was too busy to poll")
    if status_read_s is None:
        return "the completion status read has no recorded latency"
    if status_read_s > STATUS_READ_BOUND_S:
        return f"the completion status read took {status_read_s:.4f} s (> {STATUS_READ_BOUND_S} s)"
    return None


def uptime_verdict(uptime_s: float, elapsed_since_disconnect_s: float, allowance_s: float | None,
                   unplanned_fastboot: int, *, disconnect_gap_s: float | None,
                   status_read_s: float | None) -> tuple[str, str]:
    """The uptime gate of one boot on a gated path. ``elapsed`` runs from the
    earliest possible disconnect (last_present) to the end of the status
    read, so every uncertainty makes it longer, toward FAIL. A loose
    bracket on either end is UNVERIFIED (timing_bracket_problem)."""
    if unplanned_fastboot:
        return FAIL, f"{unplanned_fastboot} unplanned fastboot enumeration(s): extra boot"
    if allowance_s is None:
        return UNVERIFIED, "no calibrated bootloader allowance for this reboot path"
    problem = timing_bracket_problem(disconnect_gap_s, status_read_s)
    if problem:
        return UNVERIFIED, problem
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
