"""Choosing members, composing them, and the content key (R7.1, R7.3, section 1).

Composition is one change at a time across all its forks, from
``current[repo] = base``, with ``git merge-tree --write-tree`` and
``git commit-tree``; nothing is checked out. The manifest is never
composed. A change that conflicts with an earlier member stays queued; one
that conflicts with the base alone needs re-approval; one whose composed
camera set differs from known-good waits on Zak.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from riverqa import stage

from . import paths

RIVER = "device/motorola/river"
MANIFEST = ".repo/manifests"
GIT_ID = {"GIT_AUTHOR_NAME": "river-batch", "GIT_AUTHOR_EMAIL": "river-batch@localhost",
          "GIT_COMMITTER_NAME": "river-batch", "GIT_COMMITTER_EMAIL": "river-batch@localhost"}


def select(entries: list[dict], size: int) -> list[dict]:
    """R7.1: the queued entries of the next batch, in position order."""
    cands = sorted((e for e in entries if e["status"] == "queued"), key=lambda e: e["position"])
    if not cands:
        return []
    def special(e):
        f = e.get("flags") or {}
        return f.get("solo") or f.get("alone") or f.get("upstream") or e.get("kind") == "experiment"
    if special(cands[0]):
        return [cands[0]]
    out = []
    for e in cands:
        if special(e) or len(out) >= size:
            break
        out.append(e)
    return out


def content_key(trees: dict[str, str], manifest_xml: dict[str, bytes]) -> str:
    """SHA-256 over the sorted (fork path, tree) pairs and the manifest XML."""
    h = hashlib.sha256()
    for p in sorted(trees):
        h.update(f"{p} {trees[p]}\n".encode())
    for n in sorted(manifest_xml):
        h.update(f"{n} {len(manifest_xml[n])}\n".encode())
        h.update(manifest_xml[n])
    return h.hexdigest()


class GitError(RuntimeError):
    def __init__(self, path, args, rc, err):
        super().__init__(f"git -C {path} {' '.join(args)}: exit {rc}: {err.strip()[-300:]}")
        self.rc, self.err = rc, err


@dataclass
class Composed:
    members: list[dict] = field(default_factory=list)         # entries kept
    commits: dict[str, str] = field(default_factory=dict)     # touched fork path -> composed commit
    trees: dict[str, str] = field(default_factory=dict)       # every fork path -> tree
    stays_queued: list[tuple[dict, str]] = field(default_factory=list)
    needs_reapproval: list[tuple[dict, str]] = field(default_factory=list)
    waiting_zak: list[tuple[dict, str]] = field(default_factory=list)


def camera_set_at(git, commit: str) -> list[int]:
    rc, out = git.try_(RIVER, "show", f"{commit}:{stage.CAMERA_SWITCH_FILE.split(RIVER + '/', 1)[1]}")
    return stage.parse_camera_set(out if rc == 0 else None)


def merge_tree(git, path: str, ours: str, theirs: str) -> str | None:
    """The merged tree, or None on a conflict."""
    rc, out = git.try_(path, "merge-tree", "--write-tree", ours, theirs)
    if rc == 0:
        return out.split("\n", 1)[0].strip()
    if rc == 1:
        return None
    raise GitError(path, ["merge-tree"], rc, out)


def compose(git, bid: str, members: list[dict], base: dict[str, str], kg_cameras: list[int]) -> Composed:
    """R7.3. ``base``: every fork path -> base commit. ``git`` is a local git
    port (ports.Git)."""
    res = Composed()
    current = dict(base)
    for e in members:
        trial: dict[str, str] = {}
        why = None
        for pr in e["prs"]:
            path, head = pr["path"], pr["head"]
            tree = merge_tree(git, path, current[path], head)
            if tree is None:
                alone = current[path] == base[path] or merge_tree(git, path, base[path], head) is None
                why = ("conflicts with river-22.2" if alone else "conflicts with an earlier member", alone)
                break
            trial[path] = git.out(path, "commit-tree", tree, "-p", current[path], "-p", head, "-m",
                                  f"batch {bid}: merge {e['name']} ({pr['key']} at {head[:12]})",
                                  env=GIT_ID).strip()
        if why:
            (res.needs_reapproval if why[1] else res.stays_queued).append((e, why[0]))
            continue
        river = trial.get(RIVER, current[RIVER])
        cams = camera_set_at(git, river)
        if cams != sorted(kg_cameras):
            res.waiting_zak.append((e, f"camera set change: composed set {cams} differs from known-good "
                                       f"{sorted(kg_cameras)}; Zak merges it by hand"))
            continue
        current.update(trial)
        res.members.append(e)
    res.commits = {p: c for p, c in current.items() if c != base[p]}
    for p, c in current.items():
        res.trees[p] = git.out(p, "rev-parse", f"{c}^{{tree}}").strip()
    return res


def manifest_xml(rev: str, git) -> dict[str, bytes]:
    """The top-level *.xml files of the manifest at ``rev`` (section 1)."""
    out = {}
    names = git.out(MANIFEST, "ls-tree", "--name-only", rev).split("\n")
    for n in names:
        if n.endswith(".xml") and "/" not in n:
            out[n] = git.raw(MANIFEST, "show", f"{rev}:{n}")
    return out


def write_refs(git, bid: str, commits: dict[str, str]) -> None:
    """refs/river-batches/<id>/<repo> and local batch/<id> branches,
    overwriting leftovers of an unclaimed earlier try at this ID."""
    for path, c in commits.items():
        git.out(path, "update-ref", f"refs/river-batches/{bid}/{path.replace('/', '_')}", c)
        git.out(path, "branch", "-f", f"batch/{bid}", c)


def forks() -> dict[str, str]:
    """fork path -> owner/name (the 9 zrm625 forks, never the manifest)."""
    s = stage.repo_slugs(paths.MANIFEST_DIR / "default.xml")
    s.pop(MANIFEST, None)
    return s
