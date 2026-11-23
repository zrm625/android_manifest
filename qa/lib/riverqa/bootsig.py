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
   reset (amendment A1′, reset_anchor: a marker copy proven by its stamp
   and the log's final lines, otherwise UNVERIFIED), because river's USB
   gadget drops seconds
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


def find_marker(log: bytes, marker: str, max_flips: int = MAX_MARKER_FLIPS) -> int | None:
    """Offset of the last copy of ``marker`` in ``log``: the last exact copy
    if there is one, else the last copy within ``max_flips`` wrong
    characters. None if absent."""
    exact = log.rfind(marker.encode())
    if exact >= 0:
        return exact
    offs = marker_offsets(log, marker, max_flips)
    return offs[-1] if offs else None


TS_RE = re.compile(rb"\[\s*(\d{1,6})\.(\d{6})\]")
EARLY_END_S = 30.0


def stamps(log: bytes) -> list[float]:
    return [int(a) + int(b) / 1e6 for a, b in TS_RE.findall(log)]


def corroborated_last(ts: list[float], run: int = 3, window_s: float = 1.0) -> float | None:
    """The earliest stamp of the last run of ``run`` consecutive
    non-decreasing stamps spanning at most ``window_s``. Used only by the
    chain to spot a log that ends seconds into a boot (a FAIL signal); the
    A1′ reset anchor uses final_lines instead."""
    for i in range(len(ts) - 1, run - 2, -1):
        seq = ts[i - run + 1:i + 1]
        if all(x <= y for x, y in zip(seq, seq[1:])) and seq[-1] - seq[0] <= window_s:
            return seq[0]
    return None


def last_timestamp(log: bytes) -> float | None:
    """A timestamp near the end of the previous kernel's log (see
    corroborated_last), or None. Chain only."""
    return corroborated_last(stamps(log[-8192:]))


# --- amendment A1′: the reset anchor (phone data, 2026-11-23) ---------------
#
# ramoops here has no ECC, so every copy of the marker can arrive damaged.
# A copy is found tolerantly (rule 1) but only counts if the strict stamp
# next to it equals, to the microsecond, a stamp read back live for this
# attempt's write (rule 2): a different write, a nonce off by a character or
# a damaged stamp can't do that. The reset comes only from the log's final
# strictly parsed lines (rule 3). Anything else is UNVERIFIED.

STRICT_STAMP_RE = re.compile(rb"\[( *)(\d{1,6})\.(\d{6})\] ")
END_K = 5               # the last END_K stamps must agree within END_WINDOW_S
END_WINDOW_S = 1.5      # river's last 5 shutdown lines span 1.12 s ("SMP: failed to stop" waits 1.1 s)
FINAL_K = 3             # the last FINAL_K stamps must agree within FINAL_WINDOW_S
FINAL_WINDOW_S = 0.05   # river's last 3 lines ("Going down for restart now", ...) span under 1 ms
TAIL_MARGIN_S = 1.0     # max_shutdown_tail = the largest first-after-wipe tail + this (A1′ rule 4)
MIN_TAIL_SAMPLES = 3    # first-adb-reboot-after-wipe tails needed before the bound exists


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


def live_stamps(pair: dict) -> list[float]:
    """The stamps read back from live dmesg for this attempt's write."""
    return [float(x) for x in (pair.get("printk_ts_all") or [pair["printk_ts"]])]


def proven_marker(log: bytes, marker: str, live: list[float]) -> tuple[tuple[int, float] | None, str]:
    """((offset, stamp), note) of the last copy of ``marker`` proven by its
    stamp (A1′ rules 1-2): a copy within MAX_MARKER_FLIPS wrong characters
    whose strict stamp (stamp_before) equals one of ``live`` to the
    microsecond. Everything else about the marker comes from this one copy.
    (None, why) if no copy is proven."""
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


def final_lines(log: bytes, start: int) -> tuple[list[float] | None, str]:
    """The last END_K strictly parsed stamps of the log after ``start`` (the
    end of the proven marker copy), if they give a reset (A1′ rule 3);
    otherwise (None, why). Lines that don't parse strictly are skipped, so
    damage before the final lines can't move the estimate. The rest of the
    marker's own line is skipped too. The last END_K must not decrease, must
    span at most END_WINDOW_S, and the last FINAL_K at most FINAL_WINDOW_S."""
    segs = log[start:].split(b"\n")[1:]
    ts: list[float] = []
    for seg in segs:
        st = line_stamps(seg)
        if st is not None:
            ts.extend(st)
    if len(ts) < END_K:
        return None, f"only {len(ts)} strictly parsed stamps after the leaving marker (need {END_K})"
    last = ts[-END_K:]
    for a, b in zip(last, last[1:]):
        if b < a:
            return None, f"the final stamps decrease ({a:.6f} then {b:.6f})"
    if last[-1] - last[0] > END_WINDOW_S:
        return None, f"the last {END_K} stamps span {last[-1] - last[0]:.3f} s (> {END_WINDOW_S} s)"
    if last[-1] - last[-FINAL_K] > FINAL_WINDOW_S:
        return None, f"the last {FINAL_K} stamps span {last[-1] - last[-FINAL_K]:.3f} s (> {FINAL_WINDOW_S} s)"
    return last, ""


def check_tail_bound(anchor: dict, tail_bound: tuple[float, float]) -> str:
    """"" if the anchor's host interval meets ``tail_bound`` (disconnect,
    disconnect + max_shutdown_tail), else why not."""
    lo, hi = tail_bound
    hw, span = anchor["half_width"], anchor["final_span"]
    earliest = anchor["host_mono"]          # mid - hw - span
    mid = earliest + hw + span
    latest = mid + hw
    if latest < lo or earliest > hi:
        return (f"reset at {mid - lo:+.2f} s from the USB disconnect is outside "
                f"[0, {hi - lo:.2f}] s (the calibrated shutdown-tail bound)")
    return ""


def reset_anchor(pstore_log: bytes, marker: str, pair: dict,
                 tail_bound: tuple[float, float] | None = None) -> tuple[dict | None, str]:
    """Map the old kernel's reset onto the host's monotonic clock
    (amendment A1′).

    ``pair`` ties the leaving marker's printk stamps (every copy, read back
    from live dmesg right after one verified write) to the host time of
    that write: {"marker", "printk_ts", "printk_ts_all", "host_mono",
    "half_width"}. printk and the marker share one clock.

    1-2. The marker record is the last copy proven by its stamp
         (proven_marker). Its pstore stamp, its live pairing and its place
         in the log all come from that one copy. None proven: UNVERIFIED.
    3. The reset comes from the log's final lines after that copy
       (final_lines), and they must not be earlier than the marker. The
       reset is the final stamp; the earliest host time also takes off the
       span of the last FINAL_K stamps, so a flip inside FINAL_WINDOW_S
       can't move it later.
    4. With ``tail_bound`` (disconnect, disconnect + max_shutdown_tail) the
       anchor must fall inside it (check_tail_bound).

    Anything else: (None, why)."""
    if pair.get("marker") != marker:
        return None, f"the printk pairing is for {pair.get('marker')}, not this boot's marker {marker}"
    live = live_stamps(pair)
    rec, why = proven_marker(pstore_log, marker, live)
    if rec is None:
        return None, why
    off, pts = rec
    last, why = final_lines(pstore_log, off + len(marker.encode()))
    if last is None:
        return None, f"the log's final lines give no reset: {why}"
    if last[0] < pts:
        return None, f"the final stamps start at {last[0]:.6f}, before the marker's {pts:.6f}"
    final = last[-1]
    span = final - last[-FINAL_K]
    hw = float(pair["half_width"])
    mid = float(pair["host_mono"]) + (final - pts)
    anchor = {"printk_ts": final, "marker_ts": pts, "marker_offset": off, "marker_ts_live": live,
              "host_mono": mid - hw - span, "half_width": hw, "final_span": round(span, 6),
              "final_stamps": last}
    if tail_bound is not None:
        why = check_tail_bound(anchor, tail_bound)
        if why:
            return None, why
        anchor["tail_s"] = round(anchor["host_mono"] - tail_bound[0], 3)
    return anchor, f"reset at printk {final:.6f} s, {final - pts:.2f} s after the marker (pstore, copy proven by its stamp)"


def tail_bound_from(samples: list[float]) -> float | None:
    """max_shutdown_tail (A1′ rule 4): the largest of at least
    MIN_TAIL_SAMPLES first-adb-reboot-after-wipe tails plus TAIL_MARGIN_S,
    or None with fewer samples."""
    if len(samples) < MIN_TAIL_SAMPLES:
        return None
    return round(max(samples) + TAIL_MARGIN_S, 3)


def marker_printk_stamps(dmesg: str, marker: str) -> list[float]:
    """The printk stamps of every copy of ``marker`` in live dmesg, in order."""
    out = []
    for line in dmesg.splitlines():
        if line.rstrip().endswith(marker):
            m = TS_RE.match(line.encode())
            if m:
                out.append(int(m.group(1)) + int(m.group(2)) / 1e6)
    return out


def chain_verdict(pstore_log: bytes | None, marker: str | None, pair: dict | None = None) -> tuple[str, str]:
    """Judge the kernel-log chain for one boot. PASS needs an exact copy of
    the marker, or (amendment A1′) a copy proven by its stamp against
    ``pair``'s live readback. A copy found only with bit flips and not
    proven is UNVERIFIED, unless a new kernel session follows it (FAIL)."""
    if not marker:
        return UNVERIFIED, "no leaving marker was written before this boot"
    if pstore_log is None or len(pstore_log) == 0:
        return UNVERIFIED, "pstore console-ramoops missing or empty"
    exact = pstore_log.rfind(marker.encode())
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
    proven = None
    if exact < 0 and pair and pair.get("marker") == marker:
        proven, _why = proven_marker(pstore_log, marker, live_stamps(pair))
        if proven is not None:
            off = proven[0]
    if BANNER_RE.search(pstore_log[off:]):
        return FAIL, "a new kernel session started after the leaving marker"
    if exact < 0 and proven is None:
        return UNVERIFIED, "the leaving marker is only in the log with bit flips, and no copy is proven by its stamp"
    if exact < 0:
        return PASS, "previous kernel log ends with the leaving marker (a damaged copy proven by its stamp)"
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
