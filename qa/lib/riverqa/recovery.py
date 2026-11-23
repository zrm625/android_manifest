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
decision (slots.decide_recovery).

**Typed lookup** (H11, batch-runner design S-M7). ``lookup`` returns a
``Lookup(outcome, data, evidence)``:

- ``ok``: ``data`` is the dict below;
- ``transport``: a timeout, an ``OSError``, no server answer, HTTP 5xx or
  429, 403 with rate-limit evidence, or a GraphQL rate-limit or server error;
- ``refused``: ``gh`` or ``git`` reports HTTP 401, 403 without rate-limit
  evidence, 404 or 422, or another GraphQL error (NOT_FOUND, FORBIDDEN,
  "Could not resolve to ...");
- ``malformed``: unparsable JSON, a missing field, or a non-SHA tree.

``evidence`` is ``{"repo", "status", "line"}`` for anything but ``ok``. A
non-``ok`` lookup never counts as "none merged". ``classify`` is shared with
the batch runner (qa/lib/riverbatch/github.py).
"""

from __future__ import annotations

import json
import re
from typing import NamedTuple

from .core import run
from .stage import SHA_RE

PR_FIELDS = "number,state,mergedAt,url,mergeCommit,headRefOid,baseRefName"

OK, TRANSPORT, REFUSED, MALFORMED = "ok", "transport", "refused", "malformed"
AUTH = "auth"      # classify() only: a refusal that is HTTP 401, or 403 without rate-limit evidence


class Lookup(NamedTuple):
    outcome: str
    data: dict | None
    evidence: dict | None


class Malformed(ValueError):
    pass


_STATUS_RE = re.compile(r"\(HTTP (\d{3})\)|HTTP[/ ](?:\d(?:\.\d)? )?(\d{3})\b|status(?: code)?:? (\d{3})\b", re.I)
_RATE_RE = re.compile(r"rate.?limit|secondary rate|x-ratelimit-remaining: 0|retry-after|abuse detection", re.I)
_TRANSPORT_RE = re.compile(r"timed? ?out|timeout|connection (refused|reset)|could not resolve host|"
                           r"no such host|network is unreachable|temporary failure in name resolution|"
                           r"tls handshake|unexpected eof|\bEOF\b|i/o timeout|dial tcp|"
                           r"something went wrong|server error|bad gateway|service unavailable|"
                           r"the remote end hung up|early eof|rpc failed", re.I)
_AUTH_RE = re.compile(r"bad credentials|authentication failed|requires authentication|"
                      r"gh auth login|not logged in|invalid username or password", re.I)
_GRAPHQL_REFUSED_RE = re.compile(r"GraphQL: .*(could not resolve to|not_found|forbidden|"
                                 r"must have|does not have|not accessible|unprocessable)", re.I)
_GIT_REFUSED_RE = re.compile(r"repository not found|\[rejected\]|\[remote rejected\]|"
                             r"! \[.*\]|permission to .* denied|does not appear to be a git repository|"
                             r"stale info|protected branch", re.I)


def classify(returncode: int | None, stdout: str = "", stderr: str = "") -> tuple[str, int | None, str]:
    """(class, HTTP status or None, first error line) of a failed ``gh`` or
    ``git`` call: ``transport``, ``auth`` or ``refused`` (R11.6, H11). A
    failure with no recognizable server answer is ``transport``."""
    text = f"{stderr}\n{stdout}"
    line = next((ln.strip() for ln in (stderr or stdout or "").splitlines() if ln.strip()), "")[:300]
    m = _STATUS_RE.search(text)
    status = next((int(g) for g in m.groups() if g), None) if m else None
    rate = bool(_RATE_RE.search(text))
    if status is not None:
        if status >= 500 or status == 429:
            return TRANSPORT, status, line
        if status == 403:
            return (TRANSPORT if rate else AUTH), status, line
        if status == 401:
            return AUTH, status, line
        if 400 <= status < 500:
            return REFUSED, status, line
    if rate:
        return TRANSPORT, status, line
    if _AUTH_RE.search(text):
        return AUTH, 401, line
    if "GraphQL:" in text:
        if _GRAPHQL_REFUSED_RE.search(text):
            return REFUSED, status, line
        return TRANSPORT, status, line
    if _GIT_REFUSED_RE.search(text) and not _TRANSPORT_RE.search(text):
        return REFUSED, status, line
    return TRANSPORT, status, line


class _Failed(Exception):
    def __init__(self, outcome: str, evidence: dict):
        super().__init__(outcome)
        self.outcome, self.evidence = outcome, evidence


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


def _call(runner, cmd: list[str], repo: str):
    try:
        r = runner(cmd)
    except (OSError, TimeoutError) as e:
        raise _Failed(TRANSPORT, {"repo": repo, "status": None, "line": str(e)[:300]}) from e
    if r.returncode != 0:
        cls, status, line = classify(r.returncode, r.stdout or "", r.stderr or "")
        raise _Failed(REFUSED if cls == AUTH else cls, {"repo": repo, "status": status, "line": line})
    return r


def tree_of(runner, repo: str, commit: str) -> str:
    """The tree hash of ``commit`` in ``repo`` (owner/name). Raises _Failed."""
    r = _call(runner, ["gh", "api", f"repos/{repo}/git/commits/{commit}", "--jq", ".tree.sha"], repo)
    t = r.stdout.strip()
    # Only a real tree hash (review round 4): an error page or a partial
    # answer with exit 0 is malformed.
    if not SHA_RE.match(t):
        raise _Failed(MALFORMED, {"repo": repo, "status": None, "line": f"not a tree hash: {t[:80]!r}"})
    return t


def lookup(batch: dict, runner=None, slugs: dict[str, str] | None = None) -> Lookup:
    """The batch's merge and tag state, typed (H11).

    data: {"prs": [{"repo", "number", "url", "state", "merged", "merge_commit", "head", "base"}],
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
            repo = slug(str(pr.get("repo")))
            r = _call(runner, ["gh", "pr", "view", str(pr["number"]), "--repo", pr["repo"], "--json", PR_FIELDS],
                      repo)
            try:
                data = json.loads(r.stdout)
                state = data["state"]
                if not isinstance(data, dict) or not isinstance(state, str):
                    raise Malformed("not a PR object")
            except (ValueError, KeyError, TypeError) as e:
                raise _Failed(MALFORMED, {"repo": repo, "status": None, "line": f"gh pr view: {e}"[:300]}) from e
            merged = state == "MERGED"
            commit = (data.get("mergeCommit") or {}).get("oid")
            prs.append({"repo": repo, "number": data.get("number", pr["number"]),
                        "url": data.get("url"), "state": state, "merged": merged,
                        "merge_commit": commit if merged else None,
                        "head": data.get("headRefOid"), "base": data.get("baseRefName")})
        for repo in batch.get("tag_repos") or []:
            if not tag:
                raise _Failed(MALFORMED, {"repo": slug(repo), "status": None, "line": "tag_repos without a tag"})
            ref = f"refs/tags/{tag}"
            r = _call(runner, ["git", "ls-remote", "--tags", repo, ref, ref + "^{}"], slug(repo))
            refs = dict(reversed(line.split("\t", 1)) for line in r.stdout.splitlines() if "\t" in line)
            # An annotated tag's commit is its peeled ref; a lightweight tag's is the ref.
            commit = refs.get(ref + "^{}") or refs.get(ref)
            tree = tree_of(runner, slug(repo), commit) if commit else None
            tags.append({"repo": slug(repo), "present": ref in refs, "commit": commit, "tree": tree})
        tagged = {t["repo"] for t in tags if t["commit"]}
        for path, sha in sorted((batch.get("commit_set") or {}).items()):
            s = slugs.get(path)
            if s and s in tagged:
                tested[s] = tree_of(runner, s, sha)
    except _Failed as f:
        return Lookup(f.outcome, None, f.evidence)
    except (ValueError, KeyError, AttributeError, TypeError) as e:
        return Lookup(MALFORMED, None, {"repo": None, "status": None, "line": str(e)[:300]})
    return Lookup(OK, {"prs": prs, "tags": tags, "tested_trees": tested}, None)
