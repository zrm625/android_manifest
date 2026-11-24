"""`batch stage` (R14.1, I6, I10) and `batch qualify` (R14.2-R14.4, I8,
I20): building calibration and baseline candidates, and qualifying the
runner itself.

Refusals (R2.4, I20) for both: exit 3 if ``halt`` is set; exit 1 if
``running`` has purpose ``batch`` or ``experiment`` or names a batch.json
with ``merge_started``; exit 1 on a live blocking condition (a disturbed
tree, a foreign pair, known-good records that disagree, a strike at its
limit) instead of proceeding under an existing pause. ``stage`` also refuses
an interrupted run that still holds a pair: it does no phone work.
"""

from __future__ import annotations

import re

from riverqa import stage as rstage
from riverqa.core import now_iso, read_json, write_json

from . import abtest, compose, kb, merge, outcomes as O, paths, phases, recover, state as S, version
from .ctx import (Abort, Blocked, Ctx, Halt, Locks, Pause, commit_phase, image_id, image_tag, remote_git)
from .state import Refused

MANIFEST = compose.MANIFEST
RUNNER_ID_RE = re.compile(r"^[bq][0-9]{4,}$")


class HaltedState(Exception):
    """R2.4: exit 3."""


def refusals(ctx: Ctx, command: str) -> None:
    st = S.read()
    if st.get("halt"):
        raise HaltedState(f"HALT is set: {st['halt'].get('reason')}")
    run = st.get("running")
    if run:
        if run["purpose"] in ("batch", "experiment"):
            raise Refused(f"{run['id']} ({run['purpose']}) is running: {command} waits")
        b = ctx.batch_json(run["id"]) or {}
        if b.get("merge_started"):
            raise Refused(f"{run['id']} has merge_started")
        pair = (ctx.slot_state() or {}).get("pair") or {}
        if command == "stage" and pair.get("batch") == run["id"]:
            raise Refused(f"the interrupted run {run['id']} still holds a slot pair: roll it back with "
                          "`batch qualify` or `batch tick`")
    # I20: a live blocking condition refuses (exit 1).
    if st.get("lease") and not run:
        raise Refused(f"tree lease of {st['lease']['batch']} is held: the tree may be disturbed (batch tick "
                      "restores it)")
    why = recover.known_good_agreement(ctx)
    if why:
        raise Refused(why)
    f = recover.foreign_pair(ctx)
    if f:
        raise Refused(f[1])
    lim = [k for k, v in st["strikes"].items() if v >= 2]
    if lim:
        raise Refused(f"strike limit reached for {', '.join(lim)}: Zak resolves it and runs `batch resume --zak`")


def pin_image(ctx: Ctx) -> str:
    """ensure_image, then the image ID (R14.1 G-C3, R14.4)."""
    if image_id(ctx) is None:
        r = ctx.podman.run("build", "-t", image_tag(), "-f", str(paths.SCRIPTS / "bench" / "Containerfile"),
                           str(paths.SCRIPTS / "bench"))
        if r.returncode != 0:
            raise Refused(f"podman build failed: {(r.stderr or '').strip()[-200:]}")
    iid = image_id(ctx)
    if not iid:
        raise Refused("cannot resolve the build image ID")
    return iid


