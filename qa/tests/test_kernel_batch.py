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


DTB = b"/dts-v1/ river"


def fdt(payload=DTB):
    """A flattened device tree blob: magic, totalsize, the rest of a
    40-byte header, then the payload."""
    return b"\xd0\x0d\xfe\xed" + struct.pack(">I", 40 + len(payload)) + b"\0" * 32 + payload


def kernel(cfg=CFG, stamp=b"1", dtb=None, ikcfg=None):
    """A gzip kernel with an embedded config (none if cfg is None; the raw
    block ``ikcfg`` if given), then appended dtbs (one fdt() by default)."""
    block = ikcfg if ikcfg is not None else b"" if cfg is None else b"IKCFG_ST" + gzip.compress(cfg) + b"IKCFG_ED"
    body = b"Image" + stamp + block + b"rest"
    return gzip.compress(body) + (fdt() if dtb is None else dtb)


def boot_img(path, cmdline=CMD, stamp=b"1", version=1, cfg=CFG, k=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    k = k if k is not None else kernel(cfg, stamp)
    head = b"ANDROID!" + struct.pack("<9I", len(k), 0, 10, 0, 0, 0, 0, 2048, version) + b"\0" * 20
    head += cmdline.ljust(512, b"\0") + b"\0" * 32 + b"".ljust(1024, b"\0")
    path.write_bytes(head.ljust(2048, b"\0") + k)


def batch(name, kernel=K1, dtbo="d" * 64, **commits):
    cs = dict(SAME, **{stage.KERNEL_PATH: kernel})
    cs.update({k.replace("__", "/"): v for k, v in commits.items()})
    return {"batch": name, "commit_set": cs, "images": {"dtbo": {"sha256": dtbo}}}


def no_git(*a):
    raise AssertionError(f"git {a} called for equal commits")


def stage_pair(home, mine, cmd_mine=CMD, version=1, kg_version=1, cfg_mine=CFG, cfg_kg=CFG, k_mine=None,
               k_kg=None):
    """A known-good batch "kg" and ``mine``, with boot images; returns
    known-good.json's content."""
    kg_batch = batch("kg")
    core.write_json(home / "batches/kg/batch.json", kg_batch)
    boot_img(home / "batches/kg/images/boot.img", stamp=b"old-build-date", version=kg_version, cfg=cfg_kg, k=k_kg)
    boot_img(home / f"batches/{mine['batch']}/images/boot.img", cmdline=cmd_mine, stamp=b"new-build-date",
             version=version, cfg=cfg_mine, k=k_mine)
    return {"latest": {"batch": "kg", "commit_set": kg_batch["commit_set"]}}


class KernelChange(unittest.TestCase):
    def setup(self, home, mine, **kw):
        return stage_pair(home, mine, **kw)

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
            n, why = stage.sysrq_boots_required(batch("b1"), kg, git=no_git)
            self.assertEqual(n, 1, why)
            self.assertIn("IKCONFIG confirmed absent in both", why)

    def test_unparsable_kernel_config_is_a_kernel_batch(self):
        """R6-3: an IKCONFIG block that can't be read in both images is not
        "absent in both": the old code compared None with None."""
        broken = b"IKCFG_ST" + b"\x1f\x8b not gzip" + b"IKCFG_ED"
        for bad in (broken, b"IKCFG_ST" + gzip.compress(CFG)):         # unreadable; no IKCFG_ED after it
            with self.subTest(bad=bad[:20]), bench_home() as h:
                kg = self.setup(h, batch("b1"), k_mine=kernel(ikcfg=bad, stamp=b"new"),
                                k_kg=kernel(ikcfg=bad, stamp=b"old"))
                n, why = stage.sysrq_boots_required(batch("b1"), kg, git=no_git)
                self.assertEqual(n, 2, why)
                self.assertIn("could not be parsed", why)

    def test_corrupt_gzip_kernel_is_a_kernel_batch(self):
        with bench_home() as h:
            cut = kernel(stamp=b"x" * 5000)
            k = cut[:len(cut) // 2]
            kg = self.setup(h, batch("b1"), k_mine=k, k_kg=k)
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), kg, git=no_git)[0], 2)

    def test_appended_dtb(self):
        """R6-3: the DTB appended to Image.gz-dtb is compared; one that
        isn't whole FDT blobs can't be parsed and counts as changed."""
        with bench_home() as h:
            kg = self.setup(h, batch("b1"), k_mine=kernel(stamp=b"new", dtb=fdt(DTB + b" changed")),
                            k_kg=kernel(stamp=b"old"))
            n, why = stage.sysrq_boots_required(batch("b1"), kg, git=no_git)
            self.assertEqual(n, 2, why)
            self.assertIn("DTB appended", why)
        for tail in (b"\xd0\x0d\xfe\xed-dtb", fdt() + b"junk", fdt()[:30]):
            with self.subTest(tail=tail[:12]), bench_home() as h:
                kg = self.setup(h, batch("b1"), k_mine=kernel(stamp=b"new", dtb=tail),
                                k_kg=kernel(stamp=b"old", dtb=tail))
                n, why = stage.sysrq_boots_required(batch("b1"), kg, git=no_git)
                self.assertEqual(n, 2, why)
                self.assertIn("could not be parsed", why)
        with bench_home() as h:      # two dtbs back to back, same in both: not a kernel batch
            two = fdt() + fdt(b"second")
            kg = self.setup(h, batch("b1"), k_mine=kernel(stamp=b"new", dtb=two), k_kg=kernel(stamp=b"old", dtb=two))
            self.assertEqual(stage.sysrq_boots_required(batch("b1"), kg, git=no_git)[0], 1)

    def test_raw_kernel_dtb_cannot_be_compared(self):
        """A raw (not gzip) kernel's end can't be told, so neither can its
        appended DTB: a kernel batch."""
        with bench_home() as h:
            k = b"Image" + b"IKCFG_ST" + gzip.compress(CFG) + b"IKCFG_ED" + fdt()
            kg = self.setup(h, batch("b1"), k_mine=k, k_kg=k)
            n, why = stage.sysrq_boots_required(batch("b1"), kg, git=no_git)
            self.assertEqual(n, 2, why)

    def test_boot_image_shorter_than_its_kernel(self):
        """R6-6: page_size + kernel_size past the end of the file."""
        with bench_home() as h:
            p = h / "short.img"
            boot_img(p)
            p.write_bytes(p.read_bytes()[:-10])
            self.assertIsNone(stage.boot_info(p))

    def test_ikcfg_end_found_after_the_start(self):
        """R6-6: an IKCFG_ED string earlier in the image doesn't hide the
        config block."""
        img = b"IKCFG_ED earlier" + b"IKCFG_ST" + gzip.compress(CFG) + b"IKCFG_ED"
        import hashlib
        self.assertEqual(stage.ikconfig_sha(img), hashlib.sha256(CFG).hexdigest())


