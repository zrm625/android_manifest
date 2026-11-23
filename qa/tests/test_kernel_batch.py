"""Review rounds 4 and 5 (Opus 3, R5-6): batches that touch the kernel or
the boot image get at least 2 sysrq bench boots. Anything that can't be
compared counts as a kernel batch."""
import gzip
import struct
import unittest

import _path  # noqa: F401
from benchhome import bench_home
from riverqa import core, stage

K1, K2 = "a" * 40, "b" * 40
CMD = b"androidboot.hardware=qcom ramoops"
CFG = b"CONFIG_ARM64=y\nCONFIG_IKCONFIG=y\n"
SAME = {"device/motorola/river": "1" * 40, "device/motorola/sdm632-common": "2" * 40,
        "vendor/lineage": "3" * 40, ".repo/manifests": "4" * 40}


def kernel(cfg=CFG, stamp=b"1"):
    """A gzip kernel with an embedded config, then an appended dtb."""
    body = b"Image" + stamp + b"IKCFG_ST" + gzip.compress(cfg) + b"IKCFG_ED" + b"rest"
    return gzip.compress(body) + b"\xd0\x0d\xfe\xed-dtb"


def boot_img(path, cmdline=CMD, stamp=b"1", version=1, cfg=CFG):
    path.parent.mkdir(parents=True, exist_ok=True)
    k = kernel(cfg, stamp) if cfg is not None else b"Image" + stamp
    head = b"ANDROID!" + struct.pack("<9I", len(k), 0, 10, 0, 0, 0, 0, 2048, version) + b"\0" * 20
    head += cmdline.ljust(512, b"\0") + b"\0" * 32 + b"".ljust(1024, b"\0")
    path.write_bytes(head.ljust(2048, b"\0") + k)


def batch(name, kernel=K1, dtbo="d" * 64, **commits):
    cs = dict(SAME, **{stage.KERNEL_PATH: kernel})
    cs.update({k.replace("__", "/"): v for k, v in commits.items()})
    return {"batch": name, "commit_set": cs, "images": {"dtbo": {"sha256": dtbo}}}


def no_git(*a):
    raise AssertionError(f"git {a} called for equal commits")