def local_base(ctx: Ctx, check_xml: bool, kg: dict | None) -> dict[str, str]:
    """R8.2 steps 2 and 3 for stage and qualify: forks on a clean
    river-22.2 equal to zrm625 (fast-forward if behind), the manifest at
    origin/river-22.2; then TC."""
    forks = compose.forks()
    base = {}
    for p in sorted(forks) + [MANIFEST]:
        r, local = ("origin", "default") if p == MANIFEST else ("zrm625", "river-22.2")
        remote_git(ctx, p, "fetch", "-q", r, "river-22.2")
        want = ctx.git.out(p, "rev-parse", f"{r}/river-22.2").strip()
        from .ctx import branch, clean
        if branch(ctx, p) != local or not clean(ctx, p):
            raise Refused(f"{p} is not on a clean {local}")
        h = ctx.git.out(p, "rev-parse", "HEAD").strip()
        if h != want:
            rc, _ = ctx.git.try_(p, "merge-base", "--is-ancestor", h, want)
            if rc != 0:
                raise Refused(f"{p} is ahead of or diverged from {r}/river-22.2")
            ctx.git.out(p, "merge", "-q", "--ff-only", f"{r}/river-22.2")
        base[p] = want
    if check_xml:
        if compose.manifest_xml(base[MANIFEST], ctx.git) != phases.baseline_xml(ctx, kg):
            raise Refused("manifest pins changed since the baseline: run `batch stage --kind baseline` (Z7)")
    r = ctx.tc_check(dict(base))
    if not r.ok:
        raise Refused("tree check failed: " + r.summary())
    return base


def borrowed(ctx: Ctx, names: list[str], base: dict, kg: dict, slugs: dict) -> list[dict]:
    st = S.read()
    out = []
    for n in names:
        e = S.live_entry(st, n)
        if not e or e["status"] != "queued":
            raise Refused(f"--change {n}: not a queued entry")
        out.append(e)
    try:
        bad = phases.member_checks(ctx, out, base, slugs, kg, write=False)
    except phases.eligibility.BaseCameraMismatch as e:
        raise Refused(str(e)) from e
    if bad:
        raise Refused("; ".join(f"{e['name']}: {why}" for e, _s, why in bad))
    return out


def compose_run(ctx: Ctx, bid: str, purpose: str, members: list[dict], base: dict, kg: dict | None,
                pin: str, verified: bool) -> dict:
    cams = phases.kg_cameras(kg) if kg else None
    comp = compose.compose(ctx.git, bid, members, {p: base[p] for p in base if p != MANIFEST},
                           cams if cams is not None else rstage.parse_camera_set(None))
    if purpose == "baseline":
        comp.members = []
    elif len(comp.members) != len(members):
        dropped = [e["name"] for e, _ in comp.stays_queued + comp.needs_reapproval + comp.waiting_zak]
        raise Refused(f"cannot compose {', '.join(dropped)} (conflict or camera-set change)")
    compose.write_refs(ctx.git, bid, comp.commits)
    ckey = compose.content_key(comp.trees, compose.manifest_xml(base[MANIFEST], ctx.git))
    versions = version.current() | {"image_id": pin, "manifest_head": base[MANIFEST]}
    run = phases.new_running(bid, purpose, versions, base, verified, comp, ckey, [], [e["name"] for e in members])
    with S.transaction("claim", batch=bid, purpose=purpose) as st:
        if st["running"] or st["lease"]:
            raise Refused("another run started meanwhile")
        if purpose == "qualify":
            st["next_qual"] += 1
        st["running"] = run
    return run


# --- batch stage (R14.1) -------------------------------------------------------------------------

