"""Crash recovery (R11.2), the recovery entry point (R13.2), the paused
pass (R8.1 step 3, I19), cleanup items (R13.3, I12), finishing a run
(R10.8) and retention (R15.2, I17).

Every obligation is derived from disk and the phone each time, never from a
flag the runner may have failed to write.
"""

from __future__ import annotations

import shutil
import time

from riverqa import fbmode, journal as jmod, slots
from riverqa.core import now_iso

from . import kb, merge, outcomes as O, paths, phases, state as S, version
from .ctx import (AuthRefused, Blocked, Ctx, Halt, Pause, Refused, Retry, Unknown, commit_phase)

MANIFEST = merge.MANIFEST


# --- strikes -----------------------------------------------------------------------------

def strike(name: str, ok: bool, what: str, quiet_pause: bool = False) -> None:
    """A success resets the count; a failure adds one; the second
    consecutive failure pauses (unless a pause is already set: I19)."""
    with S.transaction("strike", name=name, ok=ok) as st:
        if ok:
            st["strikes"][name] = 0
            return
        st["strikes"][name] += 1
        n = st["strikes"][name]
        if n >= 2 and not st.get("pause"):
            S.set_pause(st, f"{what} failed twice in a row", step=name)
            S.add_report(st, f"pause-{int(time.time())}")
    if not ok and n >= 2 and not quiet_pause:
        raise Pause(f"{what} failed twice in a row")


def run_recover(ctx: Ctx, why: str) -> None:
    r = ctx.tool("flash", ["--recover"], None)
    if r.rc == 3:
        raise Halt(f"flash --recover halted ({why})")
    if r.rc == 75:                       # a lagging or failed GitHub read: retry, never a strike (Sol B2)
        raise Unknown("flash --recover", None, None, f"no answer yet ({why})")
    strike("recover", r.rc == 0, "flash --recover")


# --- R11.2 --------------------------------------------------------------------------------

def recover_running(ctx: Ctx, local_only: bool = False) -> str:
    """R11.2 for ``running``: 'aborted', 'finished' or 'waits' (forward
    path left for the next tick)."""
    st = S.read()
    run = st["running"]
    b = ctx.batch_json(run["id"])                     # unreadable: Halt
    if not (b or {}).get("merge_started"):
        abort(ctx)
        return "aborted"
    try:
        return forward(ctx, local_only)
    except Refused as e:
        # Gemini GM2-3: a confirmed refusal on the forward path that no step
        # reconciled is Zak's, never a runner error.
        raise Halt(f"{run['id']}: GitHub refused on the forward path: {e}",
                   advice="Zak: `batch settle <id> --zak`") from e


def abort(ctx: Ctx, extra_pause: str | None = None) -> None:
    """R11.2 step 2: local only, each part idempotent and committed."""
    st = S.read()
    run = st["running"]
    bid = run["id"]
    # a. rescue evidence first
    if not run.get("outcome") and run.get("phase") in ("flashing", "benching"):
        att = run.get("attempt_id")
        if not att:
            b = ctx.batch_json(bid) or {}
            atts = ctx.flash_rec(bid).get("attempts") or []
            if b.get("flash_attempt") and atts and atts[-1].get("attempt_id") == b["flash_attempt"]:
                att = b["flash_attempt"]
        ok, why = O.rescue_evidence(ctx.slot_state() or {}, ctx.wipes(), ctx.flash_rec(bid), bid, att)
        if ok:
            check = "flash.boot" if run["phase"] == "flashing" else "bench.boot"
            commit_outcome(ctx, {"class": O.FAIL, "checks": [check], "rescued": True,
                                 "evidence": {"step": run["phase"], "attempt_id": att, "why": why}})
    # b. rollback if the pair is this run's own reservation (I6)
    pair = (ctx.slot_state() or {}).get("pair")
    if owns_pair(run, pair):
        commit_phase("rolling-back")
        r = ctx.tool("flash", ["--rollback"], bid, "rollback")
        if r.rc == 3:
            raise Halt(f"flash --rollback halted in {bid}")
        strike("recover", r.rc == 0, "flash --rollback")
        if r.rc != 0:
            raise Retry(f"flash --rollback of {bid} failed; retried next tick")
    # c. lease
    tree_pause = None
    if S.read().get("lease"):
        ok, found = phases.restore(ctx)
        if not ok:
            tree_pause = "tree-disturbed: " + "; ".join(found[:6])
    # d, e. apply and clear
    with S.transaction("abort", batch=bid) as st:
        run = st["running"]
        outcome = run.get("outcome") or {"class": O.VOID, "reason": f"aborted in phase {run.get('phase')}"}
        if outcome.get("class") == O.PASS and run.get("purpose") == "batch":
            # Only a batch's pass is a merge that did not happen; an
            # experiment's pass goes to Zak (R9.2, Opus OM1, Gemini GM8).
            outcome = {"class": O.VOID, "reason": "passed but never reached merge_started"}
        if tree_pause and outcome.get("class") not in (O.FAIL, O.DESK):
            outcome = {"class": O.VOID, "reason": tree_pause}
        before = st.get("pause")
        O.apply(st, run, outcome)
        # A cause this abort knows (a disturbed tree, a GitHub auth refusal)
        # names the pause, over a generic one apply() set in this same
        # transaction (Gemini minor); a pause from before is never replaced.
        for p in (tree_pause, extra_pause, outcome.get("pause")):
            if p and (not st.get("pause") or (st["pause"] is not before and p in (tree_pause, extra_pause))):
                S.set_pause(st, p, step="abort")
                break
        st["running"] = None
        S.add_report(st, bid)
    merge.prune_local(ctx, bid, run.get("touched") or [])
    if tree_pause or extra_pause or outcome.get("pause"):
        raise Pause(tree_pause or extra_pause or outcome.get("pause"))


