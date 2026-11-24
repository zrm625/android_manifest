"""Merge, tag and promotion (R10), and the merge reconciliation (R10.4a,
I4, I5). Every step is idempotent: it reads the remote state before it acts,
so a resumed forward path (R11.2 step 3) resolves a pending intent by doing
the same step again."""

from __future__ import annotations

import json

from riverqa import bench as rbench, regression
from riverqa.core import now_iso, write_json

from . import compose, outcomes as O, paths, records, state as S
from .ctx import (Abort, Ctx, Halt, Pause, Refused, Unknown, clean, commit_intent, branch, remote_git, update_running)
from .github import BRANCH, url

MANIFEST = compose.MANIFEST
MANIFEST_SLUG = "zrm625/android_manifest"
MARK = "<!-- river-batch {bid} -->"


def slugs() -> dict[str, str]:
    from riverqa import stage
    return stage.repo_slugs(paths.MANIFEST_DIR / "default.xml")


def tested(run: dict, path: str) -> str:
    return run["composed"][path]["sha"]


def tested_tree(run: dict, path: str) -> str:
    return run["composed"][path]["tree"]


# --- R10.1 ------------------------------------------------------------------------------

def merge_rule(run: dict, batch: dict, outcome: dict, versions_now: dict, qualified: bool) -> list[str]:
    why = []
    if outcome.get("class") != O.PASS:
        why.append("class is not pass")
    if batch.get("kind") != "batch" or batch.get("experiment") is not False or batch.get("camera_set_change"):
        why.append("batch.json is not a plain batch")
    if run.get("purpose") != "batch" or not run.get("members"):
        why.append("not a batch run with members")
    for k in ("harness", "runner_build", "runner_tools"):
        if versions_now.get(k) != run["versions"].get(k):
            why.append(f"{k} changed since claim")
    if not qualified:
        why.append("not qualified")
    return why


# --- R10.2 open PRs ------------------------------------------------------------------------

def push_branches(ctx: Ctx, run: dict) -> None:
    bid = run["id"]
    sl = slugs()
    for p in run["touched"]:
        want = tested(run, p)
        ref = f"refs/heads/batch/{bid}"
        got = ctx.gh.ls_remote(url(sl[p]), ref)
        if got == want:
            continue
        if got is not None:
            raise Halt(f"{sl[p]} batch/{bid} is at {got[:12]}, not the composed {want[:12]}")
        try:
            remote_git(ctx, p, "push", "zrm625", f"{want}:{ref}")
        except Refused:
            got = ctx.gh.ls_remote(url(sl[p]), ref)
            if got != want:
                raise Halt(f"push of batch/{bid} to {sl[p]} refused and the branch is at {got}")
        except Unknown:
            if ctx.gh.ls_remote(url(sl[p]), ref) != want:
                raise


def open_prs(ctx: Ctx, run: dict) -> dict[str, int]:
    """Find or open one batch PR per touched fork; void a found PR that is
    closed, merged or at another head (before merge_started)."""
    bid = run["id"]
    sl = slugs()
    names = ", ".join(m["name"] for m in run["members"])
    prs = {}
    body = pr_body(run, S.read()["entries"], ctx.batch_json(bid) or {})
    for p in run["touched"]:
        found = ctx.gh.pr_list(sl[p], f"batch/{bid}", "all")
        if not found:
            ctx.gh.create_pr(sl[p], f"batch/{bid}", f"batch {bid}: {names}", body)
            found = ctx.gh.pr_list(sl[p], f"batch/{bid}", "all")
            if not found:
                raise Unknown("pr create", sl[p], None, "the new PR is not listed yet")
        if len(found) != 1:
            raise Abort({"class": O.VOID, "reason": f"{sl[p]}: {len(found)} PRs from batch/{bid}"})
        pr = found[0]
        if pr.get("state") != "OPEN" or pr.get("headRefOid") != tested(run, p):
            raise Abort({"class": O.VOID, "reason": f"{sl[p]}#{pr.get('number')} is {pr.get('state')} at "
                                                   f"{str(pr.get('headRefOid'))[:12]}"})
        prs[p] = int(pr["number"])
    return prs


