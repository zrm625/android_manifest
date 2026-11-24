"""slot-state.json: the slot pair, known_good_slot, and how a pair is resolved.

Pure transitions on a dict; flash does the I/O. Schema (version 1):

    {
      "version": 1,
      "known_good_slot": "a" | "b",
      "known_good": {"slot", "provisional", "build_id", "boot_sha256",
                     "batch", "recorded_at", "identity"},
                     # build_id null: an unstamped baseline, identified by
                     # "identity" (dates, slot, boot hash) instead
      "baseline_closed": null | {"batch", "build_id", "at", "seed",
                                 "seed_written"},
                     # seed: known-good.json, saved in the same write as the
                     # slot promotion; seed_written false means the
                     # checkpoint was interrupted (flash --recover finishes it)
      "pair": null | {"batch", "kind", "target", "fallback",
                      "fallback_boot_sha256", "created_at", "written",
                      "attempts": [attempt ids]},
      "history": [{"event", "at", ...}]
    }

Rules (DESIGN.md, "The batch sequence", step 2):
- The pair is fixed before anything is written, and never worked out again
  from fastboot's current-slot. The target is always the slot that is not
  known_good_slot.
- A pair is resolved only by a verified rollback to known_good_slot (which
  releases it) or by a durable promotion (known_good_slot becomes the target).
- Before any flash: no unresolved pair from another batch, the running slot is
  known_good_slot, and the write target is not. A batch's own pair is reused
  only after a verified rollback, which releases it; so a retry reserves again.
"""

from __future__ import annotations

KINDS = ("batch", "baseline", "calibration", "experiment")
SLOTS = ("a", "b")


class SlotError(RuntimeError):
    pass


def other(slot: str) -> str:
    if slot not in SLOTS:
        raise SlotError(f"unknown slot {slot!r}")
    return "b" if slot == "a" else "a"


def _event(state: dict, event: str, at: str, **kw) -> None:
    state.setdefault("history", []).append({"event": event, "at": at, **kw})


def first_run(running_slot: str, boot_sha256: str, at: str) -> dict:
    """No slot-state.json yet: the running slot becomes a provisional
    known_good_slot, with its whole boot partition hash checked live."""
    if running_slot not in SLOTS or not boot_sha256:
        raise SlotError("first-run transition needs the running slot and its boot hash")
    st = {
        "version": 1,
        "known_good_slot": running_slot,
        "known_good": {"slot": running_slot, "provisional": True, "build_id": None,
                       "boot_sha256": boot_sha256, "batch": None, "recorded_at": at},
        "baseline_closed": None,
        "pair": None,
        "history": [],
    }
    _event(st, "first-run", at, slot=running_slot)
    return st


def preflight(state: dict | None, batch_id: str, kind: str) -> list[str]:
    """Problems that forbid a flash for this batch, all decided from disk
    (R8-3): flash_batch runs this before it touches the phone. Empty list:
    go. That the phone runs known_good_slot with its build is checked on
    the phone afterwards (fbmode.verify_boot)."""
    if kind not in KINDS:
        return [f"unknown pair kind {kind!r}"]
    if state is None:
        if kind == "baseline":
            return []  # first-run transition happens first
        return ["no slot-state.json: run the --baseline flash first (first-run transition)"]
    errs = []
    pair = state.get("pair")
    if pair:
        if pair["batch"] != batch_id:
            errs.append(f"unresolved pair from batch {pair['batch']} ({pair['kind']}): resolve it first "
                        "(flash --recover)")
        else:
            errs.append("this batch's own pair is still reserved: a retry first completes a verified "
                        "rollback (flash --rollback)")
    if checkpoint_pending(state):
        errs.append("an interrupted baseline checkpoint has not written known-good.json yet: run flash --recover")
    if kind in ("batch", "calibration", "experiment") and not state.get("baseline_closed"):
        errs.append("no closed baseline in slot-state.json; run the baseline first")
    return errs


