"""flash --promote and flash --recover through the public command, with a
fake phone and stubbed gh / git ls-remote (C1, C2, A4). No adb."""
import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr
from unittest import mock

import _path  # noqa: F401
from benchhome import bench_home
from fakes import FakeShell
from riverqa import core, flashing, recovery, slots, stage

REPOS = {"build/make", "vendor/lineage", ".repo/manifests"}
COMMITS = {"build/make": "1" * 40, "vendor/lineage": "2" * 40, ".repo/manifests": "3" * 40}
# r0 and r1 have PRs (heads: the tested commits); r2, the manifest, has none.
SLUGS = {"build/make": "zrm625/r0", "vendor/lineage": "zrm625/r1", ".repo/manifests": "zrm625/r2"}
HEADS = {0: "1" * 40, 1: "2" * 40}
HASH = stage.commit_set_hash(COMMITS)
BID = f"b1-{HASH}"
BOOT = "c" * 64


def phone(slot="b", build_id=BID):
    dev = FakeShell({r"getprop ro.boot.slot_suffix": (0, f"_{slot}\n"), r"^id -u$": (0, "0\n"),
                     r"ro\.river\.build_id": (0, f"ro.river.build_id={build_id}\n"),
                     r"ro\.vendor\.river\.build_id": (0, f"ro.vendor.river.build_id={build_id}\n"),
                     r"sha256sum": (0, f"{BOOT}  /dev/block/by-name/boot_{slot}\n")})
    dev.guard = lambda: None
    return dev


def runner(merged, tagged):
    def run(cmd):
        if cmd[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(cmd, 0, "T\n", "")     # merges into an unmoved branch
        if cmd[0] == "gh":
            n = int(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"state": "MERGED" if merged[n] else "OPEN",
                                                                   "number": n, "url": f"u{n}",
                                                                   "headRefOid": HEADS[n],
                                                                   "baseRefName": "river-22.2",
                                                                   "mergeCommit": {"oid": f"m{n}"}}), "")
        n = int(cmd[3][-1])
        at = f"m{n}" if n < 2 else COMMITS[".repo/manifests"]
        out = f"{at}\trefs/tags/kg-1\n" if tagged[n] else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")
    return run


EXPECTED = {"build/make": {"repo": "zrm625/r0", "number": 0}, "vendor/lineage": {"repo": "zrm625/r1", "number": 1},
            ".repo/manifests": {"repo": "zrm625/r2", "number": None}}


def setup(home, kind="batch", build_id=BID, commit_set=None, started=True):
    st = slots.first_run("a", "f" * 64, "t0")
    st["baseline_closed"] = {"batch": "base", "build_id": None, "at": "t0", "seed": None, "seed_written": True}
    slots.reserve(st, "b1", kind, "f" * 64, "t1")
    flashing.save_state(st)
    b = {"batch": "b1", "kind": kind, "build_id": build_id, "vendor_build_id": build_id,
         "commit_set": commit_set or COMMITS, "commit_set_hash": HASH, "pair": dict(st["pair"]),
         "prs": [{"repo": "zrm625/r0", "number": 0}, {"repo": "zrm625/r1", "number": 1}],
         "no_pr": [".repo/manifests"], "tag": "kg-1",
         "tag_repos": ["https://github.com/zrm625/r0", "https://github.com/zrm625/r1",
                       "https://github.com/zrm625/r2"]}
    if started:
        b["merge_started"] = {"at": "t3", "expected_prs": EXPECTED}
    core.write_json(core.batch_dir("b1") / "batch.json", b)


def promote(merged, tagged, dev=None):
    err = io.StringIO()
    with mock.patch.object(flashing, "Device", lambda *a: dev or phone()), \
            mock.patch.object(recovery, "default_runner", runner(merged, tagged)), \
            mock.patch.object(stage, "expected_repos", lambda manifest=None: set(REPOS)), \
            mock.patch.object(stage, "repo_slugs", lambda manifest=None: dict(SLUGS)), redirect_stderr(err):
        rc = flashing.main(["--promote", "b1"])
    return rc, err.getvalue(), flashing.load_state()


