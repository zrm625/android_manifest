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
   reset (amendment A1, reset_anchor: one exact marker record and a clean
   tail, otherwise UNVERIFIED), because river's USB gadget drops seconds
   before the reset. Any unplanned fastboot enumeration counts as an
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
    non-decreasing stamps spanning at most ``window_s``. Used only by the
    chain to spot a log that ends seconds into a boot (a FAIL signal); the
    A1 reset anchor uses the strict rule in clean_tail instead."""
    for i in range(len(ts) - 1, run - 2, -1):
        seq = ts[i - run + 1:i + 1]
        if all(x <= y for x, y in zip(seq, seq[1:])) and seq[-1] - seq[0] <= window_s:
            return seq[0]
    return None


def last_timestamp(log: bytes) -> float | None:
    """A timestamp near the end of the previous kernel's log (see
    corroborated_last), or None. Chain only."""
    return corroborated_last(stamps(log[-8192:]))


# --- amendment A1: the strict reset anchor (code review round 3) -------------
#
# Any ambiguity is UNVERIFIED. The marker is found by an exact match, every
# number comes from that one record, and the reset is the stamp of the log's
# final line only if the whole tail after the record is clean. Corruption
# turns into an UNVERIFIED boot and a rerun, never a different anchor.

STRICT_STAMP_RE = re.compile(rb"\[( *)(\d{1,6})\.(\d{6})\] ")
END_K = 5               # the last END_K lines must agree within END_WINDOW_S
END_WINDOW_S = 1.5      # river's last 5 shutdown lines span 1.12 s ("SMP: failed to stop" waits 1.1 s)
FINAL_K = 3             # the last FINAL_K lines must agree within FINAL_WINDOW_S
FINAL_WINDOW_S = 0.05   # river's last 3 lines ("Going down for restart now", ...) span under 1 ms
MEASURE_MAX_GAP_S = 5.0  # the consecutive-stamp ceiling while step 1 measures (no calibration yet)
GAP_MARGIN_S = 1.0      # calibrated max_stamp_gap = the largest gap step 1 saw + this


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


def line_stamps(line: bytes) -> list[float] | None:
    """Every printk stamp in one console line, parsed strictly. The line
    must start with one. A record printed without a trailing newline puts
    the next record's stamp later on the same line (river's
    msm_pm_qos_remove_request does this); every such stamp-shaped token
    must parse strictly too. None if anything does not parse."""
    first = _strict_at(line, 0)
    if first is None:
        return None
    out, pos = [first[0]], first[1]
    for m in TS_RE.finditer(line, pos):
        hit = _strict_at(line, m.start())
        if hit is None:
            return None
        out.append(hit[0])
    return out


def strict_stamp(line: bytes) -> float | None:
    """The first stamp of a line if the whole line parses strictly
    (line_stamps), else None."""
    st = line_stamps(line)
    return None if st is None else st[0]


def marker_record(log: bytes, marker: str) -> tuple[int, float] | None:
    """(offset, stamp) of the last intact copy of ``marker``: a strictly
    parsed stamp, the exact marker straight after it, then the end of the
    line. No fuzzy matching anywhere. None if no copy is intact."""
    m = marker.encode()
    found, start = None, 0
    while True:
        i = log.find(m, start)
        if i < 0:
            return found
        start = i + 1
        j = log.rfind(b"[", max(0, i - 16), i)
        if j < 0 or log[i + len(m):i + len(m) + 1] not in (b"\n", b""):
            continue
        hit = _strict_at(log, j)
        if hit is not None and hit[1] == i:
            found = (j, hit[0])


def clean_tail(log: bytes, start: int, max_gap_s: float) -> tuple[list[float] | None, str]:
    """The stamps of every line from ``start`` (the marker record) to the
    end of the log, if the tail is clean; otherwise (None, why). Clean:
    every line parses strictly (line_stamps), the stamps never decrease,
    no two consecutive stamps are more than ``max_gap_s`` apart, the last
    END_K lines span at most END_WINDOW_S and the last FINAL_K lines at
    most FINAL_WINDOW_S."""
    lines = log[start:].split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()                     # the log's own final newline
    ts = []
    for n, line in enumerate(lines):
        t = line_stamps(line)
        if t is None:
            return None, f"line {n} after the marker record does not parse strictly: {line[:40]!r}"
        ts.extend(t)
    if len(ts) < END_K + 1:
        return None, f"only {len(ts) - 1} lines after the marker record (need {END_K})"
    for a, b in zip(ts, ts[1:]):
        if b < a:
            return None, f"stamps decrease after the marker ({a:.6f} then {b:.6f})"
        if b - a > max_gap_s:
            return None, f"{b - a:.2f} s between consecutive stamps ({a:.6f}, {b:.6f}) exceeds {max_gap_s:.2f} s"
    if ts[-1] - ts[-END_K] > END_WINDOW_S:
        return None, f"the last {END_K} lines span {ts[-1] - ts[-END_K]:.3f} s (> {END_WINDOW_S} s)"
    if ts[-1] - ts[-FINAL_K] > FINAL_WINDOW_S:
        return None, f"the last {FINAL_K} lines span {ts[-1] - ts[-FINAL_K]:.3f} s (> {FINAL_WINDOW_S} s)"
    return ts, ""


def max_gap(ts: list[float]) -> float:
    return max((b - a for a, b in zip(ts, ts[1:])), default=0.0)


def _same_us(a: float, b: float) -> bool:
    return round(a * 1e6) == round(b * 1e6)


def reset_anchor(pstore_log: bytes, marker: str, pair: dict,
                 tail_bound: tuple[float, float] | None,
                 max_gap_s: float) -> tuple[dict | None, str]:
    """Map the old kernel's reset onto the host's monotonic clock (A1,
    strict rule from code review round 3).

    ``pair`` ties the leaving marker's printk stamps (every copy, read back
    from live dmesg right after one verified write) to the host time of
    that write: {"marker", "printk_ts", "printk_ts_all", "host_mono",
    "half_width"}. printk and the marker share one clock.

    1. The marker record is the last intact copy of this exact marker in
       pstore (marker_record). No intact copy: UNVERIFIED.
    2. Its stamp must equal one of the live readback's stamps to the
       microsecond (the console and dmesg print the same record), so the
       pairing and the log are the same write. Otherwise UNVERIFIED.
    3. The tail from that record to the end must be clean (clean_tail).
       The reset is the stamp of the final line. A flip that leaves the
       final line inside FINAL_WINDOW_S of the lines before it can't be
       told apart, so the earliest host time also takes off the span of
       the last FINAL_K lines.
    4. With ``tail_bound`` (disconnect, disconnect + the largest calibrated
       shutdown tail) the anchor must fall inside it.

    Anything else: (None, why)."""
    if pair.get("marker") != marker:
        return None, f"the printk pairing is for {pair.get('marker')}, not this boot's marker {marker}"
    rec = marker_record(pstore_log, marker)
    if rec is None:
        return None, "no intact exact copy of the leaving marker in the previous kernel's log"
    off, pts = rec
    live = [float(x) for x in (pair.get("printk_ts_all") or [pair["printk_ts"]])]
    if not any(_same_us(pts, x) for x in live):
        return None, (f"the leaving marker's pstore stamp {pts:.6f} is not one of the live readback's "
                      f"stamps {', '.join(f'{x:.6f}' for x in live)}: not the same write")
    ts, why = clean_tail(pstore_log, off, max_gap_s)
    if ts is None:
        return None, f"tail after the marker is not clean: {why}"
    final = ts[-1]
    span = final - ts[-FINAL_K]
    hw = float(pair["half_width"])
    mid = float(pair["host_mono"]) + (final - pts)
    earliest, latest = mid - hw - span, mid + hw
    if tail_bound is not None:
        lo, hi = tail_bound
        if latest < lo or earliest > hi:
            return None, (f"reset at {mid - lo:+.2f} s from the USB disconnect is outside "
                          f"[0, {hi - lo:.2f}] s (the largest calibrated shutdown tail)")
    return {"printk_ts": final, "marker_ts": pts, "marker_ts_live": live, "host_mono": earliest,
            "half_width": hw, "final_span": round(span, 6), "max_gap": round(max_gap(ts), 6),
            "tail_lines": len(ts) - 1,
            "tail_s": None if tail_bound is None else round(earliest - tail_bound[0], 3)}, \
        f"reset at printk {final:.6f} s, {final - pts:.2f} s after the marker (pstore, {len(ts) - 1} clean lines)"


def marker_printk_stamps(dmesg: str, marker: str) -> list[float]:
    """The printk stamps of every copy of ``marker`` in live dmesg, in order."""
    out = []
    for line in dmesg.splitlines():
        if line.rstrip().endswith(marker):
            m = TS_RE.match(line.encode())
            if m:
                out.append(int(m.group(1)) + int(m.group(2)) / 1e6)
    return out


def chain_verdict(pstore_log: bytes | None, marker: str | None) -> tuple[str, str]:
    """Judge the kernel-log chain for one boot. PASS needs an exact copy of
    the marker (code review round 3); a copy found only with bit flips is
    UNVERIFIED, unless a new kernel session follows it (FAIL)."""
    if not marker:
        return UNVERIFIED, "no leaving marker was written before this boot"
    if pstore_log is None or len(pstore_log) == 0:
        return UNVERIFIED, "pstore console-ramoops missing or empty"
    exact = pstore_log.rfind(marker.encode())
    off = exact if exact >= 0 else find_marker(pstore_log, marker)
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
    if exact < 0:
        return UNVERIFIED, "the leaving marker is only in the log with bit flips (no exact copy)"
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
