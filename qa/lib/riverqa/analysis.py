"""Host-side signal analysis: audio bursts and video PSNR. Pure functions."""

from __future__ import annotations

import array
import math


def pcm16(data: bytes) -> list[int]:
    a = array.array("h")
    a.frombytes(data[: len(data) // 2 * 2])
    return a.tolist()


def goertzel_power(x: list[int], start: int, n: int, freq: float, rate: int) -> float:
    """Power at ``freq`` over x[start:start+n], normalised so a full-scale
    sine gives about 0.25 (Hann window)."""
    seg = x[max(0, start): max(0, start) + n]
    n = len(seg)
    if n < 16:
        return 0.0
    k = 2.0 * math.cos(2.0 * math.pi * freq / rate)
    s1 = s2 = 0.0
    for i, v in enumerate(seg):
        w = 0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1))
        s0 = v / 32768.0 * w + k * s1 - s2
        s2, s1 = s1, s0
    p = s1 * s1 + s2 * s2 - k * s1 * s2
    return p / (n * n / 4.0) / 4.0 * 4.0


def db(p: float) -> float:
    return 10.0 * math.log10(max(p, 1e-15))


def rms_dbfs(x: list[int], start: int, n: int) -> float:
    seg = x[max(0, start): max(0, start) + n]
    if not seg:
        return -150.0
    ms = sum((v / 32768.0) ** 2 for v in seg) / len(seg)
    return db(ms)


BURSTS = [(1000, 0, 400), (0, 400, 700), (2500, 700, 1100), (0, 1100, 1400), (1000, 1400, 1800)]  # ms


def find_onset(x: list[int], rate: int, play_at: int, silence_db: float) -> int | None:
    """Start of the first 1 kHz burst: the first 10 ms window after play_at
    within 6 dB of the loudest 1 kHz window in the next second. Output
    latency on river is about 190 ms. None if the tone never rises 15 dB
    above silence."""
    step = rate // 100
    powers = []
    for start in range(play_at, min(len(x) - step, play_at + rate), step):
        powers.append((start, db(goertzel_power(x, start, step, 1000, rate))))
    if not powers:
        return None
    peak = max(p for _, p in powers)
    if peak < silence_db + 15:
        return None
    for start, p in powers:
        if p >= peak - 6:
            return start
    return None


def analyse_audio(x: list[int], rate: int, play_at: int) -> dict:
    """Measurements for the audio pass rule."""
    out: dict = {"samples": len(x), "all_zero": not any(x)}
    sil_start, sil_n = int(rate * 0.1), int(rate * 0.8)
    sil = {f: db(goertzel_power(x, sil_start, sil_n, f, rate)) for f in (1000, 2500)}
    out["silence_db"] = {str(k): round(v, 1) for k, v in sil.items()}
    onset = find_onset(x, rate, play_at, sil[1000])
    out["onset"] = onset
    if onset is None:
        return out
    ms = rate // 1000
    bursts = []
    for idx, (f, a, b) in enumerate(BURSTS):
        if f == 0:
            continue
        st, n = onset + (a + 50) * ms, (b - a - 100) * ms
        p = db(goertzel_power(x, st, n, f, rate))
        lo = db(goertzel_power(x, st, n, f - 150, rate))
        hi = db(goertzel_power(x, st, n, f + 150, rate))
        gaps = []
        for gi in (idx - 1, idx + 1):
            if 0 <= gi < len(BURSTS) and BURSTS[gi][0] == 0:
                ga, gb = BURSTS[gi][1], BURSTS[gi][2]
                gaps.append(db(goertzel_power(x, onset + (ga + 50) * ms, (gb - ga - 100) * ms, f, rate)))
        bursts.append({"freq": f, "db": round(p, 1), "over_silence": round(p - sil[f], 1),
                       "over_neighbours": round(p - max(lo, hi), 1),
                       "gap_drop": round(p - max(gaps), 1) if gaps else None,
                       "level_dbfs": round(rms_dbfs(x, st, n), 1)})
    out["bursts"] = bursts
    return out


def audio_verdict(m: dict, min_db: float, gap_db: float, level_floor_dbfs: float) -> tuple[bool, str]:
    if m.get("all_zero"):
        return False, "recording is all zeros"
    if m.get("onset") is None:
        return False, "no 1 kHz tone found after playback started"
    bad = []
    for b in m["bursts"]:
        if b["over_silence"] < min_db:
            bad.append(f"{b['freq']} Hz only {b['over_silence']} dB over silence")
        if b["over_neighbours"] < min_db:
            bad.append(f"{b['freq']} Hz only {b['over_neighbours']} dB over +-150 Hz")
        if b["gap_drop"] is not None and b["gap_drop"] < gap_db:
            bad.append(f"{b['freq']} Hz gap drops only {b['gap_drop']} dB")
        if b["level_dbfs"] < level_floor_dbfs:
            bad.append(f"{b['freq']} Hz level {b['level_dbfs']} dBFS below floor {level_floor_dbfs}")
    if bad:
        return False, "; ".join(bad)
    return True, "; ".join(f"{b['freq']} Hz +{b['over_silence']} dB" for b in m["bursts"])


def psnr(a: bytes, b: bytes) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    se = 0
    for x, y in zip(a, b):
        d = x - y
        se += d * d
    mse = se / len(a)
    return 99.0 if mse == 0 else 10.0 * math.log10(255.0 * 255.0 / mse)