class PromoteCommand(unittest.TestCase):
    def test_merged_without_a_frozen_pr_set_halts(self):
        with bench_home() as h:
            setup(h, started=False)
            rc, err, st = promote([True, True], [True, True, True])
        self.assertEqual(rc, flashing.EXIT_HALT)
        self.assertEqual(st["known_good_slot"], "a")
        self.assertIn("expected PR set", err)

    def test_pending_batch_keeps_the_fallback(self):
        with bench_home() as h:
            setup(h, started=False)
            rc, err, st = promote([False, False], [False, False, False])
        self.assertEqual(rc, flashing.EXIT_ERROR)
        self.assertEqual(st["known_good_slot"], "a")
        self.assertEqual(st["pair"]["batch"], "b1")
        self.assertIn("not proven merged and tagged", err)

    def test_partly_merged_batch_halts(self):
        with bench_home() as h:
            setup(h)
            rc, _, st = promote([True, False], [False, False, False])
            self.assertEqual(rc, flashing.EXIT_HALT)
            self.assertEqual(st["known_good_slot"], "a")
            rc, _, st = promote([True, True], [True, True, False])   # merged, one tag missing
            self.assertEqual(rc, flashing.EXIT_HALT)
            self.assertIsNotNone(st["pair"])

    def test_merged_and_tagged_batch_is_promoted(self):
        with bench_home() as h:
            setup(h)
            rc, err, st = promote([True, True], [True, True, True])
        self.assertEqual(rc, flashing.EXIT_OK, err)
        self.assertEqual(st["known_good_slot"], "b")
        self.assertIsNone(st["pair"])
        self.assertEqual(st["known_good"]["build_id"], BID)
        self.assertEqual(st["known_good"]["boot_sha256"], BOOT)

    def test_baseline_pair_is_never_promoted(self):
        with bench_home() as h:
            setup(h, kind="baseline")
            rc, err, st = promote([True, True], [True, True, True])
        self.assertEqual(rc, flashing.EXIT_ERROR)
        self.assertEqual(st["pair"]["kind"], "baseline")
        self.assertIn("phone-qa --baseline", err)

    def test_unstamped_or_untied_build_is_refused(self):
        for bid, cs in (("unstamped", None), ("b1-0000000000", None), (BID, dict(COMMITS, **{"build/make": "1" * 40 + "+dirty"}))):
            with bench_home() as h:
                setup(h, build_id=bid, commit_set=cs)
                rc, err, st = promote([True, True], [True, True], dev=phone(build_id=bid))
            self.assertEqual(rc, flashing.EXIT_ERROR, bid)
            self.assertEqual(st["known_good_slot"], "a")


class MergeStartedCommand(unittest.TestCase):
    def merge_started(self):
        with mock.patch.object(stage, "repo_slugs", lambda manifest=None: dict(SLUGS)), \
                redirect_stderr(io.StringIO()):
            return flashing.main(["--merge-started", "b1"])

    def batch(self):
        return core.read_json(core.batch_dir("b1") / "batch.json")

    def test_marker_is_written_once_and_kept(self):
        with bench_home() as h:
            setup(h, started=False)
            self.assertEqual(self.merge_started(), flashing.EXIT_OK)
            first = self.batch()["merge_started"]
            self.assertTrue(first["at"])
            self.assertEqual(first["expected_prs"], EXPECTED)          # the full PR set, frozen
            self.assertEqual(self.merge_started(), flashing.EXIT_OK)
            self.assertEqual(self.batch()["merge_started"], first)

    def test_incomplete_pr_set_is_refused(self):
        with bench_home() as h:
            setup(h, started=False)
            b = self.batch()
            b["no_pr"] = []                                            # the manifest has neither
            core.write_json(core.batch_dir("b1") / "batch.json", b)
            self.assertEqual(self.merge_started(), flashing.EXIT_ERROR)
            self.assertIsNone(self.batch().get("merge_started"))

    def test_pr_set_cannot_change_afterwards(self):
        with bench_home() as h:
            setup(h, started=False)
            self.assertEqual(self.merge_started(), flashing.EXIT_OK)
            b = self.batch()
            b["prs"], b["no_pr"] = b["prs"][:1], ["vendor/lineage", ".repo/manifests"]
            core.write_json(core.batch_dir("b1") / "batch.json", b)
            self.assertEqual(self.merge_started(), flashing.EXIT_ERROR)
            self.assertEqual(self.batch()["merge_started"]["expected_prs"], EXPECTED)

    def test_recover_with_marker_and_nothing_merged_halts(self):
        with bench_home() as h:
            setup(h)
            with mock.patch.object(stage, "repo_slugs", lambda manifest=None: dict(SLUGS)), \
                    redirect_stderr(io.StringIO()):
                rc = flashing.recover(runner=runner([False, False], [False, False, False]), dev=phone())
            self.assertEqual(rc, flashing.EXIT_HALT)
            self.assertEqual(flashing.load_state()["pair"]["batch"], "b1")


