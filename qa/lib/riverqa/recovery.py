"""Look up an unresolved batch's merge and tag state on GitHub.

batch.json carries what the runner opened for the batch:

    "prs": [{"repo": "zrm625/android_manifest", "number": 12}, ...],
    "tag": "known-good-7",
    "tag_repos": ["https://github.com/zrm625/android_manifest", ...]

``gh`` and ``git ls-remote`` are called through ``runner`` so tests can stub
them. Any error makes the whole lookup fail (None): a failed lookup never
counts as "none merged".
"""

from __future__ import annotations

import json

from .core import run


def default_runner(cmd: list[str]):
    return run(cmd, timeout=60)


def lookup(batch: dict, runner=default_runner) -> dict | None:
    prs, tags = [], []
    tag = batch.get("tag")
    try:
        for pr in batch.get("prs") or []:
            r = runner(["gh", "pr", "view", str(pr["number"]), "--repo", pr["repo"],
                        "--json", "state,mergedAt,url"])
            if r.returncode != 0:
                return None
            data = json.loads(r.stdout)
            prs.append({"url": data.get("url"), "merged": data.get("state") == "MERGED"})
        for repo in batch.get("tag_repos") or []:
            if not tag:
                return None
            r = runner(["git", "ls-remote", "--tags", repo, f"refs/tags/{tag}"])
            if r.returncode != 0:
                return None
            tags.append({"repo": repo, "present": f"refs/tags/{tag}" in r.stdout})
    except (OSError, ValueError, KeyError, TimeoutError):
        return None
    return {"prs": prs, "tags": tags}
