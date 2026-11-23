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


if __name__ == "__main__":
    unittest.main()
