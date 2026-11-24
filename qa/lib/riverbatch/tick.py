"""`batch tick` (R8): the loop, preflight (R8.2) and one batch (R8.3)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from riverqa import regression
from riverqa.core import now_iso, read_json

from . import compose, eligibility, kb, merge, outcomes as O, paths, phases, recover, report, state as S, tc, \
    version
from .ctx import (EXIT_HALT, EXIT_OK, EXIT_RESUMABLE, Abort, AuthRefused, Blocked, Busy, Ctx, Halt, Locks,
                  NotQualified, Pause, ReExec, Refused, Retry, Stop, Unknown, branch, clean, commit_intent,
                  commit_phase, image_id, remote_git)

MANIFEST = compose.MANIFEST


# --- R8.2 preflight ----------------------------------------------------------------------------

def preflight(ctx: Ctx) -> dict:
    """Returns {"base", "kg", "slugs", "verified"}; raises Blocked,
    NotQualified, Pause, ReExec or Unknown."""
    bad = tc.tools_problems()                                                     # 1
    for k in ("wifi_ssid_file", "wifi_psk_file"):                                  # R3.2: setup needs both
        f = ctx.cfg.get(k)
        if not f or not Path(f).is_file():
            bad.append(f"runner/config.json {k} is not a readable file")
    if bad:
        raise Blocked("; ".join(bad))
    kg = ctx.known_good()
    if not kg:
        raise Blocked("no known-good.json")
    forks = compose.forks()
    remote_git(ctx, MANIFEST, "fetch", "-q", "origin", "river-22.2")             # 2
    origin = ctx.git.out(MANIFEST, "rev-parse", "origin/river-22.2").strip()
    base_xml = phases.baseline_xml(ctx, kg)
    if compose.manifest_xml(origin, ctx.git) != base_xml:
        raise Pause("manifest pins changed: run a new baseline (charter: council review and a new baseline), Z7")
    mhead = ctx.git.out(MANIFEST, "rev-parse", "HEAD").strip()
    if branch(ctx, MANIFEST) != "default" or not clean(ctx, MANIFEST):
        raise Blocked(".repo/manifests is not on a clean `default` branch")
    if mhead != origin:
        rc, _ = ctx.git.try_(MANIFEST, "merge-base", "--is-ancestor", mhead, origin)
        if rc != 0:
            raise Blocked(".repo/manifests is ahead of or diverged from origin/river-22.2")
        if os.environ.get("RIVER_BATCH_REEXEC") == "1":
            raise Blocked(".repo/manifests still behind after a re-exec")
        ctx.git.out(MANIFEST, "merge", "-q", "--ff-only", "origin/river-22.2")
        raise ReExec("manifest fast-forwarded: re-exec")
    if compose.manifest_xml("HEAD", ctx.git) != base_xml:
        raise Pause("manifest pins changed: run a new baseline (charter: council review and a new baseline), Z7")
    base = {}
    for p in sorted(forks):                                                       # 3
        remote_git(ctx, p, "fetch", "-q", "zrm625", "river-22.2")
        want = ctx.git.out(p, "rev-parse", "zrm625/river-22.2").strip()
        h = ctx.git.out(p, "rev-parse", "HEAD").strip()
        if branch(ctx, p) != "river-22.2" or not clean(ctx, p):
            raise Blocked(f"{p} is not on a clean river-22.2")
        if h != want:
            rc, _ = ctx.git.try_(p, "merge-base", "--is-ancestor", h, want)
            if rc != 0:
                raise Blocked(f"{p} river-22.2 is ahead of or diverged from zrm625/river-22.2")
            ctx.git.out(p, "merge", "-q", "--ff-only", "zrm625/river-22.2")
        base[p] = want
    base[MANIFEST] = origin
    expect = dict(base)
    r = ctx.tc_check(expect)                                                     # 4
    if not r.ok:
        raise Blocked("tree check failed: " + r.summary())
    recover.retention(ctx)                                                       # 5 (I17)
    free = ctx.free_gb(paths.home())
    if free < ctx.cfg["free_space_floor_gb"]:
        raise Blocked(f"only {free:.0f} GB free in $BENCH_HOME (floor {ctx.cfg['free_space_floor_gb']} GB)")
    why = qualification_problems(ctx)                                            # 6
    if why:
        raise NotQualified("not qualified: " + "; ".join(why))
    ss = ctx.slot_state() or {}                                                  # 7
    if ss.get("pair"):
        raise Blocked(f"slot pair of {ss['pair']['batch']} is open")
    if not (ss.get("known_good") or {}).get("build_id"):
        raise Blocked("known-good build unstamped")
    c = kb.check(ctx.phone(), ss)
    if c.kind == kb.HALT:
        raise Halt(c.message)
    if c.kind == kb.PAUSE:
        raise Pause(c.message)
    if not c.ok:
        raise Blocked(c.message)
    seed = kg.get("calibration_seed") or {}                                      # 8
    ref = kg.get("ram_reference") or {}
    latest = kg.get("latest") or {}
    if latest.get("batch") == seed.get("batch") and seed.get("median_mb") is not None \
            and ref.get("median_mb") is not None \
            and float(seed["median_mb"]) > float(ref["median_mb"]) + regression.RAM_MEDIAN_MARGIN_MB:
        raise Pause(f"the re-baselined RAM {seed['median_mb']} MB cannot meet the preserved reference "
                    f"{ref['median_mb']} MB from {ref.get('tag') or ref.get('batch')}; raising it is Zak's decision")
    st = S.read()
    if st.get("rebaseline"):
        rb = st["rebaseline"]
        closed = (ss.get("baseline_closed") or {})
        if closed.get("at") and closed["at"] > rb["at"] and latest.get("batch") == closed.get("batch"):
            with S.transaction("rebaseline-cleared") as st2:
                st2["rebaseline"] = None
        else:
            raise Pause(f"base unverified after settle of {rb['settle']}: re-baseline, R13.4")
    github_check(ctx, forks)                                                      # 9
    ok, why_v = phases.base_verified(ctx, base, kg)
    if not ok:
        ctx.note("base unverified: " + "; ".join(why_v[:3]))
    return {"base": base, "kg": kg, "verified": ok,
            "slugs": dict(forks) | {MANIFEST: merge.MANIFEST_SLUG}}


def github_check(ctx: Ctx, forks: dict[str, str]) -> None:
    """R8.2 step 9 (O-C3): rate_limit answers and push permission on the 9
    forks and the manifest. 401 or 403 without rate-limit evidence, or no
    push permission: pause. Anything unknown: Unknown."""
    ctx.gh.rate_limit()
    for repo in sorted(set(forks.values())) + [merge.MANIFEST_SLUG]:
        try:
            if not ctx.gh.can_push(repo):
                raise Pause(f"GitHub auth: {repo}: no push permission")
        except AuthRefused as e:
            raise Pause(f"GitHub auth: {repo}: {e.status}") from e
        except Refused as e:
            raise Pause(f"GitHub auth: {repo}: {e.status}") from e


def qualification_problems(ctx: Ctx) -> list[str]:
    """R8.2 step 6: `phone-qa qualified` exits 0 and qualified.json matches
    the current versions and image (R14.4)."""
    import json
    why = []
    r = ctx.tool("phone-qa", ["qualified"], None)
    try:
        line = json.loads(r.last_line())
    except ValueError:
        line = {}
    if r.rc != 0:
        why += line.get("missing") or [f"phone-qa qualified exited {r.rc}"]
    q = read_json(paths.qualified_file())
    if not q:
        return why + ["no runner/qualified.json"]
    now = version.current()
    for k, qk in (("runner_build", "runner_build"), ("runner_tools", "runner_tools"), ("harness", "harness_version")):
        if q.get(qk) != now[k]:
            why.append(f"{qk} changed since qualification")
    if q.get("image_id") != image_id(ctx):
        why.append("build image changed: requalify, R14.4")
    return why


# --- one batch -------------------------------------------------------------------------------------

def next_batch(ctx: Ctx, pre: dict) -> dict | None:
    """Select, check, compose and claim (R7.1-R7.3). None: nothing runnable."""
    kg = pre["kg"]
    for _ in range(50):
        st = S.read()
        members = compose.select(st["entries"], ctx.cfg["batch_size"])
        if not members:
            return None
        try:
            bad = phases.member_checks(ctx, members, pre["base"], pre["slugs"], kg)
        except eligibility.BaseCameraMismatch as e:
            raise Pause(str(e)) from e
        if bad:
            continue
        bid = f"b{st['next_batch']:04d}"
        comp = compose.compose(ctx.git, bid, members, {p: pre["base"][p] for p in pre["base"] if p != MANIFEST},
                               phases.kg_cameras(kg))
        for e, why in comp.needs_reapproval + comp.waiting_zak:
            status = "needs-reapproval" if any(x is e for x, _ in comp.needs_reapproval) else "waiting-zak"
            with S.transaction("compose-drop", name=e["name"], status=status) as st2:
                x = S.entry(st2, e["name"])
                if x and x["status"] == "queued" and x["rev"] == e["rev"]:
                    S.set_status(x, status, why)
        if not comp.members:
            continue
        compose.write_refs(ctx.git, bid, comp.commits)
        ckey = compose.content_key(comp.trees, compose.manifest_xml(pre["base"][MANIFEST], ctx.git))
        versions = version.current() | {"image_id": read_json(paths.qualified_file(), {}).get("image_id"),
                                        "manifest_head": pre["base"][MANIFEST]}
        purpose = "experiment" if comp.members[0].get("kind") == "experiment" else "batch"
        run = phases.new_running(bid, purpose, versions, pre["base"], pre["verified"], comp, ckey, comp.members, [])
        if phases.claim(run, comp.members, "next_batch"):
            return S.read()["running"]
    raise Blocked("selection did not settle after 50 tries")


def run_batch(ctx: Ctx, run: dict, kg: dict) -> None:
    """R8.3 phases 2-12 for a claimed batch."""
    outcome = None
    try:
        try:
            phases.checkout(ctx, run)
            phases.build(ctx, run)
            phases.stage_run(ctx, run, "batch", run["purpose"] == "experiment", kg)
            commit_phase("restoring")
            phases.restore_or_void(ctx)
            phases.flash(ctx, run)
            run = S.read()["running"]
            phases.bench(ctx, run, kg)
            phases.setup_candidate(ctx, run)
            run = S.read()["running"]
            outcome = phases.test(ctx, run)
        except Abort as a:
            outcome = dict(a.outcome, pause=a.pause or a.outcome.get("pause"))
        outcome = recover.commit_outcome(ctx, outcome)
        run = S.read()["running"]
        if outcome.get("class") == O.PASS and run["purpose"] == "batch":
            why = merge.merge_rule(run, ctx.batch_json(run["id"]) or {}, outcome, version.current(),
                                   not qualification_problems(ctx))
            if not why:
                try:
                    merge_path(ctx)
                    return
                except Abort as a:
                    # Sol M1, Gemini GB1, Opus OM2: R10.2-R10.3 refused before
                    # merge_started (a stale base, a conflict, a PR at another
                    # head, the pair or versions changed): the batch is void
                    # and ends here, through R11.2 step 2, in this tick.
                    if (ctx.batch_json(run["id"]) or {}).get("merge_started"):
                        raise Halt(f"{run['id']}: the merge path aborted after merge_started: {a}",
                                   advice="Zak: `batch settle <id> --zak`") from a
                    with S.transaction("decided", batch=run["id"], cls=a.outcome.get("class")) as st:
                        st["running"]["outcome"] = dict(a.outcome, pause=a.pause or a.outcome.get("pause"))
            else:
                with S.transaction() as st:
                    st["running"]["outcome"] = {"class": O.VOID, "reason": "merge rule: " + "; ".join(why),
                                                "pause": "gate not qualified"}
        recover.abort(ctx)
    except (Unknown, AuthRefused) as e:
        b = ctx.batch_json(run["id"]) or {}
        if b.get("merge_started"):
            raise
        with S.transaction() as st:
            if st["running"] and not (st["running"].get("outcome") or {}).get("class") in (O.FAIL, O.DESK):
                st["running"]["outcome"] = {"class": O.TRANSPORT, "reason": str(e)}
        recover.abort(ctx, extra_pause=(f"GitHub auth: {e.repo}: {e.status}" if isinstance(e, AuthRefused) else None))
        raise


def merge_path(ctx: Ctx) -> None:
    """R10.2-R10.3 (before merge_started), then the forward path."""
    run = S.read()["running"]
    bid = run["id"]
    with S.transaction("phase", phase="prs-open") as st:
        st["running"]["phase"] = "prs-open"
        st["running"]["reached_prs_open"] = True
        st["cleanup"] = [c for c in st["cleanup"] if c["batch"] != bid]
        tested = {p: run["composed"][p]["sha"] for p in run["touched"]}
        st["cleanup"].append(S.cleanup_item(bid, tested, branch_repos=tested))
    merge.push_branches(ctx, run)
    commit_intent("open-prs", "no batch PRs", "one open batch PR per touched fork at the tested head")
    prs = merge.open_prs(ctx, run)
    sl = merge.slugs()
    with S.transaction() as st:
        st["running"]["merge"]["prs"] = prs
        for c in st["cleanup"]:
            if c["batch"] == bid:
                c["pr_repos"] = {p: {"repo": sl[p], "number": n} for p, n in prs.items()}
    run = S.read()["running"]
    merge.check_mergeable(ctx, run, prs)
    commit_intent("record-prs", "batch.json without prs", "batch.json prs/no_pr/tag/tag_repos")
    merge.record_prs(ctx, run, prs)
    bad = merge.commit_set_check(ctx, run)                                         # R10.3
    if bad or not merge.pair_is_ours(ctx, run) or any(
            version.current()[k] != run["versions"][k] for k in ("harness", "runner_build", "runner_tools")):
        raise Abort({"class": O.VOID, "reason": "stale-base: " + "; ".join(bad[:3] or ["pair or versions changed"])})
    commit_intent("merge-started", "batch.json without merge_started", "batch.json merge_started")
    r = ctx.tool("flash", ["--merge-started", bid], bid, "merge-started")
    if r.rc != 0:
        log = (paths.batch_dir(bid) / "runner-logs" / "merge-started.log")
        text = log.read_text(errors="replace") if log.exists() else ""
        if "PR record changed" in text:
            raise Halt(f"flash --merge-started {bid}: PR record changed")
        raise Abort({"class": O.VOID, "reason": f"flash --merge-started refused (exit {r.rc})"})
    with S.transaction("merge-started", batch=bid) as st:
        st["running"]["phase"] = "merging"
        st["running"]["intent"] = None
        st["strikes"]["transport_prs"] = 0
    recover.forward(ctx)


# --- R8.1 the tick ------------------------------------------------------------------------------------

def commit_stop(e: Stop) -> None:
    """Commit how the tick stopped. HALT and a first pause are recorded with
    a report; an existing pause is never replaced (I19). A blocked,
    not-qualified or retry stop is persisted as the current ``blocker``, and
    a new or changed blocker queues a report too (Sol M12), so a tick that
    ran no batch still leaves a readable reason."""
    with S.transaction(e.kind, reason=e.message) as st:
        if isinstance(e, Halt):
            S.set_halt(st, e.message, e.step, e.advice)
            S.add_report(st, f"halt-{int(time.time())}")
        elif isinstance(e, Pause):
            if not st.get("pause"):
                S.set_pause(st, e.message, e.step, e.advice)
                S.add_report(st, f"pause-{int(time.time())}")
        elif isinstance(e, (Blocked, NotQualified, Retry)):
            old = st.get("blocker") or {}
            if (old.get("kind"), old.get("message")) != (e.kind, e.message):
                st["blocker"] = {"kind": e.kind, "message": e.message, "at": now_iso(),
                                 "report": f"{e.kind}-{int(time.time())}"}
                S.add_report(st, st["blocker"]["report"])


def clear_blocker() -> None:
    if S.read().get("blocker"):
        with S.transaction("unblocked") as st:
            st["blocker"] = None


def tick(ctx: Ctx, max_batches: int) -> tuple[int, dict]:
    summary = {"ran": [], "state": "idle"}
    st = S.read()
    report.flush(ctx)                                                          # 1
    if st.get("halt"):
        return EXIT_HALT, {"state": "halted", "halt": st["halt"]}
    paused_now = False
    try:
        locks = Locks(ctx).__enter__()                                         # 2
    except Busy as e:
        return EXIT_RESUMABLE, {"state": "busy", "why": e.message}
    try:
        if S.read().get("halt"):
            return EXIT_HALT, {"state": "halted"}
        if S.read().get("pause"):                                              # 3
            recover.paused_pass(ctx)
            report.flush(ctx)
            return EXIT_OK, {"state": "paused", "pause": S.read()["pause"]}
        try:
            recover.entry_point(ctx, setup=False)                              # 4
            for _ in range(max_batches):                                       # 5
                pre = preflight(ctx)
                clear_blocker()
                run = next_batch(ctx, pre)
                if run is None:
                    break
                summary["ran"].append(run["id"])
                run_batch(ctx, run, pre["kg"])
                locks.release()
                locks = Locks(ctx).__enter__()
                if S.read().get("halt"):
                    return EXIT_HALT, {"state": "halted", **summary}
                if S.read().get("pause"):
                    paused_now = True
                    break
                recover.entry_point(ctx, setup=False)
        except Unknown as e:
            ctx.unknown = str(e)
        except AuthRefused as e:
            commit_stop(Pause(f"GitHub auth: {e.repo}: {e.status}"))
            paused_now = True
        except (Halt, Pause, Blocked, NotQualified, Retry) as e:
            commit_stop(e)
            if isinstance(e, Halt):
                report.flush(ctx)
                return EXIT_HALT, {"state": "halted", "halt": e.message, **summary}
            paused_now = paused_now or isinstance(e, Pause)
            summary["state"] = e.kind
            summary["why"] = e.message
        # 6: the end-of-tick pass
        try:
            if S.read().get("pause") or paused_now:
                recover.paused_pass(ctx)
            else:
                recover.entry_point(ctx, setup=True, local_only=bool(ctx.unknown))
        except Unknown as e:
            ctx.unknown = ctx.unknown or str(e)
        except (Halt, Pause, Blocked, Retry) as e:
            commit_stop(e)
            if isinstance(e, Halt):
                report.flush(ctx)
                return EXIT_HALT, {"state": "halted", "halt": e.message, **summary}
            if summary["state"] == "idle":
                summary["state"], summary["why"] = e.kind, e.message
        report.flush(ctx)
    finally:
        locks.release()
    if ctx.unknown:
        return EXIT_RESUMABLE, {**summary, "state": "unknown", "why": ctx.unknown}
    if S.read().get("pause"):
        summary["state"] = "paused"
        summary["pause"] = S.read()["pause"]
    elif summary["ran"] and summary["state"] == "idle":
        summary["state"] = "ran"
    return EXIT_OK, summary