def pr_body(run: dict, entries: list[dict], batch: dict) -> str:
    """The batch PR's description (Sol minor): each change with its PRs at
    their approved heads and its council record, then what the phone
    measured."""
    lines = [f"Batch `{run['id']}`, tested on the river test phone by the batch runner.", "", "Changes:"]
    by_name = {e["name"]: e for e in entries}
    for m in run["members"]:
        e = by_name.get(m["name"]) or {}
        lines.append(f"- `{m['name']}`, council record `{str(e.get('record_sha256'))[:16]}` "
                     f"(runner/council/{m['name']}/)")
        for pr in e.get("prs") or []:
            repo, n = records.split_key(pr["key"])
            lines.append(f"  - {url(repo)}/pull/{n} at `{pr['head']}`")
    qa = run.get("qa") or {}
    lines += ["", f"QA run: `{qa.get('run_id')}` (exit {qa.get('exit')}, eligible {qa.get('eligible')})"]
    rows = rbench.attempt_rows(batch) if batch else []
    if rows:
        lines.append("Measured on the phone (one row per boot):")
        for r in rows:
            ram = r.get("used_ram_kb")
            lines.append(f"- `{r.get('run_id')}`: boot {r.get('host_boot_seconds')} s, used RAM "
                         f"{float(ram) / 1024:.0f} MB, /system {r.get('system_used_kb')} KB, "
                         f"/vendor {r.get('vendor_used_kb')} KB" if ram else f"- `{r.get('run_id')}`")
    lines += [f"Content key: `{run.get('content_key')}`", "", MARK.format(bid=run["id"])]
    return "\n".join(lines)


def check_mergeable(ctx: Ctx, run: dict, prs: dict[str, int]) -> None:
    sl = slugs()
    for p, n in prs.items():
        for i in range(16):
            v = ctx.gh.pr_view(sl[p], n)
            if v.get("headRefOid") != tested(run, p) or v.get("baseRefName") != BRANCH \
                    or v.get("isCrossRepository") is not False:
                raise Abort({"class": O.VOID, "reason": f"{sl[p]}#{n} is not at the tested head on {BRANCH}"})
            m = v.get("mergeable")
            if m == "MERGEABLE":
                break
            if m == "CONFLICTING":
                raise Abort({"class": O.VOID, "reason": f"{sl[p]}#{n} conflicts"})
            if i == 15:
                raise Unknown("mergeable", sl[p], None, "still UNKNOWN after 30 s")   # transport (S-M7)
            ctx.sleep(2)


def record_prs(ctx: Ctx, run: dict, prs: dict[str, int]) -> None:
    """R10.2 step 4: prs, no_pr, tag, tag_repos into batch.json, atomically
    (the runner holds the device lock)."""
    sl = slugs()
    b = ctx.batch_json(run["id"])
    cs = b.get("commit_set") or {}
    b["prs"] = [{"repo": sl[p], "number": n} for p, n in sorted(prs.items())]
    b["no_pr"] = sorted(p for p in cs if p not in prs)
    b["tag"] = f"known-good-{run['id']}"
    b["tag_repos"] = [url(sl[p]) for p in sorted(cs)]
    b["base"] = {p: run["base"][p] for p in sorted(cs)}        # frozen by flash --merge-started (Sol B2)
    write_json(paths.batch_dir(run["id"]) / "batch.json", b)


# --- R10.4a reconciliation, R10.4 CS ---------------------------------------------------------

PENDING, LANDED, UNKNOWN, MISMATCH = "pending", "landed", "unknown", "mismatch"


def observe(ctx: Ctx, repo: str, number: int) -> dict:
    """One observation: the PR, the branch tip and, if the tip is not the
    base, its parents and tree, read together."""
    v = ctx.gh.pr_view(repo, number)
    tip = ctx.gh.ls_remote(url(repo), f"refs/heads/{BRANCH}")
    obs = {"state": v.get("state"), "merge_commit": (v.get("mergeCommit") or {}).get("oid"),
           "head": v.get("headRefOid"), "base_ref": v.get("baseRefName"), "cross": v.get("isCrossRepository"),
           "tip": tip, "parents": None, "tree": None}
    return obs


def _with_commit(ctx: Ctx, repo: str, obs: dict, base: str) -> dict:
    if obs["tip"] and obs["tip"] != base:
        c = ctx.gh.commit(repo, obs["tip"])
        obs = dict(obs, parents=c["parents"], tree=c["tree"])
    return obs


