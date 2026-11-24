"""State transactions and outcome rules: R4.1, R7.6, R8.5, R8.6 with I3,
R9.3-R9.5, the third-failure rule with I13, reports_pending (I21 m9)."""
import json
import unittest

import _path  # noqa: F401
from benchhome import bench_home
from riverbatch import outcomes as O, paths, state as S

K = "zrm625/android_device_motorola_river#6"


def entry(name, pos, status="queued", prs=None, flags=None, batch=None):
    prs = prs or [{"key": f"zrm625/x#{pos}", "path": "device/motorola/river", "head": "1" * 40}]
    return {"name": name, "position": pos, "status": status, "rev": 1, "prs": prs, "flags": flags or {},
            "identity": "+".join(sorted(p["key"] for p in prs)), "kind": "change", "batch": batch, "decisions": []}


class Transactions(unittest.TestCase):
    def test_atomic_and_rolled_back_on_error(self):
        with bench_home():
            with S.transaction("x") as st:
                st["next_batch"] = 7
            self.assertEqual(S.read()["next_batch"], 7)
            with self.assertRaises(RuntimeError):
                with S.transaction() as st:
                    st["next_batch"] = 99
                    raise RuntimeError("boom")
            self.assertEqual(S.read()["next_batch"], 7)
            self.assertTrue(paths.events_file().exists())

    def test_unparsable_fails_closed(self):
        with bench_home():
            paths.runner().mkdir(parents=True)
            paths.state_file().write_text("{")
            with self.assertRaises(S.StateError):
                S.read()

    def test_reports_pending_is_idempotent(self):
        with bench_home():
            with S.transaction() as st:
                S.add_report(st, "b0001")
                S.add_report(st, "b0001")
            self.assertEqual(S.read()["reports_pending"], ["b0001"])

    def test_failure_history_follows_identity_and_name(self):
        """I13: keyed by the PR set; counted for the name across identities."""
        with bench_home():
            with S.transaction() as st:
                e = entry("perch", 1, prs=[{"key": K, "path": "p", "head": "1" * 40}])
                st["entries"].append(e)
                S.link_aliases(st, "perch", [K])
                for i in range(2):
                    S.add_failure(st, e, {"batch": f"b{i}"})
                # same name, new PR set (a replacement): still counted
                S.link_aliases(st, "perch", ["zrm625/android_vendor_lineage#5"])
                S.add_failure(st, dict(e, identity="zrm625/android_vendor_lineage#5"), {"batch": "b9"})
            st = S.read()
            self.assertEqual(len(S.failures(st, "perch")), 3)
            self.assertEqual(S.alias_conflicts(st, "other-name", [K]), [f"{K} belongs to perch"])