class Checkpoint(unittest.TestCase):
    def test_recover_finishes_an_interrupted_baseline_checkpoint(self):
        with bench_home():
            st = slots.first_run("a", "f" * 64, "t0")
            slots.reserve(st, "base", "baseline", "f" * 64, "t1")
            slots.close_baseline(st, "base", "id-1", BOOT, "t2", seed={"calibration_seed": {"median_mb": 900.0}})
            flashing.save_state(st)   # the host died here, before known-good.json
            self.assertTrue(slots.preflight(flashing.load_state(), "b1", "batch", "b"))
            with redirect_stderr(io.StringIO()):
                self.assertEqual(flashing.recover(dev=phone()), flashing.EXIT_OK)
            self.assertEqual(core.read_json(flashing.known_good_path())["calibration_seed"]["median_mb"], 900.0)
            self.assertEqual(slots.preflight(flashing.load_state(), "b1", "batch", "b"), [])


class Identity(unittest.TestCase):
    def batch(self, **kw):
        b = {"batch": "b1", "kind": "batch", "build_id": BID, "vendor_build_id": BID, "commit_set": dict(COMMITS),
             "commit_set_hash": HASH}
        b.update(kw)
        return b

    def errs(self, **kw):
        return stage.batch_identity_errors(self.batch(**kw), set(REPOS))

    def test_tied(self):
        self.assertEqual(self.errs(), [])

    def test_untied(self):
        self.assertTrue(self.errs(build_id="unstamped", vendor_build_id="unstamped"))
        self.assertTrue(self.errs(build_id="dev-1-" + HASH, vendor_build_id="dev-1-" + HASH))
        self.assertTrue(self.errs(vendor_build_id="other"))
        self.assertTrue(self.errs(commit_set={k: v for k, v in COMMITS.items() if k != "vendor/lineage"}))
        self.assertTrue(self.errs(commit_set=dict(COMMITS, **{".repo/manifests": "3" * 40 + "+dirty"})))
        self.assertTrue(self.errs(commit_set_hash="0" * 10))

    def test_expected_repos_come_from_default_xml(self):
        exp = stage.expected_repos()
        self.assertIn(".repo/manifests", exp)
        self.assertIn("device/motorola/river", exp)
        self.assertNotIn("frameworks/base", exp)

    def test_build_id_verdict(self):
        from riverqa.qa import build_id_verdict
        with mock.patch.object(stage, "expected_repos", lambda manifest=None: set(REPOS)):
            self.assertEqual(build_id_verdict(self.batch(), BID, BID)[0], "PASS")
            self.assertEqual(build_id_verdict(self.batch(), BID, "other")[0], "FAIL")
            self.assertEqual(build_id_verdict(self.batch(build_id="unstamped", vendor_build_id="unstamped"),
                                              "unstamped", "unstamped")[0], "FAIL")
        base = {"batch": "base", "kind": "baseline", "build_id": "unstamped", "vendor_build_id": "unstamped"}
        st, msg, d = build_id_verdict(base, "unstamped", "unstamped")
        self.assertEqual(st, "PASS")
        self.assertEqual(d["fallback"], "dates, slot and boot hash")
        self.assertEqual(build_id_verdict(base, "x-1", "x-1")[0], "FAIL")
        cal = dict(base, kind="calibration")
        self.assertEqual(build_id_verdict(cal, "unstamped", "unstamped")[0], "UNVERIFIED")

    def test_stage_refuses_an_untied_batch(self):
        import tempfile
        from pathlib import Path
        with bench_home() as h, tempfile.TemporaryDirectory() as t:
            prod = Path(t) / "out/target/product/river"
            (prod / "system").mkdir(parents=True)
            (prod / "vendor").mkdir()
            (prod / "system/build.prop").write_text("ro.river.build_id=dev-1-x\n")
            (prod / "vendor/build.prop").write_text("ro.vendor.river.build_id=dev-1-x\n")
            core.write_json(Path(t) / "out/river-build.json", {"build_id": "dev-1-x", "commit_set": COMMITS,
                                                               "commit_set_hash": HASH})
            with self.assertRaises(SystemExit) as cm:
                stage.stage("b1", prod, "batch", False, expected=set(REPOS))
            self.assertIn("not tied", str(cm.exception))
            self.assertFalse((h / "batches/b1").exists())


if __name__ == "__main__":
    unittest.main()
