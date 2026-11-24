"""Approval records and bundles (R5.1, R5.2, I13, I14, I21 basenames)."""
import json
import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
from benchhome import bench_home
from riverbatch import records

H1, H2, H3 = "1" * 40, "2" * 40, "3" * 40
K1, K2 = "zrm625/android_device_motorola_river#6", "zrm625/android_vendor_lineage#5"


def sha(b: bytes) -> str:
    return records.sha256_bytes(b)


def block(scope: str, heads: dict) -> str:
    """The scope block of a brief or reply (Sol B3)."""
    if scope == "full":
        lines = [f"Head: {k} {v}" for k, v in heads.items()]
    else:
        lines = [f"Delta: {k} {v['from']} -> {v['to']}" for k, v in heads.items()]
    return f"Scope: {scope}\n" + "\n".join(lines) + "\n"


def write_round(d: Path, tag: str, scope: str, heads: dict, verdicts=None, reply_extra="", brief_extra=""):
    brief = f"# brief {tag}\n" + block(scope, heads) + brief_extra + "\n"
    (d / f"{tag}-brief.md").write_text(brief)
    verdicts = verdicts or {p: "APPROVE 9" for p in records.PROVIDERS}
    replies = {}
    for p in records.PROVIDERS:
        text = block(scope, heads) + f"I reviewed it.\nVerdict: {verdicts[p]}\n{reply_extra}"
        (d / f"{tag}-{p}.txt").write_text(text)
        replies[p] = {"file": f"{tag}-{p}.txt", "sha256": sha(text.encode())}
    return {"scope": scope, "at": "2026-11-23", "brief": f"{tag}-brief.md", "brief_sha256": sha(brief.encode()),
            "heads": heads, "verdicts": verdicts, "replies": replies}


def make_record(d: Path, name="perch-launcher", kind="change", delta=True, **kw):
    rounds = [write_round(d, "r1", "full", {K1: H1, K2: H2})]
    if delta:
        rounds.append(write_round(d, "r2", "delta", {K1: {"from": H1, "to": H3}}))
    rec = {"version": 2, "name": name, "kind": kind, "alone": False, "needs_zak": None, "branch": name,
           "prs": [{"key": K1, "path": "device/motorola/river"}, {"key": K2, "path": "vendor/lineage"}],
           "rounds": rounds, "prior": [], **kw}
    (d / "record.json").write_text(json.dumps(rec))
    return rec


