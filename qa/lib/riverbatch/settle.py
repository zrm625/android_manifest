"""`batch settle ID --zak` (R13.4, I5, I9, I11): Zak's exit from a forward
path that cannot finish.

Order (I11): the local promotion proof is checked and known-good.json
written **first**, before any GitHub call; then GitHub is read (a transport
failure exits 75 with nothing else written); then one transaction.

A re-baseline is owed (I9, S-M2) when the batch was not promoted and
anything landed, **or** when any fork's river-22.2 tree has drifted from
known-good's commit set even though none of the batch's heads landed.
"""

from __future__ import annotations

import datetime as dt

from riverqa.core import now_iso

from . import commands, eligibility, merge, records, state as S
from .ctx import Ctx, Locks
from .github import BRANCH, url
from .state import Refused

UNKNOWN_DAYS = 1.0


def _age_hours(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(iso)).total_seconds() / 3600
    except ValueError:
        return 0.0


def preconditions(ctx: Ctx, st: dict, bid: str) -> dict:
    run = st.get("running")
    if not run or run["id"] != bid:
        raise Refused(f"{bid} is not the running batch")
    b = ctx.batch_json(bid) or {}
    if not b.get("merge_started"):
        raise Refused(f"{bid} has no merge_started: an unstarted batch is aborted by the next tick")
    long_unknown = _age_hours(run.get("reconcile_unknown_since")) > 24
    if not st.get("halt") and not long_unknown:
        raise Refused("settle needs a HALT, or a reconciliation unknown for more than 24 h (I5)")
    pair = (ctx.slot_state() or {}).get("pair")
    if pair and pair.get("batch") == bid:
        raise Refused(f"slot-state.json still holds {bid}'s pair: resolve it first (flash --rollback --zak, or "
                      f"flash --promote {bid})")
    return run


def is_ancestor(ctx: Ctx, repo: str, sha: str) -> bool:
    status = ctx.gh.call(["gh", "api", f"repos/{repo}/compare/{BRANCH}...{sha}", "--jq", ".status"], repo).strip()
    if status not in ("ahead", "behind", "identical", "diverged"):
        raise ctx_unknown(repo, status)
    return status in ("behind", "identical")


def ctx_unknown(repo, what):
    from .ctx import Unknown
    return Unknown("compare", repo, None, f"malformed: {what!r}")