def reserve(state: dict, batch_id: str, kind: str, fallback_boot_sha256: str, at: str) -> dict:
    """Fix the pair before the first write. Returns the pair."""
    if state.get("pair"):
        raise SlotError("a pair is already reserved")
    kg = state["known_good_slot"]
    target = other(kg)
    if target == kg:
        raise SlotError("write target is known_good_slot")
    if not fallback_boot_sha256:
        raise SlotError("need the fallback slot's boot hash before writing")
    pair = {"batch": batch_id, "kind": kind, "target": target, "fallback": kg,
            "fallback_boot_sha256": fallback_boot_sha256, "created_at": at,
            "written": False, "attempts": []}
    state["pair"] = pair
    _event(state, "reserve", at, batch=batch_id, kind=kind, target=target, fallback=kg)
    return pair


def mark_written(state: dict, attempt_id: str, at: str) -> None:
    pair = state.get("pair")
    if not pair:
        raise SlotError("no pair reserved")
    pair["written"] = True
    pair["attempts"].append(attempt_id)
    _event(state, "write", at, batch=pair["batch"], attempt=attempt_id)


def rolled_back(state: dict, verified_slot: str, how: str, at: str) -> None:
    """A verified rollback to known_good_slot releases the pair."""
    pair = state.get("pair")
    if not pair:
        raise SlotError("no pair to roll back")
    if verified_slot != state["known_good_slot"] or verified_slot != pair["fallback"]:
        raise SlotError(f"rollback verified slot {verified_slot}, expected {pair['fallback']}")
    _event(state, "rolled-back", at, batch=pair["batch"], kind=pair["kind"], how=how)
    state["pair"] = None


UNSTAMPED = "unstamped"


def _known_good(state: dict, pair: dict, batch_id: str, build_id: str | None, boot_sha256: str, at: str,
                identity: dict | None) -> None:
    state["known_good_slot"] = pair["target"]
    state["known_good"] = {"slot": pair["target"], "provisional": False, "build_id": build_id,
                           "boot_sha256": boot_sha256, "batch": batch_id, "recorded_at": at, "identity": identity}
    state["pair"] = None


def promote(state: dict, batch_id: str, build_id: str, boot_sha256: str, at: str) -> None:
    """The batch was merged and tagged, recorded durably; the candidate slot,
    with its build ID confirmed and its boot hash recorded, is known-good.
    Only a batch pair is promoted: a baseline closes through close_baseline,
    every other kind rolls back."""
    pair = state.get("pair")
    if not pair or pair["batch"] != batch_id:
        raise SlotError(f"no reserved pair for batch {batch_id}")
    if pair["kind"] != "batch":
        raise SlotError(f"a {pair['kind']} pair is never promoted")
    if not build_id or build_id == UNSTAMPED or not boot_sha256:
        raise SlotError("promotion needs the candidate's stamped build ID and boot hash")
    _known_good(state, pair, batch_id, build_id, boot_sha256, at, None)
    _event(state, "promote", at, batch=batch_id, slot=pair["target"])


def close_baseline(state: dict, batch_id: str, build_id: str | None, boot_sha256: str, at: str, *,
                   seed: dict, identity: dict | None = None) -> None:
    """A passing baseline closes its own pair at once (no PRs to check).

    The known-good seed is saved in slot-state.json in the same write as the
    slot promotion, marked unwritten; finish_checkpoint() then writes
    known-good.json. An unstamped baseline (the design's fallback) is
    identified by its dates, slot and boot hash in ``identity``."""
    pair = state.get("pair")
    if not pair or pair["kind"] != "baseline" or pair["batch"] != batch_id:
        raise SlotError("no baseline pair for this batch")
    if not boot_sha256:
        raise SlotError("closing a baseline needs its boot hash")
    stamped = bool(build_id) and build_id != UNSTAMPED
    if not stamped:
        dates = (identity or {}).get("dates") or {}
        if not all(dates.get(k) for k in ("build", "vendor", "bootimage")) or (identity or {}).get("slot") != pair["target"]:
            raise SlotError("an unstamped baseline needs its build dates and slot as identity")
    if not seed:
        raise SlotError("closing a baseline needs the known-good seed")
    _known_good(state, pair, batch_id, build_id if stamped else None, boot_sha256, at, identity)
    state["baseline_closed"] = {"batch": batch_id, "build_id": build_id if stamped else None, "at": at,
                                "seed": seed, "seed_written": False}
    _event(state, "close-baseline", at, batch=batch_id, slot=state["known_good_slot"],
           identity="build ID" if stamped else "dates, slot and boot hash")