def commit_outcome(ctx: Ctx, outcome: dict) -> dict:
    """Phase 10: commit the outcome (R9; R9.5 desk rule for a fail)."""
    with S.transaction("decided", cls=outcome.get("class")) as st:
        run = st["running"]
        if outcome.get("class") == O.FAIL:
            ev = phases.fail_evidence(run, outcome, run["versions"].get("harness"))
            outcome = O.decide_fail(st, run, outcome.get("checks") or [], ev) | {"rescued": outcome.get("rescued")}
        if outcome.get("class") == O.PASS and run.get("purpose") == "batch":
            st["last_fail"] = None
        run["outcome"] = outcome
        run["phase"] = "decided"
        run["intent"] = None
        return outcome


def forward(ctx: Ctx, local_only: bool = False) -> str:
    """R11.2 step 3."""
    run = S.read()["running"]
    # a. KW first: local, proven by the promotion.
    merge.write_known_good(ctx, run, local_only=True)
    if local_only:
        ctx.note(f"{run['id']}: forward path waits for the next tick (no second remote pass, I15)")
        return "waits"
    # c. versions, skipped once promoted
    if not merge.promoted(ctx, run["id"]):
        now = version.current()
        for k in ("harness", "runner_build", "runner_tools"):
            if now[k] != run["versions"].get(k):
                raise Halt("tools changed mid-merge", advice="Zak: `batch settle <id> --zak`")
        merge.merge_all(ctx, run)
        run = S.read()["running"]
        commit_phase("landed")
        bad = merge.commit_set_check(ctx, run, need_all_merged=True)
        if bad:
            raise Halt("untested content reached river-22.2: " + "; ".join(bad[:4]),
                       advice="Zak: `batch settle <id> --zak`")
        # Sol M3: no tag before the pair is proven ours (batch and attempt),
        # even when every merge was already recorded; promotion is the only
        # durable proof that skips this.
        if not merge.pair_is_ours(ctx, run):
            raise Halt(f"slot-state.json no longer reserves {run['id']}'s pair at attempt {run.get('attempt_id')}; "
                       "nothing was tagged", advice="Zak resolves the pair, then `batch settle <id> --zak`")
        commit_phase("tagging")
        merge.push_tags(ctx, run)
        merge.promote(ctx, run)
    run = S.read()["running"]
    merge.write_known_good(ctx, run)
    commit_phase("change-prs")
    st = S.read()
    merge.change_prs(ctx, st["running"], st)
    finish_merged(ctx)
    return "finished"