def classify(obs: dict, base: str, tested_sha: str, tested_tr: str) -> str:
    """pending, landed, or 'other' (I4, I5)."""
    pr_ok = obs["head"] == tested_sha and obs["base_ref"] == BRANCH and obs["cross"] is False
    if obs["tip"] == base and obs["state"] == "OPEN" and pr_ok:
        return PENDING
    merged_tip = obs["tip"] != base and obs["parents"] == [base, tested_sha] and obs["tree"] == tested_tr
    if merged_tip and obs["state"] == "MERGED" and obs["merge_commit"] == obs["tip"] and pr_ok:
        return LANDED
    if merged_tip and obs["state"] == "OPEN" and pr_ok:
        return "lag"                            # I4: GitHub lag is unknown, never landed
    return "other"


def reconcile_one(ctx: Ctx, run: dict, path: str, repo: str, number: int) -> tuple[str, dict]:
    """R10.4a for one PR: pending or landed from one observation; anything
    else is observed again 30 s later. Disagreeing reads and the PR-open lag
    are unknown (I4); two agreeing observations that fit neither pending nor
    landed are a mismatch (I5). A failed read raises Unknown."""
    base, t, tt = run["base"][path], tested(run, path), tested_tree(run, path)
    o1 = _with_commit(ctx, repo, observe(ctx, repo, number), base)
    c1 = classify(o1, base, t, tt)
    if c1 in (PENDING, LANDED):
        return c1, o1
    ctx.sleep(30)
    o2 = _with_commit(ctx, repo, observe(ctx, repo, number), base)
    if o1 != o2:
        if classify(o2, base, t, tt) == LANDED:
            return LANDED, o2                   # a lag that cleared: landed on this read (Opus minor)
        return UNKNOWN, o2
    if c1 == "lag":
        return UNKNOWN, o1
    return MISMATCH, o1


def reconcile(ctx: Ctx, run: dict) -> dict[str, str]:
    """Every frozen PR without a recorded merge: pending or landed (landed
    ones are recorded). Unknown raises Unknown (75); mismatch HALTs."""
    b = ctx.batch_json(run["id"])
    frozen = (b.get("merge_started") or {}).get("expected_prs") or {}
    merged = dict((run.get("merge") or {}).get("merged") or {})
    out = {}
    for path, e in sorted(frozen.items()):
        if e.get("number") is None or path in merged:
            continue
        try:
            cls, obs = reconcile_one(ctx, run, path, e["repo"], e["number"])
        except Unknown:
            unknown_started()                   # a failed read starts the I5 clock too (Sol M2)
            raise
        if cls == UNKNOWN:
            unknown_started()
            raise Unknown("reconcile", e["repo"], None, f"#{e['number']}: observations disagree or GitHub lags "
                                                        f"(state {obs['state']}, tip {str(obs['tip'])[:12]})")
        if cls == MISMATCH:
            raise Halt(f"{e['repo']}#{e['number']}: river-22.2 is at {str(obs['tip'])[:12]}, neither the base nor "
                       f"the tested merge (PR {obs['state']}); untested content may have reached river-22.2",
                       advice="Zak: inspect, then `batch settle <id> --zak`")
        if cls == LANDED:
            merged[path] = obs["tip"]
        out[path] = cls
    update_running(merge=dict(run.get("merge") or {}, merged=merged), reconcile_unknown_since=None)
    run["merge"] = dict(run.get("merge") or {}, merged=merged)
    return out


def unknown_started() -> None:
    """Record when reconciliation first had no answer (I5's 24 h), from the
    state file, never from a stale copy of running."""
    cur = (S.read().get("running") or {}).get("reconcile_unknown_since")
    if not cur:
        update_running(reconcile_unknown_since=now_iso())


def commit_set_check(ctx: Ctx, run: dict, need_all_merged: bool = False) -> list[str]:
    """CS (R10.4 step 1, E6), from ls-remote of every repo of the commit set."""
    b = ctx.batch_json(run["id"])
    frozen = ((b.get("merge_started") or {}).get("expected_prs")
              or {p: {"repo": s, "number": None} for p, s in slugs().items() if p in run["base"]})
    pr_paths = {p for p, e in frozen.items() if e.get("number") is not None} or set(run["merge"].get("prs") or {})
    merged = (run.get("merge") or {}).get("merged") or {}
    sl = slugs()
    bad = []
    for p in sorted(run["base"]):
        tip = ctx.gh.ls_remote(url(sl[p]), f"refs/heads/{BRANCH}")
        base = run["base"][p]
        if p in pr_paths and p in merged:
            m = merged[p]
            if tip != m:
                bad.append(f"{p}: river-22.2 at {str(tip)[:12]}, merge commit {m[:12]}")
                continue
            c = ctx.gh.commit(sl[p], m)
            if c["parents"] != [base, tested(run, p)] or c["tree"] != tested_tree(run, p):
                bad.append(f"{p}: merge {m[:12]} is not exactly [base, tested] with the tested tree")
        elif p in pr_paths:
            if need_all_merged:
                bad.append(f"{p}: no merge recorded")
            elif tip != base:
                bad.append(f"{p}: river-22.2 moved to {str(tip)[:12]} (base {base[:12]})")
        elif tip != base:
            bad.append(f"{p}: river-22.2 moved to {str(tip)[:12]} (base {base[:12]})")
    return bad