def checkpoint_pending(state: dict | None) -> dict | None:
    """The seed of an interrupted baseline checkpoint, if any."""
    bc = (state or {}).get("baseline_closed") or {}
    return bc.get("seed") if bc.get("seed_written") is False else None


def seed_written(state: dict, at: str) -> None:
    state["baseline_closed"]["seed_written"] = True
    _event(state, "seed-written", at, batch=state["baseline_closed"]["batch"])


# --- recovery ----------------------------------------------------------------

PROMOTE, ROLLBACK, HALT = "promote", "rollback", "halt"


BRANCH = "river-22.2"
OWNER = "zrm625"


def expected_pr_set(batch: dict, slugs: dict[str, str]) -> tuple[dict | None, list[str]]:
    """The complete expected PR set from the runner's ``prs`` and ``no_pr``
    (review round 3): {path: {"repo": owner/name, "number": N | None}} for
    every repo of commit_set, or (None, why). Every repo needs exactly one
    of a PR or an explicit "no PR"; a PR outside the commit set, or a repo
    outside zrm625, is refused."""
    cs = batch.get("commit_set") or {}
    if not cs:
        return None, ["batch.json has no tested commit set"]
    errs, by_slug = [], {}
    for path in sorted(cs):
        s = slugs.get(path)
        if not s:
            errs.append(f"{path}: no GitHub repo known for it in default.xml")
        elif not s.startswith(OWNER + "/"):
            errs.append(f"{path}: {s} is not a {OWNER} repo")
        else:
            by_slug[s] = path
    out: dict = {}
    for pr in batch.get("prs") or []:
        s, n = pr.get("repo"), pr.get("number")
        path = by_slug.get(s)
        if path is None:
            errs.append(f"{s}: has a PR but is not in the tested commit set")
        elif not isinstance(n, int) or isinstance(n, bool):
            errs.append(f"{path}: PR number {n!r} is not a number")
        elif path in out:
            errs.append(f"{path}: more than one PR recorded")
        else:
            out[path] = {"repo": s, "number": n}
    for path in batch.get("no_pr") or []:
        if path not in cs:
            errs.append(f"{path}: in no_pr but not in the tested commit set")
        elif path in out:
            errs.append(f"{path}: has both a PR and no_pr")
        elif path in by_slug.values():
            out[path] = {"repo": slugs[path], "number": None}
    for path in sorted(set(by_slug.values()) - set(out)):
        errs.append(f"{path}: neither a PR nor an explicit no_pr")
    return (None, errs) if errs else (out, [])


def frozen_pr_set(batch: dict) -> dict | None:
    ms = batch.get("merge_started")
    return ms.get("expected_prs") if isinstance(ms, dict) else None


def frozen_base(batch: dict) -> dict:
    """The base (river-22.2 of each repo when the runner claimed the batch),
    frozen by flash --merge-started; {} for a batch without one."""
    ms = batch.get("merge_started")
    return (ms.get("base") or {}) if isinstance(ms, dict) else {}


IF_PRESENT = "if-present"


