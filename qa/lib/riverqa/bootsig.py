"""The three signals that catch a reset before boot completes. Pure logic.

1. Kernel-log chain: before every planned reboot the harness writes a marker
   token to /dev/kmsg (several copies, because ramoops here has no ECC and
   flips bits). At the next boot completion, /sys/fs/pstore/console-ramoops*
   holds the previous kernel's log, which must contain the latest leaving
   marker with no new kernel session after it.
2. Uptime: /proc/uptime at boot completion must be at least the host time
   since the clock start, minus a bootloader allowance A set by calibration.
   On the fastboot path the clock starts at the USB disconnect after the
   final ``fastboot reboot``. On the adb path it starts at the old kernel's
   reset (amendment A1, reset_anchor), because river's USB gadget drops
   seconds before the reset. Any unplanned fastboot enumeration counts as an
   extra boot.
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


def stamps(log: bytes) -> list[float]:
    return [int(a) + int(b) / 1e6 for a, b in TS_RE.findall(log)]


def corroborated_last(ts: list[float], run: int = 3, window_s: float = 1.0) -> float | None:
    """The earliest stamp of the last run of ``run`` consecutive
    non-decreasing stamps spanning at most ``window_s`` (amendment A1, D2;
    review round 2). ramoops flips bits. A flipped stamp that leaves the
    window (120 -> 128, 41.1 -> 45.1) breaks the run. One that stays inside
    it (110.0 -> 110.4 after 109.7, 109.9) can end a run, but the run's
    first stamp is either a true stamp or, if it is the flipped one, no
    later than the true stamp after it. So one flip can never move the
    result past the true last stamp. None if there is no such run."""
    for i in range(len(ts) - 1, run - 2, -1):
        seq = ts[i - run + 1:i + 1]
        if all(x <= y for x, y in zip(seq, seq[1:])) and seq[-1] - seq[0] <= window_s:
            return seq[0]
    return None


def last_timestamp(log: bytes) -> float | None:
    """A trustworthy timestamp near the end of the previous kernel's log
    (see corroborated_last), or None. Used to spot a log that ends seconds into a
    boot; never the maximum of the last few stamps."""
    return corroborated_last(stamps(log[-8192:]))


MARKER_TS_TOL_S = 0.05   # live readback vs pstore: the same write, allowing for a flipped low digit
LOOSE_TS_RE = re.compile(rb"(\d{1,6})[^\d\n](\d{6})")


def marker_stamps_in_log(log: bytes, marker: str, max_flips: int = 3) -> list[float]:
    """The printk timestamps of the copies of ``marker`` in a pstore log.
    Exact copies if there are any, otherwise copies within ``max_flips``
    wrong characters. The stamp is read from the start of the copy's own
    line with a tolerant parse (any one byte between the seconds and the
    microseconds, brackets ignored), because ramoops flips bits there too."""
    m = marker.encode()
    offs, start = [], 0
    while True:
        i = log.find(m, start)
        if i < 0:
            break
        offs.append(i)
        start = i + 1
    if not offs:
        off = find_marker(log, marker, max_flips)
        offs = [] if off is None else [off]
    out = []
    for off in offs:
        line = log[log.rfind(b"\n", 0, off) + 1:off]
        hit = TS_RE.search(line) or LOOSE_TS_RE.search(line)
        if hit:
            out.append(int(hit.group(1)) + int(hit.group(2)) / 1e6)
    return out


def reset_anchor(pstore_log: bytes, marker: str, pair: dict,
                 tail_bound: tuple[float, float] | None) -> tuple[dict | None, str]:
    """Map the old kernel's reset onto the host's monotonic clock (A1).

    ``pair`` ties the leaving marker's own printk timestamp (read back from
    live dmesg right after one verified write) to the host time of that
    write: {"marker", "printk_ts", "host_mono", "half_width"}. printk and the
    marker share one clock, so no /proc/uptime (CLOCK_BOOTTIME) is involved.

    The pair must be for this marker, and the marker's own stamp in pstore
    must match the live readback within MARKER_TS_TOL_S; otherwise the
    readback and the log are not the same write and the result is
    UNVERIFIED (review round 2). The gap to the reset is measured inside
    pstore, from the marker's pstore stamp to the reset stamp, so both come
    from one log. The reset is the earliest stamp of the last corroborated
    run after the marker (corroborated_last); it must not precede the
    marker. The result is the earliest host time the reset could have been:
    the pairing's half-width and the live/pstore difference are both taken
    off. With ``tail_bound`` (disconnect, disconnect + the largest
    calibrated shutdown tail) the anchor must fall inside it, allowing for
    that uncertainty. Anything ambiguous or out of bounds: (None, why)."""
    if pair.get("marker") != marker:
        return None, f"the printk pairing is for {pair.get('marker')}, not this boot's marker {marker}"
    off = find_marker(pstore_log, marker)
    if off is None:
        return None, "leaving marker not in the previous kernel's log"
    live = float(pair["printk_ts"])
    found = marker_stamps_in_log(pstore_log, marker)
    if not found:
        return None, "the leaving marker's own timestamp in pstore is unreadable"
    pts = min(found, key=lambda t: abs(t - live))
    slack = abs(pts - live)
    if slack > MARKER_TS_TOL_S:
        return None, (f"the leaving marker's pstore stamp {pts:.6f} does not match the live readback "
                      f"{live:.6f}: not the same write")
    after = stamps(pstore_log[off:])
    anchor = corroborated_last(after)
    if anchor is None:
        return None, f"no corroborated timestamps after the marker ({len(after)} stamps)"
    if anchor < pts:
        return None, f"reset stamp {anchor:.3f} precedes the marker's {pts:.3f}"
    hw = float(pair["half_width"]) + slack
    mid = float(pair["host_mono"]) + (anchor - pts)
    earliest, latest = mid - hw, mid + hw
    if tail_bound is not None:
        lo, hi = tail_bound
        if latest < lo or earliest > hi:
            return None, (f"reset at {mid - lo:+.2f} s from the USB disconnect is outside "
                          f"[0, {hi - lo:.2f}] s (the largest calibrated shutdown tail)")
    return {"printk_ts": anchor, "marker_ts": pts, "marker_ts_live": live, "host_mono": earliest,
            "half_width": hw, "tail_s": None if tail_bound is None else round(earliest - tail_bound[0], 3)}, \
        f"reset at printk {anchor:.3f} s, {anchor - pts:.2f} s after the marker (pstore)"


def marker_printk_ts(dmesg: str, marker: str) -> float | None:
    """The printk timestamp of the last copy of ``marker`` in live dmesg."""
    found = None
    for line in dmesg.splitlines():
        if marker in line:
            m = TS_RE.search(line.encode())
            if m:
                found = int(m.group(1)) + int(m.group(2)) / 1e6
    return found


def chain_verdict(pstore_log: bytes | None, marker: str | None) -> tuple[str, str]:
    """Judge the kernel-log chain for one boot."""
    if not marker:
        return UNVERIFIED, "no leaving marker was written before this boot"
    if pstore_log is None or len(pstore_log) == 0:
        return UNVERIFIED, "pstore console-ramoops missing or empty"
    off = find_marker(pstore_log, marker)
    if off is None:
        # Without the marker this can only be FAIL or UNVERIFIED. A log too
        # short to corroborate is judged by its few stamps.
        end = last_timestamp(pstore_log)
        if end is None:
            few = stamps(pstore_log[-8192:])
            end = max(few) if few else None
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
