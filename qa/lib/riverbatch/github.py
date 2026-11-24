"""Every GitHub and remote git call of the runner, typed (R11.6).

A call ends in one of: success; **auth refusal** (HTTP 401, or 403 without
rate-limit evidence); **confirmed refusal** (another 4xx other than 429, a
GraphQL NOT_FOUND-style error, or git reporting a rejected ref); or
**unknown** (timeout, connection error, 5xx, 429, 403 with rate-limit
evidence, a malformed answer to a read, or a failure with no server
answer). The classifier is the harness's (riverqa.recovery.classify, H11).

Reads raise ``Unknown``, ``AuthRefused`` or ``Refused``; the tick turns
Unknown into exit 75 through the end-of-tick pass (R8.1), AuthRefused into a
pause, and Refused into whatever the step says (often a HALT).

``runner(cmd) -> CompletedProcess`` is injectable for tests.
"""

from __future__ import annotations

import json
import re
import subprocess
import time

from riverqa import recovery
from riverqa.core import run as host_run

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OWNER = "zrm625"
BRANCH = "river-22.2"


class GitHubError(Exception):
    def __init__(self, what: str, repo: str | None = None, status: int | None = None, line: str = ""):
        super().__init__(f"{what}: {repo or '-'}: {status or ''} {line}".strip())
        self.what, self.repo, self.status, self.line = what, repo, status, line


class Unknown(GitHubError):
    """No usable answer: retry later (exit 75)."""


class AuthRefused(GitHubError):
    """401, or 403 without rate-limit evidence: pause, never HALT."""


class Refused(GitHubError):
    """GitHub or the remote answered with a confirmed refusal."""


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    return host_run(cmd, timeout=90)