def commit_set_proof(batch: dict, lookup: dict, slugs: dict[str, str],
                     require_tags: bool | str = True) -> list[str]:
    """Why the lookup does not prove the batch's tested commit set was
    merged and tagged (C1, review rounds 2 and 3, Sol B2). Empty only if:

    - merge_started froze the complete expected PR set (expected_pr_set),
      it still covers exactly the commit set, and the runner's current
      ``prs``/``no_pr`` still give the same set (a repo can't drop from "PR"
      to "no PR" afterwards);
    - every expected PR was looked up, its head is the tested commit, it is
      merged into river-22.2 of its own zrm625 repo, and the tag points at
      its merge commit;
    - every repo without a PR has the tag at exactly the tested commit;
    - in every repo, the tagged tree is the tested commit's tree, so a merge
      into a branch that had moved on (content never tested) is caught;
    - the batch runner's landed proof (R10.4a, I4), read now: every expected
      PR is from the same repository (``isCrossRepository`` false), its
      repo's river-22.2 is at its merge commit, and that commit's parents
      are [base, tested head] (base as frozen at merge_started; without
      one, any first parent) with the tested tree; a repo without a PR
      still has river-22.2 at its tested commit.

``require_tags=IF_PRESENT`` (the lag check, which may run before the
runner tags) allows an absent tag but checks a present one exactly like
``require_tags=True`` (Sol SM2: a wrong tag is never masked by the lag).
``require_tags=False`` checks everything but the tags.

    A PR or tag repo outside the commit set is a stale or incomplete
    record."""
    cs = batch.get("commit_set") or {}
    if not cs:
        return ["batch.json has no tested commit set"]
    expected = frozen_pr_set(batch)
    if not expected:
        return ["merge_started did not record the expected PR set before merging"]
    errs = []
    if set(expected) != set(cs):
        errs.append("the expected PR set does not cover exactly the tested commit set "
                    f"(missing {sorted(set(cs) - set(expected))}, extra {sorted(set(expected) - set(cs))})")
    now, why = expected_pr_set(batch, slugs)
    if now is None:
        errs.extend(f"current PR record: {w}" for w in why)
    elif now != expected:
        changed = sorted(p for p in set(now) | set(expected) if now.get(p) != expected.get(p))
        errs.append(f"the PR record changed after merge_started for {changed}")
    want = {}
    for path, sha in sorted(cs.items()):
        s = slugs.get(path)
        if not s:
            errs.append(f"{path}: no GitHub repo known for it in default.xml")
        elif (expected.get(path) or {}).get("repo") not in (None, s):
            errs.append(f"{path}: expected PR is in {expected[path]['repo']}, not its own repo {s}")
        else:
            want[s] = (path, sha)
    prs, tags = {}, {}
    for p in lookup.get("prs") or []:
        if p.get("repo") in prs:
            errs.append(f"{p.get('repo')}: more than one PR recorded")
        prs[p.get("repo")] = p
    for t in lookup.get("tags") or []:
        tags[t.get("repo")] = t
    trees = lookup.get("tested_trees") or {}
    tips = lookup.get("tips") or {}
    tip_commits = lookup.get("tip_commits") or {}
    base = frozen_base(batch)
    for s in sorted(set(prs) - set(want)):
        errs.append(f"{s}: has a PR but is not in the tested commit set (stale record)")
    for s in sorted(set(tags) - set(want)):
        errs.append(f"{s}: in tag_repos but not in the tested commit set (stale record)")
    for s, (path, sha) in sorted(want.items()):
        exp = expected.get(path) or {}
        t = tags.get(s)
        check_tag = require_tags is True or (require_tags == IF_PRESENT and bool(t) and bool(t.get("present")))
        if require_tags is True:
            if t is None:
                errs.append(f"{path}: not in tag_repos")
                continue
            if not t.get("present"):
                errs.append(f"{path}: tag missing")
                continue
        if check_tag:
            if not t.get("tree") or t.get("tree") != trees.get(s):
                errs.append(f"{path}: tagged tree {str(t.get('tree'))[:12]} is not the tested tree "
                            f"{str(trees.get(s))[:12]} (merged into a branch that had moved on?)")
        p = prs.get(s)
        tip = tips.get(s)
        if exp.get("number") is None:
            if p is not None:
                errs.append(f"{path}: expected no PR, but PR {p.get('number')} was looked up")
                continue
            if check_tag and t.get("commit") != sha:
                errs.append(f"{path}: no PR, so the tag must be at the tested commit {sha[:12]}, "
                            f"not {str(t.get('commit'))[:12]}")
            if tip != sha:
                errs.append(f"{path}: no PR, but river-22.2 is at {str(tip)[:12]}, not the tested commit {sha[:12]}")
            continue
        if p is None or p.get("number") != exp["number"]:
            errs.append(f"{path}: expected PR {exp['number']} was not looked up")
            continue
        if p.get("head") != sha:
            errs.append(f"{path}: PR head {str(p.get('head'))[:12]} is not the tested commit {sha[:12]}")
        if p.get("base") != BRANCH:
            errs.append(f"{path}: PR {exp['number']} targets {p.get('base')!r}, not {BRANCH}")
        if p.get("cross") is not False:
            errs.append(f"{path}: PR {exp['number']} is not from {s} itself (isCrossRepository "
                        f"{p.get('cross')!r})")
        if not p.get("merged") or not p.get("merge_commit"):
            errs.append(f"{path}: PR not merged")
            continue
        m = p["merge_commit"]
        if check_tag and t.get("commit") != m:
            errs.append(f"{path}: tag at {str(t.get('commit'))[:12]}, merge commit {m[:12]}")
        if tip != m:
            errs.append(f"{path}: river-22.2 is at {str(tip)[:12]}, not the merge commit {m[:12]}")
            continue
        c = tip_commits.get(s) or {}
        parents = c.get("parents") or []
        want_parents = [base[path], sha] if base.get(path) else [parents[0] if parents else None, sha]
        if len(parents) != 2 or parents != want_parents:
            errs.append(f"{path}: merge commit {m[:12]} has parents {[x[:12] for x in parents]}, not "
                        f"[{'base ' + base[path][:12] if base.get(path) else 'base'}, tested {sha[:12]}]")
        if not c.get("tree") or c.get("tree") != trees.get(s):
            errs.append(f"{path}: merge commit tree {str(c.get('tree'))[:12]} is not the tested tree "
                        f"{str(trees.get(s))[:12]}")
    return errs