def pair_is_ours(ctx: Ctx, run: dict) -> bool:
    pair = (ctx.slot_state() or {}).get("pair") or {}
    return pair.get("batch") == run["id"] and (pair.get("attempts") or [None])[-1] == run.get("attempt_id")


def merge_all(ctx: Ctx, run: dict) -> None:
    """R10.4: each batch PR in path order, on reconciled state only."""
    b = ctx.batch_json(run["id"])
    frozen = (b.get("merge_started") or {}).get("expected_prs") or {}
    for path, e in sorted(frozen.items()):
        if e.get("number") is None:
            continue
        reconcile(ctx, run)
        if path in run["merge"]["merged"]:
            continue
        bad = commit_set_check(ctx, run)
        if bad:
            raise Halt("commit-set check failed before a merge: " + "; ".join(bad[:4]))
        v = ctx.gh.pr_view(e["repo"], e["number"])
        if v.get("state") != "OPEN":
            # I4, Sol M2, Gemini GM3: a PR that turned MERGED (or anything)
            # since the reconciliation above goes through R10.4a again.
            reconcile(ctx, run)
            if path in run["merge"]["merged"]:
                continue
            raise Halt(f"{e['repo']}#{e['number']} is {v.get('state')} without a landed merge")
        if v.get("isCrossRepository") is not False or v.get("baseRefName") != BRANCH \
                or v.get("headRefOid") != tested(run, path):
            raise Halt(f"{e['repo']}#{e['number']} is not open at the tested head on {BRANCH}")
        if not pair_is_ours(ctx, run):
            raise Halt(f"slot-state.json no longer reserves {run['id']}'s pair")
        commit_intent(f"merge {path}", f"{e['repo']}#{e['number']} open at {tested(run, path)[:12]}, "
                                       f"base tip {run['base'][path][:12]}",
                      f"merged, merge commit parents [{run['base'][path][:12]}, {tested(run, path)[:12]}]")
        try:
            ctx.gh.merge_pr(e["repo"], e["number"], tested(run, path))     # synchronous, never queued (Sol B4)
        except Refused as ex:
            raise Halt(f"GitHub refused the merge of {e['repo']}#{e['number']}: {ex.line}") from ex
        except Unknown:
            pass                                       # reconciled below (R11.6)
        reconcile(ctx, run)
        if path not in run["merge"]["merged"]:
            raise Unknown("merge", e["repo"], None, f"#{e['number']} not observed merged yet")


# --- R10.6 tags, promotion, known-good (KW, I7) -------------------------------------------------

def tag_targets(run: dict, b: dict) -> dict[str, str]:
    frozen = (b.get("merge_started") or {}).get("expected_prs") or {}
    out = {}
    for p, sha in (b.get("commit_set") or {}).items():
        if (frozen.get(p) or {}).get("number") is not None:
            out[p] = run["merge"]["merged"][p]
        else:
            out[p] = sha
    return out


def push_tags(ctx: Ctx, run: dict) -> None:
    b = ctx.batch_json(run["id"])
    tag = b["tag"]
    sl = slugs()
    for p, want in sorted(tag_targets(run, b).items()):
        u = url(sl[p])
        got = ctx.gh.ls_remote(u, f"refs/tags/{tag}^{{}}") or ctx.gh.ls_remote(u, f"refs/tags/{tag}")
        if got == want:
            continue
        if got is not None:
            raise Halt(f"{sl[p]} already has {tag} at {got[:12]}, not {want[:12]}")
        r = "origin" if p == MANIFEST else "zrm625"
        rc, _ = ctx.git.try_(p, "cat-file", "-e", f"{want}^{{commit}}")
        if rc != 0:
            remote_git(ctx, p, "fetch", "-q", r, BRANCH)
        rc, out = ctx.git.try_(p, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{}}")
        if rc == 0 and out.strip() != want:
            raise Halt(f"{p}: a local tag {tag} exists at {out.strip()[:12]}, not {want[:12]}")
        if rc != 0:
            ctx.git.out(p, "-c", "user.name=river-batch", "-c", "user.email=river-batch@localhost", "tag", "-a",
                        tag, "-m", f"known-good {run['id']}", want)
        remote_git(ctx, p, "push", r, f"refs/tags/{tag}")
        got = ctx.gh.ls_remote(u, f"refs/tags/{tag}^{{}}")
        if got != want:
            raise Unknown("tag", sl[p], None, f"{tag} not visible at {want[:12]} yet")


