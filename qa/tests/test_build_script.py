"""scripts/build's dirty() marks untracked files even when git is set to
hide them (review round 3). Runs the real function with bash and git on a
scratch repository; no phone."""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401

BUILD = Path(__file__).resolve().parents[2] / "scripts" / "build"


def dirty_fn() -> str:
    m = re.search(r"^dirty\(\) \{.*\}$", BUILD.read_text(), re.M)
    assert m, "dirty() not found in scripts/build"
    return m.group(0)


class Dirty(unittest.TestCase):
    def dirty(self, repo: str) -> str:
        return subprocess.run(["bash", "-c", dirty_fn() + '\ndirty "$1"', "x", repo],
                              capture_output=True, text=True, timeout=30, check=True).stdout.strip()

    def test_untracked_file_counts_even_if_git_hides_untracked(self):
        with tempfile.TemporaryDirectory() as t:
            git = ["git", "-C", t, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
            subprocess.run(git + ["init", "-q"], check=True)
            subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
            subprocess.run(["git", "-C", t, "config", "status.showUntrackedFiles", "no"], check=True)
            self.assertEqual(self.dirty(t), "")
            Path(t, "new-input.mk").write_text("x\n")
            self.assertEqual(self.dirty(t), "+dirty")



FAKE_LIB = r"""
TOP={top}
REPO={top}/fake-repo
FORK_GROUP=zrm625-fork
BENCH_HOME={home}
tree_for() {{ echo "$TOP"; }}
# A cooperating `change start` held the build lock and switched the fork's
# branch; this build gets the lock only after that.
acquire_build_lock() {{ git -C "$TOP/fork" checkout -q switched; }}
ensure_image() {{ IMAGE=img; }}
run_build() {{ mkdir -p "$2"; echo "0 1 0" > "$2/.bench-result"; }}
"""


class LockBeforeProvenance(unittest.TestCase):
    """Sol M10: the commit set is read under the build lock."""

    def test_commit_set_is_read_after_the_lock(self):
        import json
        import shutil
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            top, home, bin_ = t / "tree", t / "home", t / "scripts"
            (bin_ / "bench").mkdir(parents=True)
            shutil.copy(BUILD, bin_ / "build")
            (bin_ / "bench/lib.sh").write_text(FAKE_LIB.format(top=top, home=home))
            git = lambda d, *a: subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c",
                                                "user.email=t@t", "-c", "commit.gpgsign=false", *a],
                                               check=True, capture_output=True, text=True).stdout.strip()
            (top / ".repo/manifests").mkdir(parents=True)
            (top / "fork").mkdir()
            for d in (top / "fork", top / ".repo/manifests"):
                git(d, "init", "-q", "-b", "main")
                git(d, "commit", "-q", "--allow-empty", "-m", "a")
            before = git(top / "fork", "rev-parse", "HEAD")
            git(top / "fork", "checkout", "-q", "-b", "switched")
            git(top / "fork", "commit", "-q", "--allow-empty", "-m", "b")
            after = git(top / "fork", "rev-parse", "HEAD")
            git(top / "fork", "checkout", "-q", "main")
            (top / "fake-repo").write_text("#!/bin/sh\necho fork\n")
            (top / "fake-repo").chmod(0o755)
            r = subprocess.run(["bash", str(bin_ / "build"), "--batch", "b0001"], capture_output=True, text=True,
                               timeout=60, env={"PATH": "/usr/bin:/bin", "HOME": str(t)})
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            rec = json.loads((home / "out-stripped/river-build.json").read_text())
            self.assertEqual(rec["commit_set"]["fork"], after)        # not the pre-lock HEAD
            self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
