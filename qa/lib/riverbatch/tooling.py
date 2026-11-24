"""`change merge NAME --record FILE` (H4, R5.8, E7, S-B3): merge a
council-reviewed, tooling-only manifest PR, pinned to the reviewed head.

    python3 -I qa/lib/riverbatch/tooling.py NAME RECORD_FILE

Refused (exit 1, nothing merged) unless:

- the record is a valid, unanimous ``kind: tooling`` record (R5.2, I14)
  whose ``branch`` is NAME;
- every PR it names is in zrm625/android_manifest;
- the open PRs with head branch NAME, found on GitHub in the 9 forks and the
  manifest (whether or not a local branch exists), are exactly the record's,
  each on river-22.2, not cross-repository, at the reviewed head;
- after fetching ``origin/river-22.2`` and ``refs/pull/<n>/head`` (I21),
  every path of ``git diff --no-renames --name-only -z <merge-base>
  <reviewed head>`` is tooling: under ``qa/``, ``scripts/`` or
  ``workspace/``, or a ``*.md`` other than ``CHARTER.md``. ``--no-renames``
  lists both sides of a rename, so moving ``CHARTER.md`` or ``default.xml``
  into ``qa/`` is refused (S-B3).

Then the synchronous REST merge at the reviewed head (``GitHub.merge_pr``:
``PUT repos/{repo}/pulls/<n>/merge`` with ``sha``; never a merge queue or
auto-merge).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from riverbatch import compose, eligibility, paths, records  # noqa: E402
from riverbatch.github import BRANCH, GitHub, GitHubError  # noqa: E402

MANIFEST_REPO = "zrm625/android_manifest"


class Refusal(Exception):
    pass


def git(*args: str, manifest: Path | None = None) -> str:
    r = subprocess.run(["git", "-C", str(manifest or paths.MANIFEST_DIR), *args], capture_output=True, timeout=300)
    if r.returncode != 0:
        raise Refusal(f"git {' '.join(args[:3])} failed: {r.stderr.decode(errors='replace').strip()[-200:]}")
    return r.stdout.decode(errors="replace")


def tooling_paths_ok(names: list[str]) -> list[str]:
    """The offending paths (H4): not tooling, or a top-level manifest XML."""
    return sorted(n for n in names if not eligibility.tooling_path(n) or ("/" not in n and n.endswith(".xml")))


def check(name: str, record_file: Path, gh: GitHub, manifest: Path | None = None) -> list[tuple[int, str]]:
    """Every check; returns [(PR number, reviewed head)] to merge."""
    rec, _raw = records.load_draft(record_file)
    errs = records.validate(rec, record_file.parent, kinds=("tooling",))
    if errs:
        raise Refusal("invalid tooling record: " + "; ".join(errs[:6]))
    if rec.get("branch") != name:
        raise Refusal(f"the record's branch is {rec.get('branch')!r}, not {name!r}")
    heads = records.approved_heads(rec)
    for k in heads:
        if records.split_key(k)[0] != MANIFEST_REPO:
            raise Refusal(f"{k}: change merge merges only {MANIFEST_REPO} PRs; fork PRs go through the queue")
    repos = sorted(set(compose.forks().values()) | {MANIFEST_REPO})
    found = {}
    for repo in repos:
        for p in gh.pr_list(repo, name, "open"):
            found[f"{repo}#{p['number']}"] = p
    if set(found) != set(heads):
        raise Refusal(f"the open PRs from {name} are {sorted(found)}, the record names {sorted(heads)}")
    plan = []
    for k, head in sorted(heads.items()):
        p = found[k]
        if p.get("baseRefName") != BRANCH or p.get("isCrossRepository") is not False:
            raise Refusal(f"{k}: not a same-repository PR into {BRANCH}")
        if p.get("headRefOid") != head:
            raise Refusal(f"{k}: head is {str(p.get('headRefOid'))[:12]}, the council reviewed {head[:12]}")
        n = records.split_key(k)[1]
        git("fetch", "-q", "origin", BRANCH, f"+refs/pull/{n}/head:refs/river-tooling/{n}", manifest=manifest)
        if git("rev-parse", f"refs/river-tooling/{n}", manifest=manifest).strip() != head:
            raise Refusal(f"{k}: refs/pull/{n}/head is not the reviewed head")
        mb = git("merge-base", f"origin/{BRANCH}", head, manifest=manifest).strip()
        names = [x for x in git("diff", "--no-renames", "--name-only", "-z", mb, head, manifest=manifest).split("\0")
                 if x]
        bad = tooling_paths_ok(names)
        if bad:
            raise Refusal(f"{k} touches paths outside the tooling list: {', '.join(bad[:10])}")
        plan.append((n, head))
    return plan


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: tooling.py NAME RECORD_FILE", file=sys.stderr)
        return 2
    name, rf = argv[0], Path(argv[1])
    gh = GitHub()
    try:
        plan = check(name, rf, gh)
        for n, head in plan:
            gh.merge_pr(MANIFEST_REPO, n, head)
            print(f"  merged  https://github.com/{MANIFEST_REPO}/pull/{n} at {head[:12]}")
    except (Refusal, records.RecordError, GitHubError) as e:
        print(f"error: {e}. Nothing more was merged.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    os.environ.setdefault("BENCH_HOME", "/mnt/storage/lineageos-bench")
    sys.exit(main(sys.argv[1:]))