def stage_cmd(ctx: Ctx, kind: str, bid: str, changes: list[str]) -> dict:
    if kind not in ("calibration", "baseline"):
        raise Refused("--kind is calibration or baseline")
    rstage.check_id(bid, "batch ID")
    if RUNNER_ID_RE.match(bid):
        raise Refused(f"{bid} is a runner ID (bNNNN, qNNNN); pick another")
    if (paths.batch_dir(bid) / "batch.json").exists():
        raise Refused(f"{bid} is already staged")
    if kind == "baseline" and changes:
        raise Refused("--kind baseline builds river-22.2 as it is: no --change")
    refusals(ctx, "stage")
    with Locks(ctx):
        refusals(ctx, "stage")
        st = S.read()
        if st.get("running"):                      # R13.2 steps 2 and 3, host only
            recover.abort(ctx)
        elif st.get("lease"):
            ok, found = phases.restore(ctx)
            if not ok:
                raise Refused("tree-disturbed: " + "; ".join(found[:4]))
        why = recover.known_good_agreement(ctx)
        if why:
            raise Refused(why)
        notes = owed_notes(ctx)
        pin = pin_image(ctx)
        kg = ctx.known_good()
        base = local_base(ctx, check_xml=(kind == "calibration"), kg=kg)
        verified = False
        if kind == "calibration":
            if not kg:
                raise Refused("no known-good.json: run the baseline first")
            verified, why_v = phases.base_verified(ctx, base, kg)
            if not changes and not verified:
                raise Refused("zero-member calibration needs a verified base: " + "; ".join(why_v[:3]))
        slugs = compose.forks() | {MANIFEST: merge.MANIFEST_SLUG}
        members = borrowed(ctx, changes, base, kg, slugs) if changes else []
        run = compose_run(ctx, bid, kind, members, base, kg, pin, verified)
        try:
            if run["touched"]:
                phases.checkout(ctx, run)
            else:
                with S.transaction("phase", phase="checking-out") as st2:
                    st2["running"]["phase"] = "checking-out"
                r = ctx.tc_check(phases.expected_heads(run))
                if not r.ok:
                    raise Abort({"class": O.VOID, "reason": "tree check failed: " + r.summary()})
            phases.build(ctx, run)
            phases.stage_run(ctx, run, kind, False, None)             # I10: no camera backstop for these kinds
            commit_phase("restoring")
            phases.restore_or_void(ctx)
        except Abort as a:
            with S.transaction() as st2:
                st2["running"]["outcome"] = a.outcome
            recover.abort(ctx)
            raise Refused(f"staging {bid} failed: {a.outcome.get('reason') or a.outcome.get('checks')}") from a
        # I6: the finish transaction clears running and lease after staging,
        # so a hand-flashed pair of this ID is always foreign to the runner.
        with S.transaction("staged", batch=bid, kind=kind) as st2:
            st2["running"] = None
            st2["lease"] = None if (st2["lease"] or {}).get("batch") == bid else st2["lease"]
        merge.prune_local(ctx, bid, run["touched"])
        return {"staged": bid, "kind": kind, "borrowed": [e["name"] for e in members], "notes": notes}


def owed_notes(ctx: Ctx) -> list[str]:
    """What stage reports and leaves to tick or qualify (R14.1)."""
    from riverqa import fbmode, journal as jmod, slots
    out = []
    ss = ctx.slot_state() or {}
    if ss.get("pair"):
        out.append(f"slot pair of {ss['pair']['batch']} is open")
    if slots.checkpoint_pending(ss):
        out.append("a baseline checkpoint is pending (flash --recover)")
    if fbmode.pending() is not None:
        out.append("an fb_mode obligation is pending")
    if jmod.Journal(paths.home() / "qa" / "journal.json", paths.home() / "qa").entries:
        out.append("the QA journal is not empty")
    return out


# --- batch qualify (R14.3, R14.4) -----------------------------------------------------------------