class Builds(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(O.build_class(0, "0 12.3 456"), "ok")
        for code in (1, 2, 89, 92, 124):
            self.assertEqual(O.build_class(code, f"{code} 1 1"), "m-failed")
        for code in (90, 91, 125, 126, 127, 128, 137):
            self.assertEqual(O.build_class(code, f"{code} 0"), "void")
        self.assertEqual(O.build_class(1, None), "void")             # no .bench-result
        self.assertEqual(O.build_class(1, "1 1 1", low_disk=True), "void")

    def test_fingerprint(self):
        log = "x\n\x1b[31mFAILED: /src/out/foo.o\x1b[0m\nnote\n/src/a.c:1: error: bad\nerror: later\n"
        self.assertEqual(O.fingerprint(log), {"failed": "FAILED: out/foo.o", "error": "a.c:1: error: bad"})


def slot_state(batch="b0001", how="rescue: batch b0001: boot failed; wipe #4; slot a verified", pair=None):
    return {"pair": pair, "history": [{"event": "reserve", "batch": batch},
                                      {"event": "rolled-back", "batch": batch, "kind": "batch", "how": how}]}


WIPES = [{"seq": 3, "attempt_id": "fa-1", "how": "flash", "ok": True, "at": "2026-11-23T10:01:00+00:00"},
         {"seq": 4, "attempt_id": "fa-1", "how": "rollback", "ok": True, "at": "2026-11-23T10:20:00+00:00"}]
FLASH = {"attempts": [{"attempt_id": "fa-1", "started_at": "2026-11-23T10:00:00+00:00", "target": "b"}]}


class Rescue(unittest.TestCase):
    """R8.6 rescue evidence, for both exit-4 paths of flash (I3) and for
    device-bench (H17)."""

    def test_verified(self):
        ok, why = O.rescue_evidence(slot_state(), WIPES, FLASH, "b0001", "fa-1")
        self.assertTrue(ok, why)
        ok, _ = O.rescue_evidence(slot_state(how="rescue: batch b0001: bootloader fell back to slot a; wipe #4; v"),
                                  WIPES, FLASH, "b0001", "fa-1")
        self.assertTrue(ok)

    def test_not_a_rescue_or_wrong_wipe(self):
        cases = [
            (slot_state(how="bootloader fell back to slot a; wipe #4; v"), WIPES, "not a rescue"),   # pre-I3 path
            (slot_state(how="rescue: x; wipe #3; v"), WIPES, "not this attempt's rollback"),         # the flash wipe
            (slot_state(batch="b0002"), WIPES, "not this batch's"),
            (slot_state(pair={"batch": "b0001"}), WIPES, "still holds a pair"),
            (slot_state(), [dict(WIPES[1], attempt_id="fa-0")], "not this attempt's"),
            (slot_state(), [dict(WIPES[1], at="2026-11-23T09:00:00+00:00")], "older than"),
        ]
        for ss, wipes, want in cases:
            ok, why = O.rescue_evidence(ss, wipes, FLASH, "b0001", "fa-1")
            self.assertFalse(ok)
            self.assertIn(want, why)

    def test_adopt_by_exit_code(self):
        batch = {"flash_attempt": "fa-1"}
        booted = {"attempts": [dict(FLASH["attempts"][0], result="booted")]}
        pair = {"batch": "b0001", "target": "b", "attempts": ["fa-1"]}
        self.assertEqual(O.adopt_flash(0, batch, booted, {"pair": pair}, [], "b0001")[0], "adopt")
        self.assertEqual(O.adopt_flash(0, batch, booted, {"pair": dict(pair, target="a")}, [], "b0001")[0], "halt")
        self.assertEqual(O.adopt_flash(4, batch, FLASH, slot_state(), WIPES, "b0001")[0], "rescued")
        self.assertEqual(O.adopt_flash(4, batch, FLASH, slot_state(how="x"), WIPES, "b0001")[0], "halt")
        self.assertEqual(O.adopt_flash(1, {}, {}, {}, [], "b0001")[0], "void")
        self.assertEqual(O.adopt_flash(1, batch, FLASH, {}, [], "b0001")[0], "halt")
        self.assertEqual(O.adopt_flash(3, batch, FLASH, {}, [], "b0001")[0], "halt")


class Binding(unittest.TestCase):
    def test_bind(self):
        res = {"mode": "batch", "batch": "b1", "run_id": "r", "flash_attempt": "fa", "harness_version": "h",
               "serial": "ZY225DPT7F"}
        ss = {"pair": {"batch": "b1"}}
        self.assertTrue(O.bind(res, {"run_id": "r"}, "b1", "fa", "h", ss)[0])
        for k, v in (("mode", "calibrate"), ("batch", "b2"), ("run_id", "x"), ("flash_attempt", "f0"),
                     ("harness_version", "h2"), ("serial", "X")):
            self.assertFalse(O.bind(dict(res, **{k: v}), {"run_id": "r"}, "b1", "fa", "h", ss)[0], k)
        self.assertFalse(O.bind(res, {"run_id": "r"}, "b1", "fa", "h", {"pair": None})[0])


class Decisions(unittest.TestCase):
    def run_state(self, members):
        st = S.empty()
        st["entries"] = [entry(m, i + 1, "in-batch", batch="b0002") for i, m in enumerate(members)]
        run = {"id": "b0002", "purpose": "batch", "members": [{"name": m} for m in members], "content_key": "k2"}
        return st, run

    def test_split_on_a_multi_change_fail(self):
        st, run = self.run_state(["a", "b"])
        O.apply(st, run, {"class": O.FAIL, "checks": ["x"]})
        self.assertEqual([(e["status"], e["flags"].get("solo")) for e in st["entries"]], [("queued", True)] * 2)
        self.assertEqual(st["failure_history"], {})

    def test_single_fail_records_evidence(self):
        st, run = self.run_state(["a"])
        O.apply(st, run, {"class": O.FAIL, "checks": ["x"], "evidence": {"step": "qa"}})
        self.assertEqual(st["entries"][0]["status"], "failed")
        self.assertEqual(len(S.failures(st, "a", st["entries"][0]["identity"])), 1)

    def test_two_voids_pause(self):
        st, run = self.run_state(["a"])
        O.apply(st, run, {"class": O.VOID, "reason": "x"})
        self.assertIsNone(st["pause"])
        st["entries"][0]["status"], st["entries"][0]["batch"] = "in-batch", "b0002"
        O.apply(st, run, {"class": O.VOID, "reason": "y"})
        self.assertIn("two voids", st["pause"]["reason"])

    def test_transport_after_prs_open_strikes(self):
        st, run = self.run_state(["a"])
        run["reached_prs_open"] = True
        O.apply(st, run, {"class": O.TRANSPORT})
        self.assertEqual((st["strikes"]["transport_prs"], st["voids_in_a_row"]), (1, 0))
        st["entries"][0]["status"], st["entries"][0]["batch"] = "in-batch", "b0002"
        O.apply(st, run, {"class": O.TRANSPORT})
        self.assertIn("GitHub failed twice", st["pause"]["reason"])

    def test_desk_suspect(self):
        st, run = self.run_state(["b"])
        st["last_fail"] = {"members": ["a"], "checks": ["radios.wifi"], "batch": "b0001", "content_key": "k1"}
        out = O.decide_fail(st, run, ["radios.wifi", "x"], {})
        self.assertEqual(out["class"], O.DESK)
        O.apply(st, run, out)
        self.assertEqual(st["entries"][0]["status"], "queued")
        self.assertIn("desk suspect: radios.wifi", st["pause"]["reason"])
        # a split of the same batch is not a desk suspect
        st["last_fail"] = {"members": ["a", "b"], "checks": ["x"], "batch": "b0001", "content_key": "k1"}
        self.assertEqual(O.decide_fail(st, run, ["x"], {})["class"], O.FAIL)
        # nothing shared: last_fail moves on
        st["last_fail"] = {"members": ["a"], "checks": ["y"], "batch": "b0001", "content_key": "k1"}
        O.decide_fail(st, run, ["x"], {})
        self.assertEqual(st["last_fail"]["batch"], "b0002")

    def test_experiment_waits_on_zak_and_qualify_touches_nothing(self):
        st, run = self.run_state(["a"])
        O.apply(st, dict(run, purpose="experiment"), {"class": O.PASS})
        self.assertEqual(st["entries"][0]["status"], "waiting-zak")
        st, run = self.run_state(["a"])
        O.apply(st, dict(run, purpose="qualify"), {"class": O.FAIL})
        self.assertEqual(st["entries"][0]["status"], "in-batch")


class Waiting(unittest.TestCase):
    def test_list(self):
        st = S.empty()
        st["halt"] = {"reason": "x", "advice": "y"}
        st["rebaseline"] = {"settle": "b3"}
        st["entries"] = [dict(entry("a", 1, "failed"), status_at="2000-01-01T00:00:00+00:00"),
                         dict(entry("b", 2, "waiting-zak"), reason="camera")]
        w = S.waiting_on_zak(st)
        self.assertTrue(any("HALT" in x for x in w) and any("re-baseline" in x for x in w))
        self.assertTrue(any("a: failed" in x for x in w) and any("b: waiting" in x for x in w))
        json.dumps(w)


if __name__ == "__main__":
    unittest.main()
