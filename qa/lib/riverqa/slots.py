"""slot-state.json: the slot pair, known_good_slot, and how a pair is resolved.

Pure transitions on a dict; flash does the I/O. Schema (version 1):

    {
      "version": 1,
      "known_good_slot": "a" | "b",
      "known_good": {"slot", "provisional", "build_id", "boot_sha256",
                     "batch", "recorded_at"},
      "baseline_closed": null | {"batch", "build_id", "at"},
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


def preflight(state: dict | None, batch_id: str, kind: str, running_slot: str) -> list[str]:
    """Problems that forbid a flash for this batch. Empty list: go."""
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
    kg = state.get("known_good_slot")
    if running_slot != kg:
        errs.append(f"running slot {running_slot} is not known_good_slot {kg}")
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


def promote(state: dict, batch_id: str, build_id: str, boot_sha256: str, at: str) -> None:
    """The batch was merged and tagged, recorded durably; the candidate slot,
    with its build ID confirmed and its boot hash recorded, is known-good."""
    pair = state.get("pair")
    if not pair or pair["batch"] != batch_id:
        raise SlotError(f"no reserved pair for batch {batch_id}")
    if pair["kind"] not in ("batch", "baseline"):
        raise SlotError(f"a {pair['kind']} pair is never promoted")
    if not build_id or not boot_sha256:
        raise SlotError("promotion needs the candidate's build ID and boot hash")
    state["known_good_slot"] = pair["target"]
    state["known_good"] = {"slot": pair["target"], "provisional": False, "build_id": build_id,
                           "boot_sha256": boot_sha256, "batch": batch_id, "recorded_at": at}
    _event(state, "promote", at, batch=batch_id, slot=pair["target"])
    state["pair"] = None


def close_baseline(state: dict, batch_id: str, build_id: str, boot_sha256: str, at: str) -> None:
    """A passing baseline closes its own pair at once (no PRs to check)."""
    pair = state.get("pair")
    if not pair or pair["kind"] != "baseline" or pair["batch"] != batch_id:
        raise SlotError("no baseline pair for this batch")
    promote(state, batch_id, build_id, boot_sha256, at)
    state["baseline_closed"] = {"batch": batch_id, "build_id": build_id, "at": at}


# --- recovery ----------------------------------------------------------------

PROMOTE, ROLLBACK, HALT = "promote", "rollback", "halt"


def decide_recovery(pair: dict, lookup: dict | None) -> tuple[str, str]:
    """What to do with an unresolved pair at the start of a batch.

    ``lookup`` is None when the GitHub/tag lookup failed (network or gh down),
    otherwise {"prs": [{"url", "merged": bool}], "tags": [{"repo", "present": bool}]}.
    """
    kind = pair.get("kind")
    if kind in ("calibration", "experiment", "baseline"):
        return ROLLBACK, f"{kind} pairs never have PRs: roll back"
    if kind != "batch":
        return HALT, f"unknown pair kind {kind!r}"
    if lookup is None:
        return HALT, "merge/tag lookup failed; a failed lookup never counts as 'none merged'"
    prs, tags = lookup.get("prs") or [], lookup.get("tags") or []
    if not prs or not tags:
        return HALT, "batch.json lists no PRs or no tag repos for this batch"
    merged = [p["merged"] for p in prs]
    tagged = [t["present"] for t in tags]
    if all(merged) and all(tagged):
        return PROMOTE, "every PR merged and every repo tagged"
    if not any(merged) and not any(tagged):
        return ROLLBACK, "no PR merged and no tag: roll back"
    return HALT, (f"partly merged/tagged ({sum(merged)}/{len(merged)} PRs, {sum(tagged)}/{len(tagged)} tags); "
                  "needs Zak")