class GitHub:
    def __init__(self, runner=None, sleep=time.sleep):
        self.runner = runner or _default_runner
        self.sleep = sleep

    # --- raw ---------------------------------------------------------------
    def call(self, cmd: list[str], repo: str | None = None) -> str:
        try:
            r = self.runner(cmd)
        except (OSError, TimeoutError) as e:
            raise Unknown(" ".join(cmd[:4]), repo, None, str(e)[:300]) from e
        if r.returncode != 0:
            cls, status, line = recovery.classify(r.returncode, r.stdout or "", r.stderr or "")
            exc = {recovery.AUTH: AuthRefused, recovery.REFUSED: Refused}.get(cls, Unknown)
            raise exc(" ".join(cmd[:4]), repo, status, line)
        return r.stdout or ""

    def json(self, cmd: list[str], repo: str | None = None):
        out = self.call(cmd, repo)
        try:
            return json.loads(out)
        except ValueError as e:
            raise Unknown(" ".join(cmd[:4]), repo, None, f"malformed answer: {out[:120]!r}") from e

    # --- typed reads -------------------------------------------------------
    def rate_limit(self) -> dict:
        d = self.json(["gh", "api", "rate_limit"])
        if not isinstance(d, dict):
            raise Unknown("rate_limit", None, None, "malformed")
        return d

    def can_push(self, repo: str) -> bool:
        out = self.call(["gh", "api", f"repos/{repo}", "--jq", ".permissions.push"], repo).strip()
        if out not in ("true", "false"):
            raise Unknown("permissions", repo, None, f"malformed: {out[:60]!r}")
        return out == "true"

    def full_name(self, repo: str) -> str:
        out = self.call(["gh", "api", f"repos/{repo}", "--jq", ".full_name"], repo).strip()
        if not out:
            raise Unknown("full_name", repo, None, "empty")
        return out

    PR_FIELDS = "number,state,headRefName,headRefOid,baseRefName,isCrossRepository,mergeCommit,mergeable,url"

    def pr_view(self, repo: str, number: int) -> dict:
        d = self.json(["gh", "pr", "view", str(number), "--repo", repo, "--json", self.PR_FIELDS], repo)
        if not isinstance(d, dict) or not isinstance(d.get("state"), str) or "headRefOid" not in d:
            raise Unknown("pr view", repo, None, "malformed PR")
        return d

    def pr_list(self, repo: str, head: str, state: str = "all") -> list[dict]:
        d = self.json(["gh", "pr", "list", "--repo", repo, "--head", head, "--state", state, "--json",
                       "number,state,headRefName,headRefOid,baseRefName,isCrossRepository,url"], repo)
        if not isinstance(d, list):
            raise Unknown("pr list", repo, None, "malformed list")
        # gh matches the head branch name; keep exact matches only (I12).
        return [p for p in d if isinstance(p, dict) and p.get("headRefName") == head]

    def ls_remote(self, url: str, ref: str) -> str | None:
        """The SHA of ``ref`` on the remote, or None if absent."""
        out = self.call(["git", "ls-remote", url, ref], slug(url))
        for line in out.splitlines():
            sha, _, name = line.partition("\t")
            if name.strip() == ref:
                if not SHA_RE.match(sha):
                    raise Unknown("ls-remote", slug(url), None, f"malformed: {line[:80]!r}")
                return sha
        return None

    def commit(self, repo: str, sha: str) -> dict:
        """{"parents": [...], "tree": sha} of a commit."""
        d = self.json(["gh", "api", f"repos/{repo}/git/commits/{sha}"], repo)
        try:
            parents = [p["sha"] for p in d["parents"]]
            tree = d["tree"]["sha"]
        except (KeyError, TypeError) as e:
            raise Unknown("commit", repo, None, "malformed commit") from e
        if not SHA_RE.match(tree) or not all(SHA_RE.match(p) for p in parents):
            raise Unknown("commit", repo, None, "malformed commit")
        return {"parents": parents, "tree": tree}

    def merge_base(self, repo: str, base: str, head: str) -> str:
        out = self.call(["gh", "api", f"repos/{repo}/compare/{base}...{head}", "--jq",
                         ".merge_base_commit.sha"], repo).strip()
        if not SHA_RE.match(out):
            raise Unknown("compare", repo, None, f"malformed: {out[:60]!r}")
        return out

    def file_at(self, repo: str, path: str, ref: str) -> tuple[str | None, str | None]:
        """(blob sha, text) of ``path`` at ``ref``; (None, None) if absent
        (a 404, which parse_camera_set reads as None)."""
        try:
            d = self.json(["gh", "api", f"repos/{repo}/contents/{path}?ref={ref}"], repo)
        except Refused as e:
            if e.status == 404:
                return None, None
            raise
        if not isinstance(d, dict) or d.get("type") != "file":
            raise Unknown("contents", repo, None, "not a file")
        import base64
        try:
            text = base64.b64decode(d.get("content") or "").decode()
        except (ValueError, UnicodeDecodeError) as e:
            raise Unknown("contents", repo, None, "undecodable") from e
        return d.get("sha"), text

    def changed_files(self, repo: str, number: int) -> list[dict]:
        """[{filename, previous_filename?, status}] of a PR (R5.4 step 3, I21)."""
        d = self.json(["gh", "api", "--paginate", f"repos/{repo}/pulls/{number}/files"], repo)
        if not isinstance(d, list):
            raise Unknown("pr files", repo, None, "malformed")
        return d

    # --- writes --------------------------------------------------------------
    def comment(self, repo: str, number: int, body: str) -> None:
        self.call(["gh", "pr", "comment", str(number), "--repo", repo, "--body", body], repo)

    def comments(self, repo: str, number: int) -> list[str]:
        d = self.json(["gh", "api", "--paginate", f"repos/{repo}/issues/{number}/comments"], repo)
        if not isinstance(d, list):
            raise Unknown("comments", repo, None, "malformed")
        return [c.get("body") or "" for c in d if isinstance(c, dict)]

    def comment_once(self, repo: str, number: int, marker: str, body: str) -> None:
        """Post ``body`` only if no comment carries ``marker`` (R10.7)."""
        if any(marker in c for c in self.comments(repo, number)):
            return
        self.comment(repo, number, f"{body}\n\n{marker}")

    def close_pr(self, repo: str, number: int) -> None:
        self.call(["gh", "pr", "close", str(number), "--repo", repo], repo)

    def reopen_pr(self, repo: str, number: int) -> None:
        self.call(["gh", "pr", "reopen", str(number), "--repo", repo], repo)

    def create_pr(self, repo: str, head: str, title: str, body: str) -> None:
        self.call(["gh", "pr", "create", "--repo", repo, "--base", BRANCH, "--head", head, "--title", title,
                   "--body", body], repo)

    def merge_pr(self, repo: str, number: int, head_sha: str) -> None:
        self.call(["gh", "pr", "merge", str(number), "--repo", repo, "--merge", "--match-head-commit", head_sha],
                  repo)


def slug(repo: str) -> str:
    return recovery.slug(repo)


def url(repo_slug: str) -> str:
    return f"https://github.com/{repo_slug}"
