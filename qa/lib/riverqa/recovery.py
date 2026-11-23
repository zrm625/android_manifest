"""Look up an unresolved batch's merge and tag state on GitHub.

batch.json carries what the runner opened for the batch:

    "prs": [{"repo": "zrm625/android_manifest", "number": 12}, ...],
    "no_pr": ["<repo path>", ...],     # repos of commit_set with no PR, explicitly
    "tag": "known-good-7",
    "tag_repos": ["https://github.com/zrm625/android_manifest", ...],
    "merge_started": {"at": "...",     # flash --merge-started, before any merge or tag
                      "expected_prs": {"<repo path>": {"repo": "zrm625/x", "number": 12 | null}}}

``expected_prs`` is the complete PR set, frozen by flash --merge-started
before any merge (review round 3): every repo of the commit set, each with a
PR number or an explicit null for "no PR". Lookup and the recovery decision
use it, never a later edit of ``prs``.

``gh`` and ``git ls-remote`` are called through ``runner`` so tests can stub
them. flash --recover and flash --promote use the same lookup, and the same
decision (slots.decide_recovery). Any error makes the whole lookup fail
(None): a failed lookup never counts as "none merged".
"""

from __future__ import annotations

import json

from .core import run

PR_FIELDS = "number,state,mergedAt,url,mergeCommit,headRefOid,baseRefName"


def default_runner(cmd: list[str]):
    return run(cmd, timeout=60)


def slug(repo: str) -> str:
    """owner/name from "owner/name" or a GitHub URL."""
    return repo.removeprefix("https://github.com/").removeprefix("git@github.com:").removesuffix(".git").strip("/")


def prs_to_look_up(batch: dict) -> list[dict]:
    """The PRs to look up: the frozen expected set once merge_started has
    one, otherwise what the runner recorded in ``prs``."""
    exp = (batch.get("merge_started") or {}).get("expected_prs") if isinstance(batch.get("merge_started"), dict) \
        else None
    if exp:
        return [{"repo": e["repo"], "number": e["number"]} for _, e in sorted(exp.items())
                if e.get("number") is not None]
    return list(batch.get("prs") or [])


def tree_of(runner, repo: str, commit: str) -> str | None:
    """The tree hash of ``commit`` in ``repo`` (owner/name), or None."""
    r = runner(["gh", "api", f"repos/{repo}/git/commits/{commit}", "--jq", ".tree.sha"])
    if r.returncode != 0:
        return None
    t = r.stdout.strip()
    return t or None


def lookup(batch: dict, runner=None, slugs: dict[str, str] | None = None) -> dict | None:
    """The batch's merge and tag state, or None if any lookup failed.

    {"prs": [{"repo", "number", "url", "merged", "merge_commit", "head", "base"}],
     "tags": [{"repo", "present", "commit", "tree"}],   # commit: what the tag points at
     "tested_trees": {"<owner/name>": tree}}            # tree of each tested commit

    ``slugs`` maps commit_set paths to owner/name, for the tested trees.
    """
    runner = runner or default_runner
    slugs = slugs or {}
    prs, tags, tested = [], [], {}
    tag = batch.get("tag")
    try:
        for pr in prs_to_look_up(batch):
            r = runner(["gh", "pr", "view", str(pr["number"]), "--repo", pr["repo"], "--json", PR_FIELDS])
            if r.returncode != 0:
                return None
            data = json.loads(r.stdout)
            merged = data.get("state") == "MERGED"
            commit = (data.get("mergeCommit") or {}).get("oid")
            prs.append({"repo": slug(pr["repo"]), "number": data.get("number", pr["number"]),
                        "url": data.get("url"), "merged": merged, "merge_commit": commit if merged else None,
                        "head": data.get("headRefOid"), "base": data.get("baseRefName")})
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
            tree = None
            if commit:
                tree = tree_of(runner, slug(repo), commit)
                if tree is None:
                    return None
            tags.append({"repo": slug(repo), "present": ref in refs, "commit": commit, "tree": tree})
        tagged = {t["repo"] for t in tags if t["commit"]}
        for path, sha in sorted((batch.get("commit_set") or {}).items()):
            s = slugs.get(path)
            if s and s in tagged:
                tree = tree_of(runner, s, sha)
                if tree is None:
                    return None
                tested[s] = tree
    except (OSError, ValueError, KeyError, TimeoutError, AttributeError, TypeError):
        return None
    return {"prs": prs, "tags": tags, "tested_trees": tested}