def promoted(ctx: Ctx, bid: str) -> bool:
    return ((ctx.slot_state() or {}).get("known_good") or {}).get("batch") == bid


def promote(ctx: Ctx, run: dict) -> None:
    if promoted(ctx, run["id"]):
        return
    commit_intent("promote", "pair reserved", f"known_good.batch = {run['id']}")
    r = ctx.tool("flash", ["--promote", run["id"]], run["id"], "promote")
    if r.rc == 0:
        with S.transaction() as st:
            st["strikes"]["lookup"] = 0
        return
    if r.rc == 75:
        raise Unknown("flash --promote", None, None, "lookup transport failure; promote again next tick")
    line = None
    try:
        line = json.loads(r.last_line())
    except ValueError:
        pass
    if r.rc == 1 and isinstance(line, dict) and "lookup" in line:
        ev = line.get("evidence") or {}
        if ev.get("status") in (401, 403):
            raise Pause(f"GitHub auth: {line.get('repo')}: {ev.get('status')}", step="auth")
        with S.transaction() as st:
            st["strikes"]["lookup"] += 1
            n = st["strikes"]["lookup"]
        if n >= 2:
            raise Pause(f"flash --promote lookup {line['lookup']} twice: {ev}", step="lookup")   # Sol M7
        raise Unknown("flash --promote", line.get("repo"), ev.get("status"), f"lookup {line['lookup']}; retry")
    raise Halt(f"flash --promote {run['id']} exited {r.rc}",
               advice="Zak resolves the pair, then `batch settle <id> --zak`")


def kw_holds(ctx: Ctx, run: dict | None) -> bool:
    """KW (R10.6 step 3, O-C2) for ``running``."""
    if not run:
        return False
    b = ctx.batch_json(run["id"]) or {}
    if b.get("kind") != "batch" or not b.get("merge_started"):
        return False
    from .outcomes import last_slot_event
    ss = ctx.slot_state() or {}
    ev = last_slot_event(ss)
    if not ev or ev.get("event") != "promote" or ev.get("batch") != run["id"]:
        return False
    if (ss.get("known_good") or {}).get("batch") != run["id"]:
        return False
    return ((ctx.known_good() or {}).get("latest") or {}).get("batch") != run["id"]


def kw_commit_set(ctx: Ctx, run: dict, local_only: bool) -> dict[str, str] | None:
    """I7: merge commits for PR repos (from running.merge, else from the
    promotion record flash --promote wrote into batch.json (I11), else
    resolved from GitHub) and batch.json commit_set for no_pr repos; never
    a batch-branch SHA. None if a merge commit is missing locally and
    ``local_only``."""
    b = ctx.batch_json(run["id"])
    frozen = (b.get("merge_started") or {}).get("expected_prs") or {}
    merged = dict(((b.get("promotion") or {}).get("merge_commits") or {}),
                  **((run.get("merge") or {}).get("merged") or {}))
    out = {}
    for p, sha in (b.get("commit_set") or {}).items():
        e = frozen.get(p) or {}
        if e.get("number") is None:
            out[p] = sha
        elif p in merged:
            out[p] = merged[p]
        else:
            if local_only:
                return None
            v = ctx.gh.pr_view(e["repo"], e["number"])
            m = (v.get("mergeCommit") or {}).get("oid")
            if v.get("state") != "MERGED" or not m:
                raise Unknown("kw", e["repo"], None, f"#{e['number']} merge commit not readable")
            out[p] = m
    return out


def write_known_good(ctx: Ctx, run: dict, local_only: bool = False) -> bool:
    """KW: write known-good.json once promotion proved the batch (R10.6
    step 3). Returns True if written."""
    if not kw_holds(ctx, run):
        return False
    cs = kw_commit_set(ctx, run, local_only)
    if cs is None:
        ctx.note(f"KW for {run['id']} waits for GitHub (a merge commit is not recorded locally)")
        return False
    b = ctx.batch_json(run["id"])
    kg = ctx.known_good() or {}
    rows = rbench.attempt_rows(b)
    new = regression.update_on_tag(kg, b["tag"], run["id"], cs, [r["run_id"] for r in rows], rows,
                                   (b.get("cameras") or {}).get("set") or [])
    write_json(paths.known_good_file(), new)
    return True