def finish_merged(ctx: Ctx) -> None:
    """R10.8."""
    run = S.read()["running"]
    bid = run["id"]
    sl = merge.slugs()
    left = {}
    for p in run["touched"]:
        res = merge.delete_branch(ctx, p, sl[p], bid, run["composed"][p]["sha"])
        if res == "other":
            left[p] = run["composed"][p]["sha"]
    merge.fast_forward_local(ctx, sorted(run["base"]))
    with S.transaction("finish", batch=bid) as st:
        r = st["running"]
        for m in r["members"]:
            e = S.member_entry(st, r, m)                    # Sol SB1
            if e and e["status"] == "in-batch":
                e["batch"] = bid
                e["merged"] = {"batch": bid, "tag": f"known-good-{bid}", "at": now_iso()}
                if (r.get("followups") or {}).get(m["name"]):
                    e["followup"] = r["followups"][m["name"]]
                S.set_status(e, "merged", f"merged in {bid}")
        st["cleanup"] = [c for c in st["cleanup"] if c["batch"] != bid]
        if left:
            # Opus OM3: its batch PRs merged through the runner; cleanup must
            # not take them for unexpected merges.
            sl2 = merge.slugs()
            landed = {p: {"repo": sl2[p], "number": n} for p, n in ((r.get("merge") or {}).get("prs") or {}).items()}
            st["cleanup"].append(S.cleanup_item(bid, {p: r["composed"][p]["sha"] for p in r["touched"]},
                                                branch_repos=left, expected_merged=landed))
            S.set_pause(st, f"batch/{bid} branch not at the composed SHA in {', '.join(left)}", step="cleanup")
        st["voids_in_a_row"] = 0
        st["last_fail"] = None
        st["running"] = None
        S.add_report(st, bid)
    merge.prune_local(ctx, bid, run["touched"])
    retention(ctx)


# --- R13.2 the entry point -------------------------------------------------------------------

def known_good_agreement(ctx: Ctx) -> str | None:
    ss, kg = ctx.slot_state(), ctx.known_good()
    if not ss or not kg:
        return None
    a = (ss.get("known_good") or {}).get("batch")
    b = (kg.get("latest") or {}).get("batch")
    if a != b:
        return f"known-good records disagree: slot-state names {a}, known-good.json names {b}"
    return None


def owns_pair(run: dict | None, pair: dict | None) -> bool:
    """Is ``pair`` the one this run's own flash reserved (Sol I6)? Only
    batch, experiment and qualify runs flash; a stage run never does, so a
    pair with a stage run's ID was flashed by hand and is foreign. Once the
    run adopted an attempt, the pair's last attempt must be it."""
    if not run or not pair or pair.get("batch") != run.get("id"):
        return False
    if run.get("purpose") not in ("batch", "experiment", "qualify"):
        return False
    att = run.get("attempt_id")
    return att is None or (pair.get("attempts") or [None])[-1] == att


def foreign_pair(ctx: Ctx) -> tuple[str, str] | None:
    """(kind, message) of a pair that is not running's, or None."""
    pair = (ctx.slot_state() or {}).get("pair")
    run = S.read().get("running")
    if not pair or owns_pair(run, pair):
        return None
    if pair.get("kind") == "batch":
        return "pause", f"slot pair of {pair['batch']} is not the runner's"
    return "blocked", f"phone in use by {pair['batch']} ({pair.get('kind')})"


def entry_point(ctx: Ctx, setup: bool, local_only: bool = False) -> None:
    """R13.2. Raises Halt, Pause, Blocked, Unknown or Busy; returns when
    every local obligation is met."""
    ss = ctx.slot_state()
    if ss and not ss.get("pair") and slots.checkpoint_pending(ss):          # 1
        run_recover(ctx, "baseline checkpoint")
    st = S.read()
    if st["running"]:                                                      # 2
        if recover_running(ctx, local_only) == "waits":
            return
    elif st["lease"]:
        ok, found = phases.restore(ctx)
        if not ok:
            raise Pause("tree-disturbed: " + "; ".join(found[:6]))
    if S.read()["running"]:
        return
    why = known_good_agreement(ctx)                                        # 3
    if why:
        raise Pause(why)
    f = foreign_pair(ctx)                                                  # 4
    if f:
        raise (Pause if f[0] == "pause" else Blocked)(f[1])
    dev = ctx.phone()
    if fbmode.pending() is not None:                                       # 5
        run_recover(ctx, "fb_mode obligation")
    elif dev.state() == "" and dev.in_fastboot():
        raise Blocked("phone in fastboot")                                  # I18: someone's dev session
    j = jmod.Journal(paths.home() / "qa" / "journal.json", paths.home() / "qa")
    if j.entries:                                                          # 6
        c = kb.check(dev, ctx.slot_state())
        if c.kind == kb.HALT:
            raise Halt(c.message)
        if c.ok:
            r = ctx.tool("phone-qa", ["journal", "--restore"], None)
            if r.rc == 3:
                raise Halt("phone-qa journal --restore halted")
            strike("journal", r.rc == 0, "phone-qa journal --restore")
        else:
            ctx.note(f"journal waits: {c.message}")
    if setup:                                                              # 7
        setup_known_good(ctx)
    if not local_only:                                                     # 8
        try:
            cleanup_items(ctx)
        except Unknown as e:
            ctx.note(f"cleanup: no answer from GitHub ({e}); retried next tick")


