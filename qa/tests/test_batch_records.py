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


def write_round(d: Path, tag: str, scope: str, heads: dict, verdicts=None, reply_extra="", brief_extra=""):
    hs = [h for v in heads.values() for h in ((v,) if isinstance(v, str) else (v["from"], v["to"]))]
    brief = f"# brief {tag}\nScope: {scope}\n" + "\n".join(hs) + brief_extra + "\n"
    (d / f"{tag}-brief.md").write_text(brief)
    verdicts = verdicts or {p: "APPROVE 9" for p in records.PROVIDERS}
    replies = {}
    for p in records.PROVIDERS:
        text = f"Scope: {scope}\nI reviewed {' '.join(hs)}.\nVerdict: {verdicts[p]}\n{reply_extra}"
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

    def test_i14_reply_must_state_the_recorded_verdict(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d, delta=False)
            text = f"Scope: full\n{H1} {H2}\nVerdict: OBJECT 6\n"          # the reply objected
            (d / "r1-sol.txt").write_text(text)
            rec["rounds"][0]["replies"]["sol"]["sha256"] = sha(text.encode())
            self.assertTrue(any("sol: reply's verdict" in e for e in records.validate(rec, d)))

    def test_i14_reply_must_state_the_round_scope(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d)
            text = f"Scope: full\n{H1} {H3}\nVerdict: APPROVE 9\n"          # a full-round reply cited for a delta
            (d / "r2-opus.txt").write_text(text)
            rec["rounds"][1]["replies"]["opus"]["sha256"] = sha(text.encode())
            self.assertTrue(any("states scope ['full']" in e for e in records.validate(rec, d)))

    def test_i14_full_heads_not_prefixes(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d, delta=False)
            text = f"Scope: full\n{H1[:12]} {H2[:12]}\nVerdict: APPROVE 9\n"
            (d / "r1-gemini.txt").write_text(text)
            rec["rounds"][0]["replies"]["gemini"]["sha256"] = sha(text.encode())
            self.assertTrue(any("does not cite" in e for e in records.validate(rec, d)))
            brief = f"Scope: full\n{H1[:12]}\n"
            (d / "r1-brief.md").write_text(brief)
            rec["rounds"][0]["brief_sha256"] = sha(brief.encode())
            self.assertTrue(any("brief does not list" in e for e in records.validate(rec, d)))

    def test_delta_reply_cites_from_and_to(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rec = make_record(d)
            text = f"Scope: delta\n{H3}\nVerdict: APPROVE 9\n"              # no from
            (d / "r2-sol.txt").write_text(text)
            rec["rounds"][1]["replies"]["sol"]["sha256"] = sha(text.encode())
            self.assertTrue(any("does not cite" in e for e in records.validate(rec, d)))

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


if __name__ == "__main__":
    unittest.main()
