"""stage and qualify refusals (R2.4, I20), stage's finish transaction (I6),
the camera backstop only for production kinds (I10), part 1's measurement
and comparison (R14.2, I8), and the runner version hashes (R2.2)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
from batchfakes import mk_ctx, write_json
from benchhome import bench_home
from riverbatch import abtest, merge, phases, qualify, version
from riverbatch import ctx as C
from riverbatch import outcomes as O
from riverbatch import state as S

FULL = {p: "b" * 40 for p in merge.slugs()}


def kg_files(h, pair=None, slot_batch="kg", kg_batch="kg"):
    write_json(h / "slot-state.json", {"known_good_slot": "a", "known_good": {"batch": slot_batch}, "pair": pair})
    write_json(h / "qa/known-good.json", {"latest": {"batch": kg_batch, "cameras": [0, 1, 2]}})


class Refusals(unittest.TestCase):
    def test_halt_is_3(self):
        with bench_home():
            with S.transaction() as st:
                S.set_halt(st, "x")
            with self.assertRaises(qualify.HaltedState):
                qualify.refusals(mk_ctx(), "qualify")

    def test_running_batch_or_merge_started(self):
        with bench_home() as h:
            kg_files(h)
            with S.transaction() as st:
                st["running"] = {"id": "b0001", "purpose": "batch"}
            with self.assertRaises(S.Refused):
                qualify.refusals(mk_ctx(), "qualify")
            with S.transaction() as st:
                st["running"] = {"id": "q0001", "purpose": "qualify"}
            write_json(h / "batches/q0001/batch.json", {"merge_started": {"at": "t"}})
            with self.assertRaises(S.Refused):
                qualify.refusals(mk_ctx(), "qualify")

    def test_interrupted_qualify_run_with_a_pair(self):
        with bench_home() as h:
            kg_files(h, pair={"batch": "q0001", "kind": "batch"})
            with S.transaction() as st:
                st["running"] = {"id": "q0001", "purpose": "qualify"}
            qualify.refusals(mk_ctx(), "qualify")                   # qualify may abort it
            with self.assertRaises(S.Refused) as cm:
                qualify.refusals(mk_ctx(), "stage")                 # stage does no phone work
            self.assertIn("still holds a slot pair", str(cm.exception))

    def test_i20_live_blockers_refuse_even_under_a_pause(self):
        cases = [
            (dict(pair={"batch": "cal-1", "kind": "calibration"}), None, "phone in use by cal-1"),
            (dict(kg_batch="other"), None, "known-good records disagree"),
            ({}, "strike", "strike limit"),
            ({}, "lease", "tree lease"),
        ]
        for kw, extra, want in cases:
            with bench_home() as h:
                kg_files(h, **kw)
                with S.transaction() as st:
                    S.set_pause(st, "two voids in a row")
                    if extra == "strike":
                        st["strikes"]["setup"] = 2
                    if extra == "lease":
                        st["lease"] = {"batch": "b0004", "expect": {}, "base": {}}
                for cmd in ("qualify", "stage"):
                    with self.assertRaises(S.Refused) as cm:
                        qualify.refusals(mk_ctx(), cmd)
                    self.assertIn(want, str(cm.exception))

    def test_stage_id_rules(self):
        with bench_home():
            for bad, why in (("b0007", "runner ID"), ("q0001", "runner ID"), ("..", "bad batch ID"),
                             (".", "bad batch ID"), (".hidden", "bad batch ID")):
                with self.assertRaises(S.Refused) as cm:
                    qualify.stage_cmd(mk_ctx(), "calibration", bad, [])
                self.assertIn(why, str(cm.exception))
            with self.assertRaises(S.Refused):
                qualify.stage_cmd(mk_ctx(), "baseline", "base-3", ["perch"])
            with self.assertRaises(S.Refused):
                qualify.stage_cmd(mk_ctx(), "experiment", "x-1", [])


class TreeBeforePhone(unittest.TestCase):
    """Sol I20: both public commands, under an existing pause, refuse a
    disturbed tree before any phone recovery, setup or tool."""

    def ctx(self, h):
        from batchfakes import FakeDev, FakeTools, FakeTree
        from batchfakes import mk_ctx as mk
        tree = FakeTree(FULL)
        tree.dirty.add("vendor/lineage")
        self.dev, self.tools = FakeDev(), FakeTools()
        return mk(tree=tree, dev=self.dev, tools=self.tools)

    def locks(self):
        return mock.patch.object(C.Locks, "__enter__", lambda self: self), \
            mock.patch.object(C.Locks, "__exit__", lambda self, *a: None)

    def test_qualify_and_stage_refuse_a_dirty_tree_before_the_phone(self):
        for cmd in ("qualify", "stage"):
            with bench_home() as h:
                kg_files(h)
                write_json(h / "batches/kg/batch.json", {"batch": "kg", "dates": {"vendor": "100"}})
                with S.transaction() as st:
                    S.set_pause(st, "two voids in a row")
                ctx = self.ctx(h)
                l1, l2 = self.locks()
                with l1, l2, mock.patch.object(phases, "baseline_xml", return_value={}), \
                        mock.patch.object(qualify.compose, "manifest_xml", return_value={}):
                    with self.assertRaises(S.Refused) as cm:
                        if cmd == "qualify":
                            qualify.qualify_cmd(ctx, False, [])
                        else:
                            qualify.stage_cmd(ctx, "calibration", "cal-9", [])
                self.assertIn("vendor/lineage is dirty", str(cm.exception), cmd)
                self.assertEqual((self.dev.cmds, self.tools.calls), ([], []), cmd)


class Part2(unittest.TestCase):
    def test_part2_keeps_its_qa_result_after_the_rollback(self):
        """Gemini minor: run_part2 reported the QA result of a stale copy
        (None); it is read before abort clears running."""
        from batchfakes import mk_ctx as mk
        from riverbatch import ctx as Cx
        from riverbatch import kb, recover
        with bench_home() as h:
            kg_files(h)

            def compose_run(ctx, bid, purpose, members, base, kg, pin, verified):
                run = {"id": bid, "purpose": purpose, "touched": [], "base": dict(base), "members": [],
                       "composed": {p: {"sha": v} for p, v in base.items() if p != ".repo/manifests"},
                       "versions": {"image_id": pin}, "phase": "claimed", "outcome": None, "attempt_id": None}
                with S.transaction() as st:
                    st["running"] = run
                return run

            def test(ctx, run):
                Cx.update_running(qa={"run_id": "qa-7", "exit": 0, "eligible": True})
                return {"class": O.PASS}
            names = ["build", "stage_run", "restore_or_void", "flash", "bench", "setup_candidate"]
            ps = [mock.patch.object(phases, n) for n in names] + [
                mock.patch.object(phases, "test", test), mock.patch.object(qualify, "compose_run", compose_run),
                mock.patch.object(qualify, "commit_phase"), mock.patch.object(recover, "setup_known_good"),
                mock.patch.object(recover, "setup_owed", return_value=(False, kb.Check(kb.OK)))]
            for x in ps:
                x.start()
            try:
                out = qualify.run_part2(mk(), dict(FULL), {"latest": {"cameras": [0, 1, 2]}}, "img", [])
            finally:
                mock.patch.stopall()
            self.assertEqual(out["qa"]["run_id"], "qa-7")
            self.assertTrue(out["passed"], out["failed"])


class StageFinish(unittest.TestCase):
    """I6 and I10 through stage_cmd with its steps faked."""

    def test_running_and_lease_cleared_after_staging(self):
        with bench_home() as h:
            kg_files(h)
            seen = {}

            def compose_run(ctx, bid, purpose, members, base, kg, pin, verified):
                run = {"id": bid, "purpose": purpose, "touched": [], "base": base,
                       "composed": {p: {"sha": v} for p, v in base.items() if p != ".repo/manifests"},
                       "versions": {"image_id": pin}}
                with S.transaction() as st:
                    st["running"] = run
                return run

            def stage_run(ctx, run, kind, experiment, kg):
                seen["kg"] = kg
                seen["running"] = S.read()["running"]["id"]
            with mock.patch.object(qualify, "pin_image", return_value="img"), \
                    mock.patch.object(qualify, "local_base", return_value=dict(FULL)), \
                    mock.patch.object(qualify, "tree_first"), \
                    mock.patch.object(qualify, "compose_run", compose_run), \
                    mock.patch.object(qualify.phases, "build"), \
                    mock.patch.object(qualify.phases, "stage_run", stage_run), \
                    mock.patch.object(qualify.phases, "restore_or_void"), \
                    mock.patch.object(C.Locks, "__enter__", lambda self: self), \
                    mock.patch.object(C.Locks, "__exit__", lambda self, *a: None):
                out = qualify.stage_cmd(mk_ctx(), "baseline", "base-3", [])
            st = S.read()
            self.assertEqual(out["staged"], "base-3")
            self.assertEqual(seen, {"kg": None, "running": "base-3"})    # I10: no camera backstop
            self.assertIsNone(st["running"])                            # I6
            self.assertIsNone(st["lease"])

    def test_camera_backstop_for_production_only(self):
        with bench_home() as h:
            kg_files(h)
            write_json(h / "batches/b0001/batch.json", {"cameras": {"set": [0, 1]}})
            with S.transaction() as st:
                st["running"] = {"id": "b0001", "phase": "building"}
            run = {"id": "b0001", "composed": {p: {"sha": v} for p, v in FULL.items() if p != ".repo/manifests"},
                   "base": dict(FULL)}
            ctx = mk_ctx()
            with self.assertRaises(C.Abort) as cm:
                phases.stage_run(ctx, run, "batch", False, {"latest": {"cameras": [0, 1, 2]}})
            self.assertEqual(cm.exception.pause, "camera set differs from known-good; needs Zak")
            phases.stage_run(ctx, run, "baseline", False, None)          # a changed set is allowed


class ABTest(unittest.TestCase):
    """R14.2 and I8."""

    CS = {"device/motorola/river": "1" * 40, "kernel/motorola/sdm632": "2" * 40, ".repo/manifests": "3" * 40}

    def manifest_xml(self, river="1" * 40):
        return ("<manifest>\n"
                f'  <project path="device/motorola/river" name="x" revision="{river}"/>\n'
                f'  <project path="kernel/motorola/sdm632" name="k" revision="{"2" * 40}"/>\n'
                f'  <project path="external/foo" name="f" revision="{"7" * 40}"/>\n</manifest>\n')

    def test_i8_build_manifest_revisions_checked_and_blanked(self):
        a, errs = abtest.normalize_build_manifest(self.manifest_xml(), self.CS)
        self.assertEqual(errs, [])
        self.assertIn('revision="EXPECTED"', a)
        self.assertIn("7" * 40, a)                                # other projects keep their revisions
        b, errs = abtest.normalize_build_manifest(self.manifest_xml("9" * 40), self.CS)
        self.assertTrue(errs and "device/motorola/river" in errs[0])
        b_cs = dict(self.CS, **{"device/motorola/river": "9" * 40})
        b, errs = abtest.normalize_build_manifest(self.manifest_xml("9" * 40), b_cs)
        self.assertEqual((errs, a), ([], b))                      # A and B compare equal after blanking

    def test_mask_prop(self):
        text = "ro.build.date=X\nro.vendor.build.date.utc=1\nro.river.build_id=b1-x\nro.product.model=g7\n"
        self.assertEqual(abtest.mask_prop(text, abtest.load("abtest_masks.json")["props"]), "ro.product.model=g7")

    def test_hash_tree(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            (p / "system/product/etc").mkdir(parents=True)
            (p / "vendor/etc").mkdir(parents=True)
            (p / "system/build.prop").write_text("ro.build.date=1\nro.x=1\n")
            (p / "system/product/etc/build-manifest.xml").write_text(self.manifest_xml())
            (p / "vendor/etc/a.conf").write_text("a")
            files, errs = abtest.hash_tree(p, ["ro\\.build\\.date"], [], self.CS)
            (p / "system/build.prop").write_text("ro.build.date=2\nro.x=1\n")
            files2, _ = abtest.hash_tree(p, ["ro\\.build\\.date"], [], self.CS)
            self.assertEqual(files, files2)
            self.assertEqual(errs, [])

    def m(self, files=None, dtbo="d", ovl=None, ikc="i", old=True, new=False):
        return {"files": files or {"vendor/etc/audio_ext_spkr.conf": "1"}, "errors": [], "dtbo": dtbo,
                "overlays": ovl or {"o": "1"}, "boot_info": {"ikconfig_sha256": ikc}, "kernel_old": old,
                "kernel_new": new}

    def passing(self):
        cfg = abtest.load("abtest_config.json")
        a = self.m()
        b1 = self.m(files={"vendor/etc/audio_ext_spkr.conf": "2", "vendor/etc/abtest-marker.txt": "3"},
                    old=False, new=True)
        return cfg, {"A": a, "B1": b1, "A1": self.m(), "B2": self.m(dtbo="d2", ovl={"o": "2"}), "A2": self.m(),
                     "B3": self.m(ikc="i2"), "A3": self.m(), "B4": dict(b1), "A4": self.m()}

    def test_compare_passes_and_each_criterion_fails(self):
        cfg, m = self.passing()
        self.assertEqual(abtest.compare(m, cfg), [])
        bad = [("A2", dict(m["A2"], dtbo="x")), ("B2", dict(m["B2"], dtbo="d")), ("B3", dict(m["B3"], boot_info={"ikconfig_sha256": "i"})),
               ("A4", dict(m["A4"], kernel_new=True)), ("B1", dict(m["B1"], kernel_old=True)),
               ("A1", dict(m["A1"], files={"x": "1"}))]
        for k, v in bad:
            mm = dict(m, **{k: v})
            self.assertTrue(abtest.compare(mm, cfg), k)

    def test_config_targets_are_same_size(self):
        cfg = abtest.load("abtest_config.json")
        for b in ("B1", "B2", "B3"):
            for e in cfg[b]["edits"]:
                self.assertEqual(len(e["old"]), len(e["new"]), e)
                self.assertNotEqual(e["old"], e["new"])
        self.assertEqual(cfg["B4"]["string"], cfg["B1"]["kernel"]["new"])


def repo_manifest_r(revs: dict) -> str:
    """What `repo manifest -r` prints for default.xml: one <project> line per
    project at its revision, annotations on their own lines."""
    import xml.etree.ElementTree as ET

    from riverbatch import paths
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<manifest>"]
    for pj in ET.parse(paths.MANIFEST_DIR / "default.xml").getroot().iter("project"):
        path = pj.get("path") or pj.get("name")
        rev = revs.get(path, "7" * 40)
        anns = pj.findall("annotation")
        out.append(f'  <project name="{pj.get("name")}" path="{path}" revision="{rev}" '
                   f'upstream="refs/heads/river-22.2"' + (">" if anns else "/>"))
        for a in anns:
            out.append(f'    <annotation name="{a.get("name")}" value="{a.get("value")}"/>')
        if anns:
            out.append("  </project>")
    return "\n".join(out + ["</manifest>", ""])


def produced(revs: dict) -> str:
    """build-manifest_xml.mk: repo manifest -r | grep -Ev "proprietary_"."""
    return "\n".join(ln for ln in repo_manifest_r(revs).split("\n") if "proprietary_" not in ln)


class BuildManifestProducer(unittest.TestCase):
    """Sol B1: the producer filters the proprietary_* forks out."""

    CS = {p: f"{i:x}" * 40 for i, p in enumerate(sorted(merge.slugs()), start=1)}
    VENDOR = {"vendor/motorola/river", "vendor/motorola/sdm632-common"}

    def product(self, t, xml):
        prod = Path(t) / "product"
        (prod / "system/product/etc").mkdir(parents=True)
        (prod / "vendor").mkdir()
        if xml is not None:
            (prod / "system/product/etc/build-manifest.xml").write_text(xml)
        return prod

    def test_the_producer_drops_exactly_the_two_vendor_forks(self):
        self.assertEqual(abtest.producer_excluded(list(self.CS)), self.VENDOR)
        text = produced(self.CS)
        for p in self.VENDOR:
            self.assertNotIn(f'path="{p}"', text)

    def test_producer_shaped_xml_measures_clean(self):
        cfg = abtest.load("abtest_config.json")
        heads = {p: self.CS[p] for p in self.VENDOR}
        with tempfile.TemporaryDirectory() as t:
            m = abtest.measure(self.product(t, produced(self.CS)), Path(t) / "staged", cfg, self.CS, heads)
            self.assertEqual(m["errors"], [])
            # The excluded forks are checked against the trial checkout.
            bad = dict(heads, **{"vendor/motorola/river": "e" * 40})
            m = abtest.measure(self.product(t + "/2", produced(self.CS)), Path(t) / "staged", cfg, self.CS, bad)
            self.assertTrue(any("vendor/motorola/river (not in build-manifest.xml)" in e for e in m["errors"]))

    def test_a_missing_build_manifest_fails(self):
        cfg = abtest.load("abtest_config.json")
        with tempfile.TemporaryDirectory() as t:
            m = abtest.measure(self.product(t, None), Path(t) / "staged", cfg, self.CS,
                               {p: self.CS[p] for p in self.VENDOR})
            self.assertTrue(any("build-manifest.xml is missing" in e for e in m["errors"]), m["errors"])

    def test_an_emitted_fork_still_needs_its_revision(self):
        cfg = abtest.load("abtest_config.json")
        wrong = dict(self.CS, **{"device/motorola/river": "e" * 40})
        with tempfile.TemporaryDirectory() as t:
            m = abtest.measure(self.product(t, produced(wrong)), Path(t) / "staged", cfg, self.CS,
                               {p: self.CS[p] for p in self.VENDOR})
            self.assertTrue(any("device/motorola/river at eeeeeeeeeeee" in e for e in m["errors"]), m["errors"])

    def test_mask_is_a_full_match(self):
        text = "ro.build.host=a\nro.build.hostname=b\n"
        self.assertEqual(abtest.mask_prop(text, ["ro\\.build\\.host"]), "ro.build.hostname=b")


class OneBuild(unittest.TestCase):
    """Sol M8, Gemini GB3, Opus OB1: a failed B restores the tree."""

    BASE = BuildManifestProducer.CS
    COMMITS = {"device/motorola/river": "e" * 40}

    def setUp(self):
        from batchfakes import FakeTree
        self.tree = FakeTree(self.BASE)
        self.cfg = abtest.load("abtest_config.json")

    def build(self, exc):
        from batchfakes import mk_ctx
        ctx = mk_ctx(tree=self.tree)
        with mock.patch.object(abtest.phases, "build", side_effect=exc):
            abtest.one_build(ctx, "q0001", "B1", dict(self.BASE), dict(self.COMMITS), "img", self.cfg)
        return ctx

    def test_failed_b_after_checkout_restores_and_clears(self):
        with bench_home():
            with self.assertRaises(C.Abort):
                self.build(C.Abort({"class": O.VOID, "reason": "build infrastructure failure"}))
            st = S.read()
            self.assertIsNone(st["running"])
            self.assertIsNone(st["lease"])
            self.assertEqual(self.tree.head["device/motorola/river"], "river-22.2")
            from batchfakes import mk_ctx
            self.assertEqual(abtest.delete_abtest_branches(mk_ctx(tree=self.tree)), [])
            self.assertNotIn("batch/abtest-B1", self.tree.branches["device/motorola/river"])

    def test_an_interrupt_leaves_running_and_the_lease_for_recovery(self):
        with bench_home():
            with self.assertRaises(KeyboardInterrupt):
                self.build(KeyboardInterrupt())
            st = S.read()
            self.assertEqual(st["running"]["id"], "q0001-ab-B1")
            self.assertEqual(st["lease"]["batch"], "q0001-ab-B1")
            self.assertEqual(self.tree.head["device/motorola/river"], "batch/abtest-B1")

    def test_b4_removes_the_exited_container(self):
        from batchfakes import FakePodman, mk_ctx

        class Proc:
            returncode = 0

            def poll(self):
                return 0

            def wait(self):
                return 0
        with bench_home():
            pod = FakePodman(image="img", ps="exited\n")
            ctx = mk_ctx(podman=pod)
            ctx.tools.start = lambda *a, **k: Proc()
            with S.transaction() as st:
                st["running"] = {"id": "q0001-ab-B4", "phase": "claimed"}
            run = {"id": "q0001-ab-B4"}
            done = abtest.interrupted_build(ctx, run, "img", {"object": "x.o", "string": "s"})
            self.assertTrue(done)
            self.assertIn(("rm", "lineageos-build"), pod.calls)


class Versions(unittest.TestCase):
    def test_runner_build_counts_tracked_package_files_only(self):
        files = version.package_files()
        self.assertIn("qa/lib/riverbatch/tick.py", files)
        self.assertIn("qa/lib/riverbatch/manifest_stub.xml", files)
        self.assertIn("qa/lib/riverbatch/tc_allowlist.json", files)
        self.assertFalse(any("__pycache__" in f for f in files))
        self.assertNotIn("qa/lib/riverbatch/../riverqa/qa.py", files)
        self.assertEqual(len(version.runner_build()), 16)
        self.assertNotEqual(version.runner_build(), version.runner_tools())

    def test_harness_version_ignores_the_runner(self):
        import inspect

        from riverqa import qa
        src = inspect.getsource(qa.harness_version)
        self.assertIn('"riverqa"', src)
        self.assertNotIn("riverbatch", src)


class Outcome(unittest.TestCase):
    def test_qa_gate_state_pauses(self):
        self.assertEqual(phases.gate_reasons({"not_eligible_because": [
            "thresholds.json is not calibrated", "controls not qualified (2 not run)", "apps.calibration: FAIL"]}),
            ["thresholds.json is not calibrated", "controls not qualified (2 not run)"])
        self.assertEqual(O.PASS, "pass")


if __name__ == "__main__":
    unittest.main()
