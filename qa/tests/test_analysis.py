import math
import unittest

import _path  # noqa: F401
from riverqa import analysis as an


def tone_recording(rate=48000, play_at=48000, latency=0.12, amp=0.3, noise=0.001, gap_level=0.0):
    n = rate + int(rate * 1.8) + rate // 2 + int(latency * rate) + rate // 10
    x = [0.0] * n
    import random
    rnd = random.Random(1)
    for i in range(n):
        x[i] = rnd.uniform(-noise, noise)
    start = play_at + int(latency * rate)
    for f, a, b in an.BURSTS:
        for i in range(int(a * rate / 1000), int(b * rate / 1000)):
            j = start + i
            if f:
                x[j] += amp * math.sin(2 * math.pi * f * i / rate)
            else:
                x[j] += gap_level * math.sin(2 * math.pi * 1000 * i / rate)
    return [max(-32768, min(32767, int(v * 32767))) for v in x]


class Audio(unittest.TestCase):
    def test_clean_tones_pass(self):
        x = tone_recording()
        m = an.analyse_audio(x, 48000, 48000)
        ok, msg = an.audio_verdict(m, 15, 10, -70)
        self.assertTrue(ok, (msg, m))

    def test_silence_fails(self):
        x = tone_recording(amp=0.0)
        ok, msg = an.audio_verdict(an.analyse_audio(x, 48000, 48000), 15, 10, -70)
        self.assertFalse(ok)

    def test_all_zero(self):
        ok, msg = an.audio_verdict(an.analyse_audio([0] * 150000, 48000, 48000), 15, 10, -70)
        self.assertFalse(ok)
        self.assertIn("zeros", msg)


class Psnr(unittest.TestCase):
    def test_psnr(self):
        a = bytes(range(256)) * 10
        self.assertEqual(an.psnr(a, a), 99.0)
        b = bytes((v + 3) % 256 for v in a)
        self.assertLess(an.psnr(a, b), 30)
        self.assertEqual(an.psnr(a, a[:-1]), 0.0)


if __name__ == "__main__":
    unittest.main()
