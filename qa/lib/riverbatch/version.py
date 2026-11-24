"""Runner versions: two hashes (R2.2).

Each is SHA-256 over the listed files' paths and bytes, sorted by path.
Under ``qa/lib/riverbatch/`` only the tracked ``*.py``, ``*.json`` and
``*.xml`` files count (``git ls-files``), never ``__pycache__`` or untracked
files.

- ``runner_build``: scripts/batch, qa/lib/riverbatch/ (as above),
  scripts/build, scripts/bench/lib.sh, scripts/bench/Containerfile,
  scripts/common.sh. A change needs both parts of qualification (R14).
- ``runner_tools``: scripts/flash, scripts/bench/device-bench,
  scripts/device-lock, scripts/phone-setup, scripts/change,
  scripts/merge-upstream, scripts/setup-forks. A change needs part 2.

The harness version is riverqa.qa.harness_version(); nothing here is an
input to it (R2.1).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from . import paths

BUILD_FILES = ["scripts/batch", "scripts/build", "scripts/bench/lib.sh", "scripts/bench/Containerfile",
               "scripts/common.sh"]
TOOLS_FILES = ["scripts/flash", "scripts/bench/device-bench", "scripts/device-lock", "scripts/phone-setup",
               "scripts/change", "scripts/merge-upstream", "scripts/setup-forks"]


def package_files(manifest: Path | None = None) -> list[str]:
    m = manifest or paths.MANIFEST_DIR
    r = subprocess.run(["git", "-C", str(m), "ls-files", "-z", "--", "qa/lib/riverbatch"], capture_output=True,
                       timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git ls-files failed in {m}")
    names = [n for n in r.stdout.decode().split("\0") if n]
    return sorted(n for n in names if n.endswith((".py", ".json", ".xml")) and "__pycache__" not in n)


def digest(files: list[str], manifest: Path | None = None) -> str:
    m = manifest or paths.MANIFEST_DIR
    h = hashlib.sha256()
    for f in sorted(files):
        p = m / f
        h.update(f.encode() + b"\0")
        h.update(p.read_bytes() if p.exists() else b"<absent>")
        h.update(b"\0")
    return h.hexdigest()[:16]


def runner_build(manifest: Path | None = None) -> str:
    return digest(BUILD_FILES + package_files(manifest), manifest)


def runner_tools(manifest: Path | None = None) -> str:
    return digest(TOOLS_FILES, manifest)


def harness() -> str:
    from riverqa.qa import harness_version
    return harness_version()


def current(manifest: Path | None = None) -> dict:
    return {"harness": harness(), "runner_build": runner_build(manifest), "runner_tools": runner_tools(manifest)}