def setup_owed(ctx: Ctx, dev) -> tuple[bool, kb.Check]:
    c = kb.check(dev, ctx.slot_state())
    if not c.ok:
        return False, c
    if ctx.wipe_seq() > (S.read().get("setup_wipe_seq") or 0):
        return True, c
    for cmd in ("settings get secure user_setup_complete", "settings get global device_provisioned"):
        rc, out = kb.checked_read(dev, cmd)
        if not rc.ok:
            return False, rc
        if out.strip() != "1":
            return True, c
    return False, c


def setup_known_good(ctx: Ctx, quiet_pause: bool = False) -> None:
    """R13.2 step 7: setup on known-good and the readback."""
    if (ctx.slot_state() or {}).get("pair"):
        return
    dev = ctx.phone()
    owed, c = setup_owed(ctx, dev)
    if c.kind == kb.HALT:
        raise Halt(c.message)
    if c.kind == kb.PAUSE and not quiet_pause:
        raise Pause(c.message)
    if not owed:
        return
    seq = ctx.wipe_seq()
    rc = phases.phone_setup(ctx, None)
    ok = rc == 0
    if ok:
        c2, bad = phases.readback(ctx, dev)
        if c2.kind == kb.HALT:
            raise Halt(c2.message)
        ok = c2.ok and not bad
        if not ok:
            ctx.note("setup readback failed: " + (c2.message or "; ".join(bad)))
    if ok:
        with S.transaction("setup", seq=seq) as st:
            st["setup_wipe_seq"] = seq
            st["strikes"]["setup"] = 0
        return
    strike("setup", False, "phone-setup on known-good", quiet_pause=quiet_pause)


def paused_pass(ctx: Ctx) -> None:
    """R8.1 step 3 (G-C5, I19): only R13.2 step 4 (a pair, ours or
    foreign: nothing touches the phone) and step 7 (setup on known-good,
    strike below 2). A foreign pair only gates setup; it never overwrites
    the pause reason."""
    st = S.read()
    if (ctx.slot_state() or {}).get("pair"):
        f = foreign_pair(ctx)
        if f:
            ctx.note(f"paused; {f[1]}")
        return
    if st["strikes"]["setup"] >= 2 or st.get("running"):
        return
    try:
        setup_known_good(ctx, quiet_pause=True)
    except Pause as e:
        ctx.note(f"paused pass: {e.message}")


# --- R13.3 cleanup items (I12) -----------------------------------------------------------------