def qualify_cmd(ctx: Ctx, run_only: bool, changes: list[str]) -> dict:
    refusals(ctx, "qualify")
    with Locks(ctx):
        refusals(ctx, "qualify")
        recover.entry_point(ctx, setup=True)          # the full entry point: phone recovery allowed
        if S.read().get("running"):
            raise Refused("an interrupted run could not be finished")
        kg = ctx.known_good()
        if not kg:
            raise Refused("no known-good.json")
        pin = pin_image(ctx)
        base = local_base(ctx, check_xml=True, kg=kg)
        ok, why = phases.base_verified(ctx, base, kg)
        if not ok:
            raise Refused("qualification needs a verified base: " + "; ".join(why[:3]))
        c = kb.check(ctx.phone(), ctx.slot_state())
        if not c.ok:
            raise Refused(f"KB: {c.message}")
        result = {"runner_build": version.runner_build(), "runner_tools": version.runner_tools(),
                  "harness_version": version.harness(), "image_id": pin}
        ab = read_json(paths.abtest_file()) or {}
        if run_only:
            if not (ab.get("passed") and ab.get("runner_build") == result["runner_build"] and ab.get("image_id") == pin):
                raise Refused("--run-only needs a passing part 1 for this runner_build and image (runner/abtest.json)")
        elif not (ab.get("passed") and ab.get("runner_build") == result["runner_build"] and ab.get("image_id") == pin):
            ab = abtest.run(ctx, base, pin)
            write_json(paths.abtest_file(), ab)
            if not ab.get("passed"):
                return {"qualified": False, "part": 1, "failed": ab.get("failures")}
        result["abtest"] = {k: ab.get(k) for k in ("at", "runner_build", "image_id")}
        part2 = run_part2(ctx, base, kg, pin, changes)
        if not part2["passed"]:
            return {"qualified": False, "part": 2, "failed": part2["failed"]}
        result["run"] = part2
        result["at"] = now_iso()
        write_json(paths.qualified_file(), result)
        with S.transaction("qualified", run=part2.get("id")):
            pass
        return {"qualified": True, **result}


def run_part2(ctx: Ctx, base: dict, kg: dict, pin: str, changes: list[str]) -> dict:
    """R14.3: phases 1-9 and 11 of a qNNNN run, then the end-of-tick setup
    and readback, TC after restore, and nothing derived left pending."""
    st = S.read()
    qid = f"q{st['next_qual']:04d}"
    slugs = compose.forks() | {MANIFEST: merge.MANIFEST_SLUG}
    members = borrowed(ctx, changes, base, kg, slugs) if changes else []
    run = compose_run(ctx, qid, "qualify", members, base, kg, pin, True)
    failed = []
    outcome = None
    try:
        if run["touched"]:
            phases.checkout(ctx, run)
        phases.build(ctx, run)
        phases.stage_run(ctx, run, "batch", False, kg)
        commit_phase("restoring")
        phases.restore_or_void(ctx)
        phases.flash(ctx, run)
        run = S.read()["running"]
        phases.bench(ctx, run, kg)
        phases.setup_candidate(ctx, run)
        run = S.read()["running"]
        outcome = phases.test(ctx, run)
    except Abort as a:
        outcome = a.outcome
    if outcome.get("class") != O.PASS:
        failed.append(f"run: {outcome.get('class')}: {outcome.get('reason') or outcome.get('checks')}")
    with S.transaction("decided", batch=qid) as st2:
        st2["running"]["outcome"] = outcome
        st2["running"]["phase"] = "decided"
    try:
        recover.abort(ctx)                           # phase 11: rollback; nothing applied to entries
    except (Pause, Halt) as e:
        failed.append(f"rollback/restore: {e.message}")
        return {"passed": False, "failed": failed, "id": qid}
    try:
        recover.setup_known_good(ctx)
    except (Pause, Halt, Blocked) as e:
        failed.append(f"setup on known-good: {e.message}")
    r = ctx.tc_check(dict(base))
    if not r.ok:
        failed.append("TC after restore: " + r.summary())
    st3 = S.read()
    if st3.get("running") or st3.get("lease"):
        failed.append("the state file still has running or a lease")
    owed = owed_notes(ctx)
    dev = ctx.phone()
    so, c = recover.setup_owed(ctx, dev)
    if so:
        owed.append("setup still owed")
    if not c.ok:
        owed.append(f"KB after the run: {c.message}")
    if owed:
        failed.append("derived obligations pending: " + "; ".join(owed))
    qa = (run.get("qa") or S.read().get("last_qa") or {})
    return {"passed": not failed, "failed": failed, "id": qid, "at": now_iso(),
            "borrowed": [e["name"] for e in members], "qa": qa}
