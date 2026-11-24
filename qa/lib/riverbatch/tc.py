"""The whole-tree check, TC (R7.4).

TC passes only if the effective manifest is the stub that includes
``default.xml`` with no local manifests; every fork's HEAD is its expected
commit, the manifest's is its expected commit, and every other project's
HEAD is its ``default.xml`` revision (a SHA); ``git status --porcelain
--ignored=matching --untracked-files=normal`` is empty in every project and
the manifest except ``tc_allowlist.json``; no assume-unchanged or
skip-worktree entry; ``.repo/project.list`` equals the manifest's project
paths; nothing outside the projects but the allowed entries and the
``copyfile``/``linkfile`` destinations (checked); and no git-lfs pointer
files. It runs over the projects with 8 workers and records its duration.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

MANIFEST = ".repo/manifests"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TOP_ALLOWED = {".repo", ".repo-bootstrap", ".claude", "out"}
LINKS = {"AGENTS.md": ".repo/manifests/AGENTS.md", "CLAUDE.md": ".repo/manifests/AGENTS.md"}


@dataclass
class Project:
    path: str
    revision: str
    copyfiles: list = field(default_factory=list)   # (src, dest)
    linkfiles: list = field(default_factory=list)


@dataclass
class Result:
    ok: bool
    blocking: list[str]
    seconds: float

    def summary(self, n: int = 12) -> str:
        more = f" (+{len(self.blocking) - n} more)" if len(self.blocking) > n else ""
        return "; ".join(self.blocking[:n]) + more


def parse_manifest(xml_path: Path) -> list[Project]:
    root = ET.parse(xml_path).getroot()
    default_rev = (root.find("default").get("revision") if root.find("default") is not None else None) or ""
    out = []
    for p in root.iter("project"):
        path = p.get("path") or p.get("name")
        proj = Project(path, p.get("revision") or default_rev)
        for c in p.findall("copyfile"):
            proj.copyfiles.append((c.get("src"), c.get("dest")))
        for c in p.findall("linkfile"):
            proj.linkfiles.append((c.get("src"), c.get("dest")))
        out.append(proj)
    return out


def load_allowlist() -> dict:
    return json.loads((paths.PKG / "tc_allowlist.json").read_text())


def _git(top: Path, path: str, *args, timeout=300) -> subprocess.CompletedProcess:
    # GIT_OPTIONAL_LOCKS=0: a check never refreshes an index.
    return subprocess.run(["git", "-C", str(top / path), *args], capture_output=True, text=True, timeout=timeout,
                          env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))


def _pycache_ok(top: Path, proj: str, d: str) -> list[str]:
    """An allowlisted __pycache__/ may hold only *.cpython-*.pyc files named
    after a .py in its parent."""
    bad = []
    base = top / proj / d
    parent = base.parent
    try:
        names = os.listdir(base)
    except OSError:
        return []
    for n in names:
        m = re.match(r"^(.+)\.cpython-[0-9]+[^.]*\.pyc$", n)
        if not m or not (parent / f"{m.group(1)}.py").is_file() or not (base / n).is_file():
            bad.append(f"{proj}/{d}{n}")
    return bad


def status_problems(top: Path, path: str, allow: list[str], nested: set[str]) -> list[str]:
    r = _git(top, path, "status", "--porcelain", "--ignored=matching", "--untracked-files=normal")
    if r.returncode != 0:
        return [f"{path}: git status failed: {r.stderr.strip()[-200:]}"]
    bad = []
    for line in r.stdout.splitlines():
        if len(line) < 4:
            continue
        name = line[3:]
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        name = name.strip('"')
        full = f"{path}/{name}".rstrip("/")
        if full in nested or any(n.startswith(full + "/") for n in nested):
            continue          # a nested project, or a directory holding one
        # Gemini GM7: the allowlist covers untracked (??) and ignored (!!)
        # entries only; a tracked file changed under an allowlisted prefix
        # is a disturbed tree. An allowlisted __pycache__/ is checked file by
        # file however git reports it (with or without a .gitignore).
        code = line[:2]
        hit = None
        if code in ("??", "!!"):
            hit = next((a for a in allow if (name.startswith(a) if a.endswith("/") else name == a)), None)
        if hit is None:
            bad.append(f"{path}: {code.strip() or '??'} {name}")
        elif hit.endswith("__pycache__/"):
            bad.extend(_pycache_ok(top, path, hit))
    return bad


def flags_problems(top: Path, path: str) -> list[str]:
    r = _git(top, path, "ls-files", "-v")
    if r.returncode != 0:
        return [f"{path}: git ls-files failed"]
    bad = []
    for line in r.stdout.splitlines():
        tag = line[:1]
        if tag == "S" or (tag.isalpha() and tag.islower()):
            bad.append(f"{path}: {line[2:]} is {'skip-worktree' if tag == 'S' else 'assume-unchanged'}")
    return bad


def lfs_problems(top: Path, path: str) -> list[str]:
    attrs = top / path / ".gitattributes"
    try:
        if "filter=lfs" not in attrs.read_text(errors="replace"):
            return []
    except OSError:
        return []
    r = _git(top, path, "lfs", "ls-files")
    if r.returncode != 0:
        return [f"{path}: git lfs ls-files failed: {r.stderr.strip()[-200:]}"]
    # `git lfs ls-files`: "<oid> <*|-> <path>"; "-" is a pointer, never
    # pulled (Gemini minor: match the token, not " - " in a file name).
    out = []
    for ln in r.stdout.splitlines():
        parts = ln.split(None, 2)
        if len(parts) == 3 and parts[1] == "-":
            out.append(f"{path}: LFS pointer {parts[2]}")
    return out


def head_problem(top: Path, path: str, want: str) -> list[str]:
    r = _git(top, path, "rev-parse", "HEAD")
    got = r.stdout.strip()
    if r.returncode != 0 or got != want:
        return [f"{path}: HEAD {got[:12] or '?'} is not {want[:12]}"]
    return []


def outside_problems(top: Path, projects: list[Project]) -> list[str]:
    """Nothing outside the projects but the allowed entries (item 6)."""
    proj_paths = {p.path for p in projects}
    ancestors = set()
    for p in proj_paths:
        parts = p.split("/")
        for i in range(1, len(parts)):
            ancestors.add("/".join(parts[:i]))
    dests = {}
    for p in projects:
        for src, dest in p.copyfiles:
            dests[dest] = ("copy", f"{p.path}/{src}")
        for src, dest in p.linkfiles:
            dests[dest] = ("link", f"{p.path}/{src}")
    bad = []

    def check_dir(rel: str):
        d = top / rel if rel else top
        for n in sorted(os.listdir(d)):
            r = f"{rel}/{n}" if rel else n
            if r in proj_paths:
                continue
            if r in ancestors:
                check_dir(r)
                continue
            if r in dests:
                kind, src = dests[r]
                f = top / r
                if kind == "copy":
                    if f.is_symlink() or not f.is_file() or f.read_bytes() != (top / src).read_bytes():
                        bad.append(f"{r}: copyfile destination differs from {src}")
                else:
                    want = os.path.relpath(top / src, (top / r).parent)
                    if not f.is_symlink() or os.readlink(f) != want:
                        bad.append(f"{r}: linkfile destination is not a link to {want}")
                continue
            if not rel:
                if n in TOP_ALLOWED:
                    if n == "out" and (top / "out").is_dir() and os.listdir(top / "out"):
                        bad.append("out/ is not empty")
                    continue
                if n in LINKS:
                    f = top / n
                    if not f.is_symlink() or os.readlink(f) != LINKS[n]:
                        bad.append(f"{n}: not the link to {LINKS[n]}")
                    continue
            bad.append(f"{r}: outside every project")
    check_dir("")
    for dest in dests:
        if not os.path.lexists(top / dest):
            bad.append(f"{dest}: {dests[dest][0]}file destination missing")
    return bad


def check(expected: dict[str, str], top: Path | None = None, workers: int = 8) -> Result:
    """``expected``: every fork path and the manifest -> its expected HEAD."""
    t0 = time.monotonic()
    top = top or paths.TOP
    bad: list[str] = []
    stub = (paths.PKG / "manifest_stub.xml").read_bytes()
    try:
        if (top / ".repo/manifest.xml").read_bytes() != stub:
            bad.append(".repo/manifest.xml is not the stub that includes default.xml")
    except OSError as e:
        bad.append(f".repo/manifest.xml: {e}")
    lm = top / ".repo/local_manifests"
    if lm.exists() and (not lm.is_dir() or os.listdir(lm)):
        bad.append(".repo/local_manifests is not empty")
    projects = parse_manifest(top / MANIFEST / "default.xml")
    proj_paths = [p.path for p in projects]
    try:
        listed = [ln for ln in (top / ".repo/project.list").read_text().splitlines() if ln.strip()]
    except OSError:
        listed = []
    if sorted(listed) != sorted(proj_paths):
        bad.append(".repo/project.list does not list exactly the manifest's projects")
    allow = load_allowlist()
    nested_all = set(proj_paths)

    def one(p: Project) -> list[str]:
        out = []
        want = expected.get(p.path)
        if want is None:
            if not SHA_RE.match(p.revision or ""):
                return [f"{p.path}: default.xml revision {p.revision!r} is not a SHA"]
            want = p.revision
        out += head_problem(top, p.path, want)
        nested = {n for n in nested_all if n.startswith(p.path + "/")}
        out += status_problems(top, p.path, allow.get("projects", {}).get(p.path, []), nested)
        out += flags_problems(top, p.path)
        out += lfs_problems(top, p.path)
        return out

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for probs in ex.map(one, projects):
            bad.extend(probs)
    if MANIFEST in expected:
        bad += head_problem(top, MANIFEST, expected[MANIFEST])
    bad += status_problems(top, MANIFEST, allow.get("manifest", []), set())
    bad += flags_problems(top, MANIFEST)
    bad += outside_problems(top, projects)
    return Result(not bad, bad, time.monotonic() - t0)


def tools_problems(top: Path | None = None) -> list[str]:
    """Preflight item 1: git >= 2.38 and git-lfs, with .repo-bootstrap/bin
    first on PATH, and gh with ``api --slurp``."""
    top = top or paths.TOP
    bad = []
    boot = str(top / ".repo-bootstrap" / "bin")
    if os.environ.get("PATH", "").split(":")[0] != boot:
        bad.append(f"{boot} is not first on PATH")
    try:
        v = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30).stdout
        m = re.search(r"(\d+)\.(\d+)", v)
        if not m or (int(m.group(1)), int(m.group(2))) < (2, 38):
            bad.append(f"git {v.strip()} is older than 2.38")
        if subprocess.run(["git", "lfs", "version"], capture_output=True, timeout=30).returncode != 0:
            bad.append("git lfs is not available")
    except (OSError, subprocess.TimeoutExpired) as e:
        bad.append(f"git: {e}")
    try:                                                   # Opus minor: GitHub.pages needs `gh api --slurp`
        h = subprocess.run(["gh", "api", "--help"], capture_output=True, text=True, timeout=30)
        if "--slurp" not in (h.stdout or "") + (h.stderr or ""):
            bad.append("gh does not support `gh api --slurp` (needs gh 2.48 or later)")
    except (OSError, subprocess.TimeoutExpired) as e:
        bad.append(f"gh: {e}")
    return bad
