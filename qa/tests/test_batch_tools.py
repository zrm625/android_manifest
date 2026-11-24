"""The holder (R6.2), TC on a scratch tree (R7.4), and the tooling merge
check behind `change merge --record` (H4, S-B3, I21)."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _path  # noqa: F401
from batchfakes import FakeGH
from riverbatch import paths, tc, tooling
from test_batch_records import write_round

HOLD = Path(paths.PKG / "hold.py")


class Holder(unittest.TestCase):
    def test_record_before_exec_same_pid_and_cleanup(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "children"
            probe = ("import os, json, sys, pathlib; d = pathlib.Path(sys.argv[1]); "
                     "rec = json.loads((d / f'{os.getpid()}.json').read_text()); "
                     "print(json.dumps({'pid': os.getpid(), 'pgid': os.getpgid(0), 'rec': rec, "
                     "'tok': os.environ.get('BENCH_LOCK_TOKEN')}))")
            r = subprocess.run([sys.executable, "-I", str(HOLD), str(d), "--", sys.executable, "-c", probe, str(d)],
                               capture_output=True, text=True, timeout=60, start_new_session=True,
                               env=dict(os.environ, BENCH_LOCK_TOKEN="tok1"))
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["rec"]["pid"], out["pid"])           # exec kept the pid
            self.assertEqual(out["rec"]["pgid"], out["pgid"])
            self.assertEqual(out["tok"], "tok1")
            self.assertEqual(list(d.iterdir()), [])                   # the record is gone

    def test_exit_status_and_signal(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "c"
            r = subprocess.run([sys.executable, "-I", str(HOLD), str(d), "--", "sh", "-c", "exit 7"], timeout=60)
            self.assertEqual(r.returncode, 7)
            p = subprocess.Popen([sys.executable, "-I", str(HOLD), str(d), "--", "sleep", "60"],
                                 start_new_session=True)
            for _ in range(100):
                if list(d.glob("*.json")):
                    break
                time.sleep(0.05)
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=30), 128 + signal.SIGTERM)
            self.assertEqual(list(d.glob("*.json")), [])

    def test_lock_fds_never_reach_the_tool(self):
        with tempfile.TemporaryDirectory() as t:
            lockf = Path(t) / "lock"
            fd = os.open(lockf, os.O_RDWR | os.O_CREAT)
            try:
                probe = f"import os; print(os.path.exists('/proc/self/fd/{fd}'))"
                r = subprocess.run([sys.executable, "-I", str(HOLD), str(Path(t) / "c"), "--", sys.executable, "-c",
                                    probe], pass_fds=(fd,), capture_output=True, text=True, timeout=60)
                self.assertEqual(r.stdout.strip(), "False")
            finally:
                os.close(fd)


def git(d, *a):
    subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    *a], check=True, capture_output=True)


class TreeCheck(unittest.TestCase):
    def tree(self, t):
        top = Path(t)
        (top / ".repo/manifests").mkdir(parents=True)
        for p in ("a/fork", "b/pinned"):
            (top / p).mkdir(parents=True)
            git(top / p, "init", "-q")
            (top / p / "f").write_text("x")
            git(top / p, "add", "f")
            git(top / p, "commit", "-qm", "x")
        sha = lambda p: subprocess.run(["git", "-C", str(top / p), "rev-parse", "HEAD"], capture_output=True,  # noqa: E731
                                       text=True).stdout.strip()
        m = top / ".repo/manifests"
        git(m, "init", "-q")
        (m / "default.xml").write_text(
            '<manifest><default revision="x"/>'
            '<project path="a/fork" name="fork" revision="refs/heads/river-22.2">'
            '<linkfile src="f" dest="link-f"/></project>'
            f'<project path="b/pinned" name="p" revision="{sha("b/pinned")}"/></manifest>')
        git(m, "add", "default.xml")
        git(m, "commit", "-qm", "m")
        (top / ".repo/manifest.xml").write_bytes((paths.PKG / "manifest_stub.xml").read_bytes())
        (top / ".repo/project.list").write_text("a/fork\nb/pinned\n")
        os.symlink("a/fork/f", top / "link-f")
        for n in ("AGENTS.md", "CLAUDE.md"):
            os.symlink(".repo/manifests/AGENTS.md", top / n)
        (top / "out").mkdir()
        return top, {"a/fork": sha("a/fork"), ".repo/manifests": sha(".repo/manifests")}

    def test_clean_tree_passes_and_each_disturbance_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            top, exp = self.tree(t)
            r = tc.check(exp, top)
            self.assertTrue(r.ok, r.blocking)
            (top / "a/fork/new.txt").write_text("y")
            self.assertTrue(any("new.txt" in b for b in tc.check(exp, top).blocking))
            os.remove(top / "a/fork/new.txt")
            (top / "stray").write_text("z")
            self.assertTrue(any("stray: outside every project" in b for b in tc.check(exp, top).blocking))
            os.remove(top / "stray")
            git(top / "a/fork", "update-index", "--assume-unchanged", "f")
            self.assertTrue(any("assume-unchanged" in b for b in tc.check(exp, top).blocking))
            git(top / "a/fork", "update-index", "--no-assume-unchanged", "f")
            (top / "out/x").write_text("1")
            self.assertTrue(any("out/ is not empty" in b for b in tc.check(exp, top).blocking))
            os.remove(top / "out/x")
            self.assertTrue(any("HEAD" in b for b in tc.check(dict(exp, **{"a/fork": "0" * 40}), top).blocking))
            (top / ".repo/local_manifests").mkdir()
            (top / ".repo/local_manifests/x.xml").write_text("<manifest/>")
            self.assertTrue(any("local_manifests" in b for b in tc.check(exp, top).blocking))

    def test_pycache_allowlist_holds_only_matching_pyc(self):
        with tempfile.TemporaryDirectory() as t:
            top, exp = self.tree(t)
            m = top / ".repo/manifests"
            (m / ".gitignore").write_text("__pycache__/\n")
            git(m, "add", ".gitignore")
            git(m, "commit", "-qm", "ignore")
            exp[".repo/manifests"] = subprocess.run(["git", "-C", str(m), "rev-parse", "HEAD"], capture_output=True,
                                                    text=True).stdout.strip()
            d = m / "qa/tests"
            (d / "__pycache__").mkdir(parents=True)
            (d / "t.py").write_text("")
            git(m, "add", "qa/tests/t.py")
            git(m, "commit", "-qm", "t")
            exp[".repo/manifests"] = subprocess.run(["git", "-C", str(m), "rev-parse", "HEAD"], capture_output=True,
                                                    text=True).stdout.strip()
            (d / "__pycache__/t.cpython-313.pyc").write_bytes(b"x")
            self.assertTrue(tc.check(exp, top).ok, tc.check(exp, top).blocking)
            (d / "__pycache__/evil.cpython-313.pyc").write_bytes(b"x")
            self.assertTrue(any("evil" in b for b in tc.check(exp, top).blocking))

    def test_allowlist_covers_untracked_and_ignored_only(self):
        """Gemini GM7: a tracked file changed under an allowlisted prefix
        blocks; an allowlisted __pycache__/ is checked file by file even when
        git reports it untracked (no .gitignore)."""
        with tempfile.TemporaryDirectory() as t:
            top, exp = self.tree(t)
            m = top / ".repo/manifests"
            (m / "qa/.ruff_cache").mkdir(parents=True)
            (m / "qa/.ruff_cache/tracked").write_text("a")
            (m / "qa/tests").mkdir(parents=True)
            (m / "qa/tests/t.py").write_text("")
            git(m, "add", "-f", "qa/.ruff_cache/tracked", "qa/tests/t.py")
            git(m, "commit", "-qm", "t")
            exp[".repo/manifests"] = subprocess.run(["git", "-C", str(m), "rev-parse", "HEAD"], capture_output=True,
                                                    text=True).stdout.strip()
            self.assertTrue(tc.check(exp, top).ok, tc.check(exp, top).blocking)
            (m / "qa/.ruff_cache/tracked").write_text("b")
            self.assertTrue(any("qa/.ruff_cache/tracked" in b for b in tc.check(exp, top).blocking))
            (m / "qa/.ruff_cache/tracked").write_text("a")
            (m / "qa/tests/__pycache__").mkdir()
            (m / "qa/tests/__pycache__/t.cpython-313.pyc").write_bytes(b"x")
            self.assertTrue(tc.check(exp, top).ok, tc.check(exp, top).blocking)       # untracked, no .gitignore
            (m / "qa/tests/__pycache__/payload.sh").write_text("x")
            self.assertTrue(any("payload.sh" in b for b in tc.check(exp, top).blocking))

    def test_lfs_pointer_is_the_dash_token(self):
        out = ("0123456789 * prebuilt/x86/a - b.apk\n"          # pulled, though its name has " - "
               "abcdef0123 - prebuilt/x86/webview.apk\n")      # a pointer
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "p").mkdir()
            (Path(t) / "p/.gitattributes").write_text("*.apk filter=lfs diff=lfs merge=lfs -text\n")
            with mock.patch.object(tc, "_git", return_value=subprocess.CompletedProcess([], 0, out, "")):
                self.assertEqual(tc.lfs_problems(Path(t), "p"), ["p: LFS pointer prebuilt/x86/webview.apk"])


class Tooling(unittest.TestCase):
    """H4: change merge --record checks, before any merge."""

    HEAD = "4" * 40
    KEY = "zrm625/android_manifest#12"

    def record(self, d, kind="tooling", key=None):
        key = key or self.KEY
        rnd = write_round(d, "t1", "full", {key: self.HEAD})
        rec = {"version": 2, "name": "runner-fix", "kind": kind, "alone": False, "needs_zak": None,
               "branch": "runner-fix", "prs": [{"key": key, "path": ".repo/manifests"}], "rounds": [rnd],
               "prior": []}
        (d / "record.json").write_text(json.dumps(rec))
        return d / "record.json"

    def gh(self, extra=None):
        gh = FakeGH()
        gh.prs[("zrm625/android_manifest", 12)] = {"state": "OPEN", "headRefName": "runner-fix",
                                                   "headRefOid": self.HEAD, "baseRefName": "river-22.2",
                                                   "isCrossRepository": False}
        for k, v in (extra or {}).items():
            gh.prs[k] = v
        return gh

    def run_check(self, rf, gh, diff_names):
        def fake_git(*args, manifest=None):
            if args[0] == "rev-parse":
                return self.HEAD + "\n"
            if args[0] == "merge-base":
                return "1" * 40 + "\n"
            if args[0] == "diff":
                self.assertIn("--no-renames", args)
                return "\0".join(diff_names) + "\0"
            return ""
        with mock.patch.object(tooling, "git", fake_git):
            return tooling.check("runner-fix", rf, gh)

    def test_fetches_before_it_reads(self):
        """Gemini minor (H4, I21): origin/river-22.2 and refs/pull/<n>/head
        are fetched before the reviewed head and the diff are read."""
        calls = []

        def fake_git(*args, manifest=None):
            calls.append(args[0])
            return {"rev-parse": self.HEAD + "\n", "merge-base": "1" * 40 + "\n"}.get(args[0], "")
        with tempfile.TemporaryDirectory() as t, mock.patch.object(tooling, "git", fake_git):
            tooling.check("runner-fix", self.record(Path(t)), self.gh())
        self.assertEqual(calls[:4], ["fetch", "rev-parse", "merge-base", "diff"])

    def test_tooling_only_passes(self):
        with tempfile.TemporaryDirectory() as t:
            rf = self.record(Path(t))
            self.assertEqual(self.run_check(rf, self.gh(), ["qa/lib/riverbatch/tick.py", "README.md"]),
                             [(12, self.HEAD)])

    def test_renamed_charter_and_xml_refused(self):
        with tempfile.TemporaryDirectory() as t:
            rf = self.record(Path(t))
            for names in (["CHARTER.md", "qa/CHARTER.md"], ["default.xml", "qa/default.xml"], ["local.xml"]):
                with self.assertRaises(tooling.Refusal):
                    self.run_check(rf, self.gh(), names)

    def test_change_record_and_fork_pr_refused(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(tooling.Refusal):
                self.run_check(self.record(Path(t), kind="change"), self.gh(), ["qa/x"])
        with tempfile.TemporaryDirectory() as t:
            gh = self.gh({("zrm625/android_vendor_lineage", 9): {"state": "OPEN", "headRefName": "runner-fix",
                                                                  "headRefOid": "5" * 40,
                                                                  "baseRefName": "river-22.2",
                                                                  "isCrossRepository": False}})
            with self.assertRaises(tooling.Refusal) as cm:
                self.run_check(self.record(Path(t)), gh, ["qa/x"])
            self.assertIn("open PRs", str(cm.exception))

    def test_moved_head_refused(self):
        with tempfile.TemporaryDirectory() as t:
            gh = self.gh()
            gh.prs[("zrm625/android_manifest", 12)]["headRefOid"] = "6" * 40
            with self.assertRaises(tooling.Refusal):
                self.run_check(self.record(Path(t)), gh, ["qa/x"])

    def test_change_merge_without_record_refuses(self):
        script = (paths.SCRIPTS / "change").read_text()
        self.assertIn('[[ -n "$record" ]] || die "change merge needs --record FILE', script)
        self.assertIn("acquire_build_lock\n    refuse_while_batch", script)
        mu = (paths.SCRIPTS / "merge-upstream").read_text()
        self.assertIn("--push is gone", mu)
        self.assertIn("upstream never merges into $FORK_BRANCH directly", mu)
        self.assertIn("acquire_build_lock\nrefuse_while_batch", (paths.SCRIPTS / "setup-forks").read_text())

    def test_change_merge_record_needs_a_file(self):
        """Gemini minor: `change merge NAME --record` with no FILE says so
        (it ran into an unbound $2 before). Runs scripts/change from a
        scratch tree with a scratch $BENCH_HOME."""
        import shutil
        with tempfile.TemporaryDirectory() as t:
            m = Path(t) / ".repo/manifests"
            shutil.copytree(paths.SCRIPTS, m / "scripts", symlinks=True)
            shutil.copytree(paths.SCRIPTS.parent / "qa/lib", m / "qa/lib",
                            ignore=shutil.ignore_patterns("__pycache__"))
            r = subprocess.run(["bash", str(m / "scripts/change"), "merge", "x", "--record"], capture_output=True,
                               text=True, timeout=60, env=dict(os.environ, BENCH_HOME=str(Path(t) / "home")))
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("--record needs a FILE", r.stderr + r.stdout)
            self.assertNotIn("unbound variable", r.stderr)


if __name__ == "__main__":
    unittest.main()