def settle(ctx: Ctx, bid: str) -> dict:
    with Locks(ctx):
        st = S.read()
        run = preconditions(ctx, st, bid)
        promoted = merge.promoted(ctx, bid)
        kw_written = False
        if promoted:                                     # I11: local proof first, no GitHub
            kw_written = merge.write_known_good(ctx, run, local_only=True)
        # GitHub reads (transport: 75, nothing written below)
        if promoted and not kw_written and merge.kw_holds(ctx, run):
            kw_written = merge.write_known_good(ctx, run)
        b = ctx.batch_json(bid) or {}
        frozen = (b.get("merge_started") or {}).get("expected_prs") or {}
        sl = merge.slugs()
        open_prs, merged_prs = {}, {}
        for p, e in frozen.items():
            if e.get("number") is not None:
                v = ctx.gh.pr_view(e["repo"], e["number"])
                if v.get("state") == "OPEN":
                    open_prs[p] = {"repo": e["repo"], "number": e["number"]}
                elif v.get("state") == "MERGED":
                    merged_prs[p] = {"repo": e["repo"], "number": e["number"]}
        # Sol SM1, Gemini GM2-4: a batch PR GitHub still reports OPEN whose
        # tested head is already in river-22.2 landed (GitHub lag): it is
        # expected to show MERGED, never closed, and its branch is kept.
        landed_open = {}
        for p, x in sorted(open_prs.items()):
            if is_ancestor(ctx, x["repo"], run["composed"][p]["sha"]):
                landed_open[p] = x
        branches = {}
        for p in run.get("touched") or []:
            sha = run["composed"][p]["sha"]
            if ctx.gh.ls_remote(url(sl[p]), f"refs/heads/batch/{bid}") is not None:
                branches[p] = sha
        tags = [p for p in sorted(b.get("commit_set") or {})
                if ctx.gh.ls_remote(url(sl[p]), f"refs/tags/known-good-{bid}") is not None]
        landed = {}
        members = {m["name"]: S.member_entry(st, run, m) for m in run["members"]}     # Sol SB1
        lost = [n for n, e in members.items() if e is None]
        if lost:
            raise Refused(f"{', '.join(lost)}: no longer the entry {bid} claimed; Zak inspects the state file")
        for m in run["members"]:
            e = members[m["name"]]
            got = [p for p in e["prs"] if is_ancestor(ctx, records.split_key(p["key"])[0], p["head"])]
            landed[m["name"]] = ("all" if len(got) == len(e["prs"]) else "some" if got else "none",
                                 [p["key"] for p in got])
        drifted = drift(ctx, sl) if not promoted else []
        elig = {}
        if not promoted:
            for name, (how, _) in landed.items():
                if how == "none":
                    e = members[name]
                    rec, errs = records.load_bundle(name, e["record_sha256"])
                    try:
                        elig[name] = commands.eligibility_of(ctx, rec, errs)
                    except Refused as ex:
                        # Gemini GM6: river-22.2's camera set changed (or the
                        # base cannot be read as one): the member waits for
                        # Zak; the settle still finishes and owes the re-baseline.
                        elig[name] = eligibility.Outcome(eligibility.WAITING, f"after the settle of {bid}: {ex}")
        anything_landed = any(how != "none" for how, _ in landed.values())
        rebaseline = (not promoted) and (anything_landed or bool(drifted))
        with S.transaction("settle", batch=bid) as st2:
            r = st2["running"]
            for m in r["members"]:
                e = S.member_entry(st2, r, m)
                if not e or e["status"] != "in-batch":
                    continue
                how, keys = landed[m["name"]]
                e["batch"] = None
                if promoted:
                    e["merged"] = {"batch": bid, "tag": f"known-good-{bid}", "at": now_iso(), "settled": True}
                    S.set_status(e, "merged", f"settled in {bid}", by="zak", action="settle")
                elif how == "all":
                    e["merged"] = {"batch": bid, "tag": None, "at": now_iso(), "settled": True}
                    e["followup"] = f"settled in {bid}: untagged"
                    S.set_status(e, "merged", f"settled in {bid}: untagged", by="zak", action="settle")
                elif how == "some":
                    S.set_status(e, "needs-reapproval", f"partly landed in {bid}: {', '.join(keys)}", by="zak",
                                 action="settle")
                else:
                    o = elig[m["name"]]
                    if o.kind == eligibility.OK:
                        S.set_status(e, "queued", f"returned by the settle of {bid}", by="zak", action="settle")
                    else:
                        S.set_status(e, eligibility.WAITING if o.kind == eligibility.REFUSED else o.kind, o.reason,
                                     by="zak", action="settle")
            st2["cleanup"] = [c for c in st2["cleanup"] if c["batch"] != bid]
            if branches or open_prs:
                # Sol M4, SM1, Gemini GM2-4: while a re-baseline is owed
                # (anything landed, drift, or GitHub lagging past I5) the open
                # batch PRs and their tested branches are Zak's repair (merge
                # them at their tested heads): nothing is closed or deleted.
                # The ones that landed are expected (Opus OM3), the ones
                # landed but still OPEN too; every touched repo keeps its
                # tested head (Sol M5).
                hold = ({"settle": bid, "why": "the open batch PRs and branches are kept for Zak's repair until "
                                               "the re-baseline"} if rebaseline else None)
                st2["cleanup"].append(S.cleanup_item(
                    bid, {p: run["composed"][p]["sha"] for p in run.get("touched") or []}, branch_repos=branches,
                    pr_repos={p: x for p, x in open_prs.items() if p not in landed_open},
                    expected_merged=dict(merged_prs, **landed_open), hold=hold, landed_open=landed_open))
            if rebaseline:
                st2["rebaseline"] = {"settle": bid, "at": now_iso(), "drifted": drifted,
                                     "landed": {k: v[0] for k, v in landed.items()}}
            st2["running"] = None
            st2["halt"] = None
            st2["lease"] = st2["lease"] if st2["lease"] and st2["lease"]["batch"] != bid else None
            S.add_report(st2, f"settle-{bid}")
            st2.setdefault("settles", {})[bid] = {
                "at": now_iso(), "promoted": promoted, "known_good_written": kw_written,
                "landed": {k: v[0] for k, v in landed.items()}, "tags": tags, "rebaseline": rebaseline,
                "drifted": drifted,
                "advice": ("Re-baseline owed: river-22.2 holds content no gate tested as a whole. If that content is "
                           "broken, revert it through reviewed PRs first; to finish a partial merge instead, Zak "
                           "merges the missing PRs of this batch at their exact tested heads "
                           "(--match-head-commit). Then run the re-baseline sequence (qa/RUNNER.md, R13.4)."
                           if rebaseline else None)}
        merge.prune_local(ctx, bid, run.get("touched") or [])
        return st2["settles"][bid]


def drift(ctx: Ctx, sl: dict[str, str]) -> list[str]:
    """Forks whose river-22.2 tree differs from known-good's commit (I9)."""
    kg = ctx.known_good() or {}
    cs = (kg.get("latest") or {}).get("commit_set") or {}
    out = []
    for p, s in sorted(sl.items()):
        if p == merge.MANIFEST:
            continue
        tip = ctx.gh.ls_remote(url(s), f"refs/heads/{BRANCH}")
        want = cs.get(p)
        if not tip or not want:
            out.append(p)
            continue
        if tip != want and ctx.gh.commit(s, tip)["tree"] != ctx.gh.commit(s, want)["tree"]:
            out.append(p)
    return out