class ToolchainPins(unittest.TestCase):
    """R6-3: the kernel toolchain includes external/dtc (DTC_EXT),
    prebuilts/clang-tools and prebuilts/rust (BoardConfigKernel.mk), and
    prebuilts/tools-lineage is matched."""

    def test_new_pins_change_the_toolchain(self):
        def xml(path, rev):
            return (f'<manifest><project path="{path}" revision="{rev}"/>'
                    '<project path="packages/apps/Foo" revision="x"/></manifest>')
        for path in ("external/dtc", "prebuilts/clang-tools", "prebuilts/rust", "prebuilts/tools-lineage"):
            with self.subTest(path=path), bench_home() as h:
                kg = stage_pair(h, batch("b1"))
                b = batch("b1")
                b["commit_set"][".repo/manifests"] = "5" * 40

                def git(repo, *args, path=path):
                    return xml(path, "old") if args[1].startswith("4" * 40) else xml(path, "new")
                n, why = stage.sysrq_boots_required(b, kg, git=git)
                self.assertEqual(n, 2, why)
                self.assertIn(path, why)

    def test_real_manifest_pins(self):
        from pathlib import Path
        xml = (Path(__file__).resolve().parents[2] / "default.xml").read_text()
        pins = stage.toolchain_pins(xml)
        for path in ("external/dtc", "prebuilts/clang-tools", "prebuilts/rust", "prebuilts/tools-lineage",
                     "prebuilts/kernel-build-tools", "prebuilts/clang/kernel/linux-x86/clang-r416183b"):
            self.assertIn(path, pins)


if __name__ == "__main__":
    unittest.main()