class KernelChange(unittest.TestCase):
    def setup(self, home, mine, cmd_mine=CMD, version=1, kg_version=1, cfg_mine=CFG, cfg_kg=CFG):
        kg_batch = batch("kg")
        core.write_json(home / "batches/kg/batch.json", kg_batch)
        boot_img(home / "batches/kg/images/boot.img", stamp=b"old-build-date", version=kg_version, cfg=cfg_kg)
        boot_img(home / f"batches/{mine['batch']}/images/boot.img", cmdline=cmd_mine, stamp=b"new-build-date",
                 version=version, cfg=cfg_mine)
        return {"latest": {"batch": "kg", "commit_set": kg_batch["commit_set"]}}

    def test_same_kernel_is_one_sysrq_boot_even_though_the_boot_image_hash_differs(self):
        with bench_home() as h:
            kg = self.setup(h, batch("b1"))
            n, why = stage.sysrq_boots_required(batch("b1"), kg, git=no_git)
            self.assertEqual(n, 1, why)
            self.assertIn("IKCONFIG", why)

    def test_kernel_commit_dtbo_or_cmdline_change_is_two(self):
        with bench_home() as h:
            kg = self.setup(h, batch("b1"))
            n, why = stage.sysrq_boots_required(batch("b1", kernel=K2), kg)
            self.assertEqual(n, 2)
            self.assertIn("kernel/motorola/sdm632", why)
            self.assertEqual(stage.sysrq_boots_required(batch("b1", dtbo="e" * 64), kg, git=no_git)[0], 2)
        with bench_home() as h:
            kg = self.setup(h, batch("b1"), cmd_mine=CMD + b".mem_size=0")
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), kg, git=no_git)[0], 2)

    def test_unknown_counts_as_changed(self):
        with bench_home() as h:
            kg = self.setup(h, batch("b1"))
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), None)[0], 2)
            self.assertEqual(stage.sysrq_boots_required({"batch": "b1"}, kg)[0], 2)
            (h / "batches/b1/images/boot.img").write_bytes(b"not a boot image")
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), kg, git=no_git)[0], 2)
            gone = {"latest": {"batch": "missing", "commit_set": kg["latest"]["commit_set"]}}
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), gone, git=no_git)[0], 2)

    def test_device_repo_boot_inputs(self):
        """Opus minor 1: a BoardConfig change with the same kernel commit is a
        kernel batch; other device repo changes are not; a commit git can't
        compare counts as a change."""
        dev = "device/motorola/river"
        calls = []

        def git(repo, *args, out=""):
            calls.append((repo, args))
            return out
        with bench_home() as h:
            kg = self.setup(h, batch("b1"))
            b = batch("b1", device__motorola__river="9" * 40)
            n, why = stage.sysrq_boots_required(b, kg, git=lambda r, *a: git(r, *a, out="BoardConfig.mk\n"))
            self.assertEqual(n, 2)
            self.assertIn("device/motorola/river changed BoardConfig.mk", why)
            self.assertEqual(calls[0], (dev, ("diff", "--name-only", "1" * 40, "9" * 40, "--", "BoardConfig*.mk")))
            self.assertEqual(stage.sysrq_boots_required(b, kg, git=git)[0], 1)              # no boot input touched
            self.assertEqual(stage.sysrq_boots_required(b, kg, git=lambda *a: None)[0], 2)  # can't compare
            common = batch("b1", device__motorola__sdm632_common="8" * 40)
            common["commit_set"]["device/motorola/sdm632-common"] = "8" * 40
            got = stage.kernel_change(common, kg, git=lambda r, *a: "BoardConfigCommon.mk\n")
            self.assertTrue(got[0])
            lineage = dict(batch("b1"))
            lineage["commit_set"] = dict(lineage["commit_set"], **{"vendor/lineage": "7" * 40})
            self.assertTrue(stage.kernel_change(lineage, kg, git=lambda r, *a: "build/tasks/kernel.mk\n")[0])
            missing = batch("b1")
            del missing["commit_set"]["device/motorola/river"]
            self.assertTrue(stage.kernel_change(missing, kg, git=no_git)[0])

    def test_toolchain_pins(self):
        def xml(clang):
            return (f'<manifest><project path="prebuilts/clang/kernel/linux-x86/clang-r416183b" revision="{clang}"/>'
                    '<project path="packages/apps/Foo" revision="x"/></manifest>')

        def git_for(old, new):
            def git(repo, *args):
                self.assertEqual(repo, ".repo/manifests")
                return xml(old) if args[1].startswith("4" * 40) else xml(new)
            return git
        with bench_home() as h:
            kg = self.setup(h, batch("b1"))
            b = batch("b1")
            b["commit_set"][".repo/manifests"] = "5" * 40
            n, why = stage.sysrq_boots_required(b, kg, git=git_for("c1", "c2"))
            self.assertEqual(n, 2)
            self.assertIn("prebuilts/clang/kernel/linux-x86/clang-r416183b", why)
            self.assertEqual(stage.sysrq_boots_required(b, kg, git=git_for("c1", "c1"))[0], 1)
            self.assertEqual(stage.sysrq_boots_required(b, kg, git=lambda *a: None)[0], 2)

    def test_unknown_header_version_is_a_kernel_batch(self):
        """R5-17: header v3/v4 put the command line elsewhere; reading v0-v2
        offsets from them must not make two images look equal."""
        for v in (3, 4):
            with bench_home() as h:
                kg = self.setup(h, batch("b1"), version=v, kg_version=v)
                n, why = stage.sysrq_boots_required(batch("b1"), kg, git=no_git)
                self.assertEqual(n, 2, v)
                self.assertIn("unknown header version", why)
        with bench_home() as h:
            kg = self.setup(h, batch("b1"), version=2, kg_version=1)
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), kg, git=no_git)[0], 2)

    def test_embedded_kernel_config(self):
        with bench_home() as h:
            kg = self.setup(h, batch("b1"), cfg_mine=CFG + b"CONFIG_PSTORE=n\n")
            n, why = stage.sysrq_boots_required(batch("b1"), kg, git=no_git)
            self.assertEqual(n, 2)
            self.assertIn("IKCONFIG", why)
        with bench_home() as h:
            kg = self.setup(h, batch("b1"), cfg_mine=None)          # the batch dropped IKCONFIG
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), kg, git=no_git)[0], 2)
        with bench_home() as h:
            kg = self.setup(h, batch("b1"), cfg_mine=None, cfg_kg=None)
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), kg, git=no_git)[0], 1)


if __name__ == "__main__":
    unittest.main()