class Validate(unittest.TestCase):
    def test_valid_chain(self):
        with tempfile.TemporaryDirectory() as t:
            rec = make_record(Path(t))
            self.assertEqual(records.validate(rec, Path(t)), [])
            self.assertEqual(records.approved_heads(rec), {K1: H3, K2: H2})
            self.assertEqual(records.identity(rec), f"{K1}+{K2}" if K1 < K2 else f"{K2}+{K1}")

    def test_empty_prs_and_first_round_full(self):
        with tempfile.TemporaryDirectory() as t:
            rec = make_record(Path(t))
            self.assertIn("prs is empty", records.validate(dict(rec, prs=[]), Path(t)))
            bad = dict(rec, rounds=[dict(rec["rounds"][1])])
            self.assertTrue(any("first round must be full" in e for e in records.validate(bad, Path(t))))

    def test_delta_from_must_chain(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d, delta=False)
            rec["rounds"].append(write_round(d, "r2", "delta", {K1: {"from": H2, "to": H3}}))
            self.assertTrue(any("not the head approved so far" in e for e in records.validate(rec, d)))

    def test_objection_is_invalid(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d, delta=False)
            rec["rounds"][0] = write_round(d, "r1", "full", {K1: H1, K2: H2},
                                           verdicts={"opus": "APPROVE 9", "gemini": "APPROVE 8", "sol": "OBJECT 6"})
            self.assertTrue(any("sol did not approve" in e for e in records.validate(rec, d)))

    def reply(self, d, rec, rnd, prov, text):
        (d / f"r{rnd + 1}-{prov}.txt").write_text(text)
        rec["rounds"][rnd]["replies"][prov]["sha256"] = sha(text.encode())
        return records.validate(rec, d)

    def test_i14_reply_must_state_the_recorded_verdict(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d, delta=False)
            errs = self.reply(d, rec, 0, "sol", block("full", {K1: H1, K2: H2}) + "Verdict: OBJECT 6\n")
            self.assertTrue(any("sol: reply's verdict" in e for e in errs))

    def test_i14_reply_without_a_verdict_line(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d, delta=False)
            errs = self.reply(d, rec, 0, "opus", block("full", {K1: H1, K2: H2}) + "Looks good, approve.\n")
            self.assertTrue(any("opus: reply has no 'Verdict:' line" in e for e in errs), errs)

    def test_i14_reply_must_state_the_round_scope(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d)
            errs = self.reply(d, rec, 1, "opus", block("full", {K1: H3}) + "Verdict: APPROVE 9\n")
            self.assertTrue(any("states scope ['full']" in e for e in errs))

    def test_i14_full_heads_not_prefixes(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d, delta=False)
            errs = self.reply(d, rec, 0, "gemini", f"Scope: full\nHead: {K1} {H1[:12]}\nHead: {K2} {H2[:12]}\n"
                                                   "Verdict: APPROVE 9\n")
            self.assertTrue(any("gemini: reply: malformed Head line" in e for e in errs), errs)
            brief = f"Scope: full\nHead: {K1} {H1[:12]}\n"
            (d / "r1-brief.md").write_text(brief)
            rec["rounds"][0]["brief_sha256"] = sha(brief.encode())
            self.assertTrue(any("brief: malformed Head line" in e for e in records.validate(rec, d)))

    def test_b3_sha_presence_alone_binds_nothing(self):
        # Every head appears, but not as the round's PR-to-head lines.
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d, delta=False)
            errs = self.reply(d, rec, 0, "sol", f"Scope: full\nI reviewed {H1} and {H2}.\nVerdict: APPROVE 9\n")
            self.assertTrue(any("sol: reply: scope block does not bind" in e for e in errs), errs)

    def test_b3_reversed_delta_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d)
            rev = block("delta", {K1: {"from": H3, "to": H1}})                 # to -> from: the reverse change
            errs = self.reply(d, rec, 1, "gemini", rev + "Verdict: APPROVE 9\n")
            self.assertTrue(any("gemini: reply: scope block does not bind" in e and K1 in e for e in errs), errs)
            brief = "# brief\n" + rev
            (d / "r2-brief.md").write_text(brief)
            rec["rounds"][1]["brief_sha256"] = sha(brief.encode())
            self.assertTrue(any("brief: scope block does not bind" in e for e in records.validate(rec, d)))

    def test_b3_swapped_prs_are_refused(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d, delta=False)
            errs = self.reply(d, rec, 0, "opus", block("full", {K1: H2, K2: H1}) + "Verdict: APPROVE 9\n")
            self.assertTrue(any("binds" in e and "other heads" in e for e in errs), errs)

    def test_b3_extra_pr_and_double_binding_are_refused(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d, delta=False)
            extra = block("full", {K1: H1, K2: H2, "zrm625/android_build#9": H3}) + "Verdict: APPROVE 9\n"
            self.assertTrue(any("outside the round" in e for e in self.reply(d, rec, 0, "opus", extra)))
            twice = block("full", {K1: H1, K2: H2}) + f"Head: {K1} {H3}\nVerdict: APPROVE 9\n"
            self.assertTrue(any("bound twice" in e for e in self.reply(d, rec, 0, "opus", twice)))

    def test_delta_reply_binds_from_and_to(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d)
            errs = self.reply(d, rec, 1, "sol", f"Scope: delta\nHead: {K1} {H3}\nVerdict: APPROVE 9\n")
            self.assertTrue(any("Head lines in a delta round" in e for e in errs), errs)
            self.assertTrue(any("no Delta line" in e for e in errs), errs)

    def test_sha_mismatch_and_basenames(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d)
            (d / "r1-opus.txt").write_text("edited")
            self.assertTrue(any("recorded SHA-256" in e for e in records.validate(rec, d)))
            rec = make_record(d)
            rec["rounds"][0]["brief"] = "../r1-brief.md"
            self.assertTrue(any("plain file name" in e for e in records.validate(rec, d)))

    def test_kinds(self):
        with tempfile.TemporaryDirectory() as t:
            rec = make_record(Path(t), kind="tooling")
            self.assertEqual(records.validate(rec, Path(t), kinds=("tooling",)), [])
            self.assertTrue(records.validate(rec, Path(t), kinds=("change", "experiment")))


class Bundles(unittest.TestCase):
    def test_store_is_immutable_and_reused(self):
        with bench_home(), tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d)
            raw = (d / "record.json").read_bytes()
            s1 = records.store_bundle(rec, raw, d)
            self.assertEqual(s1, sha(raw))
            self.assertEqual(records.store_bundle(rec, raw, d), s1)          # same content: reused
            got, errs = records.load_bundle(rec["name"], s1)
            self.assertEqual(errs, [])
            (d / "r1-opus.txt").write_text("changed")
            with self.assertRaises(records.RecordError):
                records.store_bundle(rec, raw, d)
            b = records.bundle_dir(rec["name"], s1)
            (b / "record.json").write_text("{}")
            self.assertIsNone(records.load_bundle(rec["name"], s1)[0])        # no longer hashes to its name

    def test_prior_names_and_missing_files(self):
        with bench_home(), tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d, prior=[{"brief": "..", "replies": {"sol": {"file": "record.json"},
                                                                    "opus": {"file": "old-opus.txt"}}}])
            self.assertNotIn("..", records.files_of(rec))
            self.assertNotIn("record.json", records.files_of(rec))
            raw = (d / "record.json").read_bytes()
            with self.assertRaises(records.RecordError) as cm:                  # old-opus.txt is not there
                records.store_bundle(rec, raw, d)
            self.assertIn("old-opus.txt", str(cm.exception))
            (d / "old-opus.txt").write_text("an old objection")
            s1 = records.store_bundle(rec, raw, d)
            self.assertEqual(json.loads((records.bundle_dir(rec["name"], s1) / "record.json").read_bytes()),
                             rec)                                               # never overwritten by a prior name

    def test_unparsable_bundle_is_an_error_not_a_crash(self):
        with bench_home():
            raw = b"not json"
            b = records.bundle_dir("x", sha(raw))
            b.mkdir(parents=True)
            (b / "record.json").write_bytes(raw)
            rec, errs = records.load_bundle("x", sha(raw))
            self.assertIsNone(rec)
            self.assertIn("does not parse", errs[0])


if __name__ == "__main__":
    unittest.main()
