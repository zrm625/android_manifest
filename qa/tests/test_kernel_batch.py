"""Review round 4, item 7 (Opus 3): batches that touch the kernel or the
boot image get at least 2 sysrq bench boots."""
import struct
import unittest

import _path  # noqa: F401
from benchhome import bench_home
from riverqa import core, stage

K1, K2 = "a" * 40, "b" * 40


def boot_img(path, cmdline=b"androidboot.hardware=qcom ramoops", stamp=b"1"):
    path.parent.mkdir(parents=True, exist_ok=True)
    head = b"ANDROID!" + struct.pack("<9I", 10, 0, 10, 0, 0, 0, 0, 2048, 1) + b"\0" * 20
    head += cmdline.ljust(512, b"\0") + b"\0" * 32 + b"".ljust(1024, b"\0")
    path.write_bytes(head.ljust(2048, b"\0") + b"kernel" + stamp)


def batch(name, kernel=K1, dtbo="d" * 64):
    return {"batch": name, "commit_set": {stage.KERNEL_PATH: kernel}, "images": {"dtbo": {"sha256": dtbo}}}


class KernelChange(unittest.TestCase):
    def setup(self, home, mine, cmd_mine=b"androidboot.hardware=qcom ramoops"):
        kg_batch = batch("kg")
        core.write_json(home / "batches/kg/batch.json", kg_batch)
        boot_img(home / "batches/kg/images/boot.img", stamp=b"old-build-date")
        boot_img(home / f"batches/{mine['batch']}/images/boot.img", cmdline=cmd_mine, stamp=b"new-build-date")
        return {"latest": {"batch": "kg", "commit_set": {stage.KERNEL_PATH: K1}}}

    def test_same_kernel_is_one_sysrq_boot_even_though_the_boot_image_hash_differs(self):
        with bench_home() as h:
            kg = self.setup(h, batch("b1"))
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), kg)[0], 1)

    def test_kernel_commit_dtbo_or_cmdline_change_is_two(self):
        with bench_home() as h:
            kg = self.setup(h, batch("b1"))
            n, why = stage.sysrq_boots_required(batch("b1", kernel=K2), kg)
            self.assertEqual(n, 2)
            self.assertIn("kernel/motorola/sdm632", why)
            self.assertEqual(stage.sysrq_boots_required(batch("b1", dtbo="e" * 64), kg)[0], 2)
        with bench_home() as h:
            kg = self.setup(h, batch("b1"), cmd_mine=b"androidboot.hardware=qcom ramoops.mem_size=0")
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), kg)[0], 2)

    def test_unknown_counts_as_changed(self):
        with bench_home() as h:
            kg = self.setup(h, batch("b1"))
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), None)[0], 2)
            self.assertEqual(stage.sysrq_boots_required({"batch": "b1"}, kg)[0], 2)
            (h / "batches/b1/images/boot.img").write_bytes(b"not a boot image")
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), kg)[0], 2)
            gone = {"latest": {"batch": "missing", "commit_set": {stage.KERNEL_PATH: K1}}}
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), gone)[0], 2)


if __name__ == "__main__":
    unittest.main()
