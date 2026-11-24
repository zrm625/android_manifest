"""A recorded GitHub for riverqa.recovery.lookup: the `gh` and `git
ls-remote` commands it runs, answered from one consistent model of repos,
branch tips, commits, tags and PRs. No network."""
import json
import subprocess


class GHWorld:
    """``repos[slug] = {"tip": sha | None, "tags": {tag: commit}}``;
    ``commits[(slug, sha)] = {"tree", "parents"}``; ``prs[(slug, n)]`` is a
    `gh pr view --json` object. ``annotated``: tags are annotated (peeled
    ``^{}`` lines). ``fail(cmd)`` may return a CompletedProcess to fail a
    command. Every command is recorded in ``cmds``."""

    def __init__(self, annotated=True):
        self.repos, self.commits, self.prs = {}, {}, {}
        self.annotated, self.fail, self.cmds = annotated, None, []

    def repo(self, slug, tip=None):
        return self.repos.setdefault(slug, {"tip": tip, "tags": {}})

    def __call__(self, cmd):
        self.cmds.append(cmd)
        if self.fail:
            r = self.fail(cmd)
            if r is not None:
                return r
        ok = lambda out: subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[:3] == ["gh", "pr", "view"]:
            n, repo = int(cmd[3]), cmd[5]
            p = self.prs.get((repo, n))
            if p is None:
                return subprocess.CompletedProcess(cmd, 1, "", "GraphQL: Could not resolve to a PullRequest")
            return ok(json.dumps(dict(p, number=n)))
        if cmd[:2] == ["gh", "api"]:
            path = cmd[2]
            repo, sha = path.removeprefix("repos/").split("/git/commits/")
            c = self.commits.get((repo, sha))
            if c is None:
                return subprocess.CompletedProcess(cmd, 1, "", "gh: Not Found (HTTP 404)")
            jq = cmd[cmd.index("--jq") + 1] if "--jq" in cmd else None
            if jq == ".tree.sha":
                return ok(c["tree"] + "\n")
            return ok(json.dumps({"tree": c["tree"], "parents": list(c["parents"])}))
        if cmd[:3] == ["git", "ls-remote", "--tags"]:
            repo = cmd[3].removeprefix("https://github.com/")
            tag = cmd[4].removeprefix("refs/tags/")
            at = self.repos.get(repo, {}).get("tags", {}).get(tag)
            if not at:
                return ok("")
            if self.annotated:
                return ok(f"{'f' * 40}\trefs/tags/{tag}\n{at}\trefs/tags/{tag}^{{}}\n")
            return ok(f"{at}\trefs/tags/{tag}\n")
        if cmd[:2] == ["git", "ls-remote"]:
            repo = cmd[2].removeprefix("https://github.com/")
            tip = self.repos.get(repo, {}).get("tip")
            return ok(f"{tip}\t{cmd[3]}\n" if tip else "")
        raise AssertionError(f"unexpected command {cmd}")

    # --- building a world ------------------------------------------------
    def pr(self, slug, n, head, state="OPEN", base="river-22.2", cross=False, merge_commit=None):
        self.prs[(slug, n)] = {"state": state, "url": f"https://github.com/{slug}/pull/{n}", "headRefOid": head,
                               "baseRefName": base, "isCrossRepository": cross,
                               "mergeCommit": {"oid": merge_commit} if merge_commit else None}

    def merge(self, slug, n, base, merge_commit, tree):
        """PR n of slug lands: river-22.2 moves to a merge [base, head]."""
        p = self.prs[(slug, n)]
        self.commits[(slug, merge_commit)] = {"tree": tree, "parents": [base, p["headRefOid"]]}
        self.repo(slug)["tip"] = merge_commit
        p.update(state="MERGED", mergeCommit={"oid": merge_commit})