# --- R10.7 change PRs, R10.8 finish ---------------------------------------------------------------

def change_prs(ctx: Ctx, run: dict, st: dict) -> None:
    """R10.7. Before closing a change PR the approved head is recorded as a
    close intent (Sol M6); a PR found closed with an intent outstanding is
    checked again, so a close that was interrupted, or raced a push, still
    gets its reopen and follow-up."""
    bid = run["id"]
    tag = f"known-good-{bid}"
    mark = MARK.format(bid=bid)

    def followup(name: str, why: str) -> None:
        with S.transaction() as st2:
            st2["running"].setdefault("followups", {})[name] = why

    def closing(key: str, head: str | None) -> None:
        with S.transaction() as st2:
            c = st2["running"].setdefault("closing", {})
            if head is None:
                c.pop(key, None)
            else:
                c[key] = head

    def reopen(repo: str, n: int, pr: dict, name: str) -> None:
        ctx.gh.reopen_pr(repo, n)
        ctx.gh.comment_once(repo, n, mark + " reopen", f"Approved head {pr['head']} landed in {bid}; later "
                                                       "commits need a new council round.")
        followup(name, f"{pr['key']}: head moved during close; reopened")

    for m in run["members"]:
        e = S.entry(st, m["name"])
        for pr in e["prs"]:
            repo, n = records.split_key(pr["key"])
            intent = ((S.read().get("running") or {}).get("closing") or {}).get(pr["key"])
            v = None
            for _ in range(12):
                v = ctx.gh.pr_view(repo, n)
                if v.get("state") != "OPEN":
                    break
                ctx.sleep(5)
            if v.get("state") != "OPEN":
                if intent is not None:
                    if v.get("state") == "CLOSED" and v.get("headRefOid") != intent:
                        reopen(repo, n, pr, m["name"])
                    closing(pr["key"], None)
                continue
            batch_pr = (run["merge"].get("prs") or {}).get(pr["path"])
            ref = f"{repo}#{batch_pr}" if batch_pr else "the batch PR"
            if v.get("headRefOid") == pr["head"]:
                ctx.gh.comment_once(repo, n, mark, f"Merged through {ref} (batch {bid}), tagged `{tag}`.")
                closing(pr["key"], pr["head"])
                ctx.gh.close_pr(repo, n)
                again = ctx.gh.pr_view(repo, n)
                if again.get("headRefOid") != pr["head"]:
                    reopen(repo, n, pr, m["name"])
                closing(pr["key"], None)
            else:
                ctx.gh.comment_once(repo, n, mark, f"Approved head {pr['head']} landed in {bid}; later commits "
                                                   "need a new council round.")
                followup(m["name"], f"{pr['key']}: head moved past the approved head")


def delete_branch(ctx: Ctx, path: str, repo: str, bid: str, sha: str) -> str:
    """R13.3: 'absent', 'deleted', or 'other' (left; caller pauses)."""
    ref = f"refs/heads/batch/{bid}"
    got = ctx.gh.ls_remote(url(repo), ref)
    if got is None:
        return "absent"
    if got != sha:
        return "other"
    remote_git(ctx, path, "push", f"--force-with-lease={ref}:{sha}", "zrm625", f":{ref}")
    return "deleted"


def fast_forward_local(ctx: Ctx, paths_: list[str]) -> None:
    for p in paths_:
        r = "origin" if p == MANIFEST else "zrm625"
        local = "default" if p == MANIFEST else BRANCH
        try:
            remote_git(ctx, p, "fetch", "-q", r, BRANCH)
        except (Unknown, Refused):
            continue
        if branch(ctx, p) == local and clean(ctx, p):
            ctx.git.try_(p, "merge", "-q", "--ff-only", f"{r}/{BRANCH}")


def prune_local(ctx: Ctx, bid: str, touched: list[str]) -> None:
    for p in touched:
        ctx.git.try_(p, "branch", "-q", "-D", f"batch/{bid}")
        rc, out = ctx.git.try_(p, "for-each-ref", "--format=%(refname)", f"refs/river-batches/{bid}/")
        for ref in out.split() if rc == 0 else []:
            ctx.git.try_(p, "update-ref", "-d", ref)