def landed_but_open(batch: dict, lookup: dict, slugs: dict[str, str]) -> list[str] | None:
    """I4 and Sol B2: the expected PRs GitHub still reports OPEN although
    their merge is on river-22.2 (lagging reads), when that lag is the only
    thing between the lookup and the runner's landed proof. Returns the
    lagging PRs ("repo#n"), or None if there is no lag or anything else is
    wrong. Tags are not needed: the runner tags only after it saw MERGED."""
    tips = lookup.get("tips") or {}
    expected = frozen_pr_set(batch) or {}
    by_key = {(e["repo"], e["number"]) for e in expected.values() if e.get("number") is not None}
    lag, prs = [], []
    for p in lookup.get("prs") or []:
        if p.get("state") == "OPEN" and (p.get("repo"), p.get("number")) in by_key and tips.get(p.get("repo")):
            lag.append(f"{p['repo']}#{p['number']}")
            p = dict(p, merged=True, merge_commit=tips[p["repo"]])
        prs.append(p)
    if not lag:
        return None
    if commit_set_proof(batch, dict(lookup, prs=prs), slugs, require_tags=IF_PRESENT):
        return None
    return lag


def decide_recovery(pair: dict, lookup: dict | None, batch: dict | None = None,
                    slugs: dict[str, str] | None = None) -> tuple[str, str]:
    """What to do with an unresolved pair at the start of a batch.

    ``lookup`` is None when the GitHub/tag lookup failed (network or gh down),
    otherwise recovery.lookup()'s result. ``batch`` is batch.json (its
    commit_set and merge_started); ``slugs`` maps its repo paths to GitHub
    repos (stage.repo_slugs()).

    - Every repo of the tested commit set proven merged and tagged against
      the PR set frozen at merge_started (commit_set_proof): promote.
    - Nothing merged and nothing tagged (or nothing recorded at all), and no
      merge_started: the run stopped before merging, so roll back.
    - Anything else, including merge_started with incomplete evidence and a
      stale or incomplete prs/tag_repos record: halt.
    """
    kind = pair.get("kind")
    if kind in ("calibration", "experiment", "baseline"):
        return ROLLBACK, f"{kind} pairs never have PRs: roll back"
    if kind != "batch":
        return HALT, f"unknown pair kind {kind!r}"
    if lookup is None:
        return HALT, "merge/tag lookup failed; a failed lookup never counts as 'none merged'"
    batch = batch or {}
    ms = batch.get("merge_started")
    started = ((ms.get("at") if isinstance(ms, dict) else None) or "an unrecorded time") if ms else None
    prs, tags = lookup.get("prs") or [], lookup.get("tags") or []
    if not any(p.get("merged") for p in prs) and not any(t.get("present") for t in tags):
        what = "no PRs or tag repos recorded" if not prs and not tags else "no PR merged and no tag"
        if not started:
            return ROLLBACK, f"{what}, and no merge_started: stopped before merging: roll back"
        return HALT, f"{what}, but merge_started at {started}: interrupted mid-merge; needs Zak"
    errs = commit_set_proof(batch, lookup, slugs or {})
    if not errs:
        return PROMOTE, ("every repo of the tested commit set is merged into river-22.2 and tagged "
                         "(PR heads are the tested commits; tags at merge commits or tested commits; "
                         "tagged trees are the tested trees)")
    return HALT, ("not proven: " + "; ".join(errs[:8]) + (f"; +{len(errs) - 8} more" if len(errs) > 8 else "")
                  + "; needs Zak")
