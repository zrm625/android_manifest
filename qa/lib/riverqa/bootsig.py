"""The three signals that catch a reset before boot completes. Pure logic.

1. Kernel-log chain: before every planned reboot the harness writes a marker
   token to /dev/kmsg (several copies, because ramoops here has no ECC and
   flips bits). At the next boot completion, /sys/fs/pstore/console-ramoops*
   holds the previous kernel's log, which must contain the latest leaving
   marker with no new kernel session after it.
   PASS needs a copy proven by its stamp (amendment A2, chain_verdict).
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
BANNER_RE = re.compile(rb"Linux version \d+\.\d+")


def make_marker(boot_id: str, nonce: str | None = None) -> str:
    nonce = nonce or secrets.token_hex(4)
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
    # Anchor on every 6-character slice of the token to keep the scan cheap.
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


TS_RE = re.compile(rb"\[\s*(\d{1,6})\.(\d{6})\]")
STRICT_STAMP_RE = re.compile(rb"\[( *)(\d{1,6})\.(\d{6})\] ")


def _strict_at(buf: bytes, pos: int) -> tuple[float, int] | None:
    """(stamp, end) of a strictly parsed stamp starting at ``pos``: the
    kernel's own "[%5lu.%06lu] " layout with no leading zero."""
    m = STRICT_STAMP_RE.match(buf, pos)
    if not m:
        return None
    pad, sec = m.group(1), m.group(2)
    if len(sec) > 1 and sec.startswith(b"0"):
        return None                     # a padding space flipped to '0'
    if len(pad) + len(sec) != max(5, len(sec)):
        return None
    return int(sec) + int(m.group(3)) / 1e6, m.end()


def stamp_before(log: bytes, off: int) -> float | None:
    """The strictly parsed stamp that ends exactly at ``off`` (the "[...] "
    straight before a marker copy), or None. Only the stamp next to the
    copy counts, never an earlier one on a merged line."""
    j = log.rfind(b"[", max(0, off - 16), off)
    if j < 0:
        return None
    hit = _strict_at(log, j)
    return hit[0] if hit is not None and hit[1] == off else None


def _same_us(a: float, b: float) -> bool:
    return round(a * 1e6) == round(b * 1e6)


def proven_marker(log: bytes, marker: str, live: list[float]) -> tuple[tuple[int, float] | None, str]:
    """((offset, stamp), note) of the last copy of ``marker`` proven by its
    stamp (A1′ rules 1-2, kept by A2 for the chain): a copy within
    MAX_MARKER_FLIPS wrong characters whose strict stamp (stamp_before)
    equals one of ``live`` to the microsecond. (None, why) if no copy is
    proven."""
    offs = marker_offsets(log, marker)
    if not offs:
        return None, f"no copy of the leaving marker (within {MAX_MARKER_FLIPS} bit flips) in the previous kernel's log"
    proven, seen = [], []
    for off in offs:
        t = stamp_before(log, off)
        if t is None:
            continue
        seen.append(t)
        if any(_same_us(t, x) for x in live):
            proven.append((off, t))
    if not proven:
        got = ", ".join(f"{t:.6f}" for t in seen) or "none parse strictly"
        return None, (f"{len(offs)} copies of the leaving marker, but no stamp next to one ({got}) equals a live "
                      f"stamp of this write ({', '.join(f'{x:.6f}' for x in live)}): not proven the same write")
    off, t = proven[-1]
    return (off, t), f"copy at {off} proven by its stamp {t:.6f} ({len(proven)} of {len(offs)} copies proven)"


def marker_printk_stamps(dmesg: str, marker: str) -> list[float]:
    """The printk stamps of every copy of ``marker`` in live dmesg, in order."""
    out = []
    for line in dmesg.splitlines():
        if line.rstrip().endswith(marker):
            m = TS_RE.match(line.encode())
            if m:
                out.append(int(m.group(1)) + int(m.group(2)) / 1e6)
    return out


def chain_verdict(pstore_log: bytes | None, marker: str | None, live: list[float] | None) -> tuple[str, str]:
    """Judge the kernel-log chain for one boot (amendment A2).

    ``live`` holds the stamps of this attempt's marker copies, read back
    from live dmesg right after the write. PASS needs a copy proven by its
    stamp (proven_marker) with no new kernel session after it. An extra
    kernel that ran past ramoops init replaces the log, so a readable log
    with no copy of this attempt's marker at all, or a new session after
    one, is a FAIL. A missing or empty log, a marker that was not read back,
    or copies whose stamps are all damaged is UNVERIFIED."""
    if not marker:
        return UNVERIFIED, "no leaving marker was written before this boot"
    if pstore_log is None or len(pstore_log) == 0:
        return UNVERIFIED, "pstore console-ramoops missing or empty"
    offs = marker_offsets(pstore_log, marker)
    if not offs:
        return FAIL, ("the previous kernel's log holds no copy of this attempt's leaving marker: another kernel "
                      "ran after ramoops init and replaced it")
    if BANNER_RE.search(pstore_log[offs[0]:]):
        return FAIL, "a new kernel session started after the leaving marker"
    if not live:
        return UNVERIFIED, "the leaving marker was not read back from live dmesg, so no copy can be proven"
    rec, why = proven_marker(pstore_log, marker, live)
    if rec is None:
        return UNVERIFIED, why
    return PASS, f"previous kernel log ends with the leaving marker ({why})"


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
