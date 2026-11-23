"""Look up an unresolved batch's merge and tag state on GitHub.

batch.json carries what the runner opened for the batch:

    "prs": [{"repo": "zrm625/android_manifest", "number": 12}, ...],
    "tag": "known-good-7",
    "tag_repos": ["https://github.com/zrm625/android_manifest", ...],
    "merge_started": {"at": "..."}     # flash --merge-started, before any merge or tag

``gh`` and ``git ls-remote`` are called through ``runner`` so tests can stub
them. flash --recover and flash --promote use the same lookup, and the same
decision (slots.decide_recovery). Any error makes the whole lookup fail (None): a failed lookup never
counts as "none merged".
"""

from __future__ import annotations

import json

from .core import run


def default_runner(cmd: list[str]):
    return run(cmd, timeout=60)


def slug(repo: str) -> str:
    """owner/name from "owner/name" or a GitHub URL."""
    return repo.removeprefix("https://github.com/").removeprefix("git@github.com:").removesuffix(".git").strip("/")


def lookup(batch: dict, runner=None) -> dict | None:
    """The batch's merge and tag state, or None if any lookup failed.

    {"prs": [{"repo", "url", "merged", "merge_commit", "head"}],   # head: the PR's head SHA
     "tags": [{"repo", "present", "commit"}]}  # commit: what the tag points at
    """
    runner = runner or default_runner
    prs, tags = [], []
    tag = batch.get("tag")
    try:
        for pr in batch.get("prs") or []:
            r = runner(["gh", "pr", "view", str(pr["number"]), "--repo", pr["repo"],
                        "--json", "state,mergedAt,url,mergeCommit,headRefOid"])
            if r.returncode != 0:
                return None
            data = json.loads(r.stdout)
            merged = data.get("state") == "MERGED"
            commit = (data.get("mergeCommit") or {}).get("oid")
            prs.append({"repo": slug(pr["repo"]), "url": data.get("url"), "merged": merged,
                        "merge_commit": commit if merged else None, "head": data.get("headRefOid")})
        for repo in batch.get("tag_repos") or []:
            if not tag:
                return None
            ref = f"refs/tags/{tag}"
            r = runner(["git", "ls-remote", "--tags", repo, ref, ref + "^{}"])
            if r.returncode != 0:
                return None
            refs = dict(reversed(line.split("\t", 1)) for line in r.stdout.splitlines() if "\t" in line)
            # An annotated tag's commit is its peeled ref; a lightweight tag's is the ref.
            commit = refs.get(ref + "^{}") or refs.get(ref)
            tags.append({"repo": slug(repo), "present": ref in refs, "commit": commit})
    except (OSError, ValueError, KeyError, TimeoutError, AttributeError):
        return None
    return {"prs": prs, "tags": tags}