def cleanup_items(ctx: Ctx) -> None:
    """R13.3, I12: close the batch PRs found by exact head branch in every
    touched repo, at the tested head recorded for that repo (Sol M5), and
    delete the batch branches at their composed SHAs. A batch PR that
    landed through the runner or a settle is expected (Opus OM3); a held
    item waits for Zak's repair (Sol M4); after the hold, a batch PR that
    was open at the settle and is now merged is Zak's repair."""
    st = S.read()
    sl = merge.slugs()
    for item in list(st["cleanup"]):
        bid = item["batch"]
        hold = item.get("hold")
        if hold and (st.get("rebaseline") or {}).get("settle") == hold.get("settle"):
            ctx.note(f"cleanup of {bid} held: {hold.get('why')}")
            continue
        tested = item.get("tested") or {}
        expected = dict(item.get("expected_merged") or {})
        if hold:
            expected.update(item.get("pr_repos") or {})
        problems, done, waiting = [], True, set()
        landed_open = item.get("landed_open") or {}
        touched = sorted(set(tested) | set(item.get("branch_repos") or {}) | set(item.get("pr_repos") or {})
                         | set(item.get("expected_merged") or {}))
        try:
            for p in touched:                       # I12: by exact head branch in every touched repo
                for pr in ctx.gh.pr_list(sl[p], f"batch/{bid}", "all"):
                    ident = {"repo": sl[p], "number": pr.get("number")}
                    if pr.get("state") == "MERGED":
                        # Sol SM1: an expected merge counts only at the tested
                        # head, into river-22.2, from the repo itself.
                        if expected.get(p) == ident and pr.get("headRefOid") == tested.get(p) \
                                and pr.get("baseRefName") == "river-22.2" and pr.get("isCrossRepository") is False:
                            continue
                        raise Pause(f"batch PR {sl[p]}#{pr['number']} merged without the runner (or not at its "
                                    "tested head into river-22.2)", step="cleanup")
                    if pr.get("state") != "OPEN":
                        continue
                    if landed_open.get(p) == ident:
                        # Landed, GitHub still says OPEN: wait for MERGED;
                        # never close it or delete its branch (Sol SM1).
                        waiting.add(p)
                        done = False
                        continue
                    sha = tested.get(p)
                    if not sha:
                        problems.append(f"{sl[p]}#{pr['number']}: no tested head recorded for {p}; not closing it")
                        done = False
                        continue
                    if pr.get("headRefName") != f"batch/{bid}" or pr.get("baseRefName") != "river-22.2" \
                            or pr.get("isCrossRepository") is not False or pr.get("headRefOid") != sha:
                        problems.append(f"{sl[p]}#{pr['number']} does not match batch/{bid} at its composed head")
                        done = False
                        continue
                    ctx.gh.comment_once(sl[p], pr["number"], merge.MARK.format(bid=bid),
                                        f"Batch {bid} did not merge; closing its PR.")
                    ctx.gh.close_pr(sl[p], pr["number"])
            for p, sha in sorted((item.get("branch_repos") or {}).items()):
                if p in waiting:
                    continue
                res = merge.delete_branch(ctx, p, sl[p], bid, sha)
                if res == "other":
                    problems.append(f"{sl[p]} batch/{bid} is not at the composed SHA {sha[:12]}")
                    done = False
        except AuthRefused as e:
            raise Pause(f"GitHub auth: {e.repo}: {e.status}", step="auth")
        except Refused as e:
            third = False
            with S.transaction("cleanup-refused", batch=bid) as st2:
                it = next((c for c in st2["cleanup"] if c["batch"] == bid), None)
                if it is not None:
                    it["refusals"] = it.get("refusals", 0) + 1 if it.get("last_refusal") == str(e) else 1
                    it["last_refusal"] = str(e)
                    if it["refusals"] >= 3:
                        third = True
                        S.set_pause(st2, f"cleanup of {bid}: the same refusal three ticks in a row: {e}",
                                    step="cleanup")
            if third:                       # Gemini minor: the pause stops the tick
                raise Pause(f"cleanup of {bid}: the same refusal three ticks in a row: {e}", step="cleanup")
            continue
        with S.transaction("cleanup", batch=bid, done=done) as st2:
            if done:
                st2["cleanup"] = [c for c in st2["cleanup"] if c["batch"] != bid]
            elif problems:
                S.set_pause(st2, f"cleanup of {bid}: " + "; ".join(problems[:4]), step="cleanup")
        if problems:
            raise Pause(f"cleanup of {bid}: " + "; ".join(problems[:4]), step="cleanup")


# --- R15.2 retention (I17) ------------------------------------------------------------------------

def protected_batches(ctx: Ctx) -> set[str]:
    out = set()
    kg = ctx.known_good() or {}
    out.add((kg.get("latest") or {}).get("batch"))
    ss = ctx.slot_state() or {}
    out.add((ss.get("known_good") or {}).get("batch"))
    out.add((ss.get("pair") or {}).get("batch"))
    out.add((ss.get("baseline_closed") or {}).get("batch"))
    st = S.read()
    out.add((st.get("running") or {}).get("id"))
    for e in st["entries"]:
        if e["status"] == "failed":
            hist = S.failures(st, e["name"], e.get("identity"))
            if hist:
                # The latest evidence by time, across every identity the
                # change had (Sol I17): failures() concatenates them by key.
                out.add(max(hist, key=lambda ev: str(ev.get("at") or "")).get("batch"))
    out.discard(None)
    return out


def retention(ctx: Ctx, now: float | None = None) -> list[str]:
    """Delete batches/<id>/images/ older than 7 days except protected
    batches; build logs older than 30 days. Runs under the caller's locks,
    at finish and before the free-space check (I17)."""
    now = now if now is not None else ctx.now()
    keep = protected_batches(ctx)
    removed = []
    root = paths.home() / "batches"
    if root.is_dir():
        for d in sorted(root.iterdir()):
            img = d / "images"
            # Sol SM7: the exclusions (protected batches, no images left)
            # apply to image deletion only; the log policy runs for every
            # batch directory.
            if d.name not in keep and img.is_dir() and now - img.stat().st_mtime > 7 * 86400:
                shutil.rmtree(img, ignore_errors=True)
                removed.append(str(img))
            logs = d / "runner-logs"
            if logs.is_dir():
                for f in logs.glob("build-*.log"):
                    if now - f.stat().st_mtime > 30 * 86400:
                        f.unlink()
                        removed.append(str(f))
    logs = paths.home() / "logs"
    if logs.is_dir():
        for f in logs.glob("build-*.log"):
            if now - f.stat().st_mtime > 30 * 86400:
                f.unlink()
                removed.append(str(f))
    return removed
