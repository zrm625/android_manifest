"""Outcome rules: build classes (R7.6), adopting a flash attempt and the
rescue evidence (R8.6, I3), binding a QA result (R8.5), the desk-suspect
rule (R9.5), and applying an outcome to the members (R9.2-R9.4, R11.2 2d).

Pure functions over dicts; the caller reads the files and commits.
"""

from __future__ import annotations

import datetime as dt
import re

from . import state as S

PASS, FAIL, VOID, TRANSPORT, HALT, DESK = "pass", "fail", "void", "transport", "halt", "desk-suspect"


# --- R7.6 builds --------------------------------------------------------------------

def build_class(script_rc: int, bench_result: str | None, low_disk: bool = False) -> str:
    """'ok', 'm-failed' (rebuild once, then fail) or 'void'."""
    if low_disk:
        return "void"
    if bench_result is None:
        return "void"                           # no .bench-result: scripts/build never ran m, or was killed
    try:
        code = int(bench_result.split()[0])
    except (ValueError, IndexError):
        return "void"
    if code == 0:
        return "ok" if script_rc == 0 else "void"
    if 1 <= code <= 89 or 92 <= code <= 124:
        return "m-failed"
    return "void"                               # 90 breakfast, 91 installclean, 125-127, >=128 killed (137 OOM)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def fingerprint(log_text: str) -> dict:
    """The first ``FAILED:`` line and the first ``error:`` line after it,
    ANSI codes and ``/src/`` stripped."""
    text = ANSI_RE.sub("", log_text).replace("/src/", "")
    lines = text.splitlines()
    failed = next((i for i, ln in enumerate(lines) if "FAILED:" in ln), None)
    err = None
    if failed is not None:
        err = next((ln.strip() for ln in lines[failed + 1:] if "error:" in ln), None)
    return {"failed": lines[failed].strip() if failed is not None else None, "error": err}


# --- R8.6 adoption and rescue evidence -----------------------------------------------

def _ts(s: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def last_slot_event(slot_state: dict, kinds=("reserve", "rolled-back", "promote", "close-baseline")) -> dict | None:
    """The last slot-changing history event (log-only events skipped)."""
    for ev in reversed((slot_state or {}).get("history") or []):
        if ev.get("event") in kinds:
            return ev
    return None


def rescue_evidence(slot_state: dict, wipes: list[dict], flash_rec: dict, batch_id: str,
                    attempt_id: str | None) -> tuple[bool, str]:
    """R8.6 (G-C6, I3): a verified rescue of this attempt, or (False, why).

    slot-state.json has no pair; its last rolled-back event names this batch,
    its ``how`` starts with ``rescue:`` and contains ``wipe #<seq>``; the
    wipes.jsonl record with that seq has ``ok`` true, ``how`` ``rollback``,
    the attempt ID, and ``at`` no earlier than the attempt's ``started_at``.
    Both exit-4 paths of flash (a failed boot and a bootloader fallback, I3)
    and device-bench's bench-boot failure (H17) leave exactly this."""
    if not attempt_id:
        return False, "no attempt ID"
    if (slot_state or {}).get("pair"):
        return False, "slot-state.json still holds a pair"
    ev = next((e for e in reversed((slot_state or {}).get("history") or []) if e.get("event") == "rolled-back"),
              None)
    if not ev or ev.get("batch") != batch_id:
        return False, "the last rollback is not this batch's"
    how = ev.get("how") or ""
    if not how.startswith("rescue:"):
        return False, f"the last rollback of {batch_id} is not a rescue ({how[:60]!r})"
    m = re.search(r"wipe #(\d+)", how)
    if not m:
        return False, "the rescue names no wipe"
    seq = int(m.group(1))
    w = next((x for x in wipes if x.get("seq") == seq), None)
    if not w or not w.get("ok") or w.get("how") != "rollback" or w.get("attempt_id") != attempt_id:
        return False, f"wipe #{seq} is not this attempt's rollback wipe"
    att = next((a for a in (flash_rec or {}).get("attempts") or [] if a.get("attempt_id") == attempt_id), None)
    start, at = _ts((att or {}).get("started_at")), _ts(w.get("at"))
    if not att or not start or not at or at < start:
        return False, f"wipe #{seq} is older than attempt {attempt_id}"
    return True, f"rescue verified: wipe #{seq} of attempt {attempt_id}"


def adopt_flash(rc: int, batch: dict, flash_rec: dict, slot_state: dict, wipes: list[dict],
                batch_id: str) -> tuple[str, str | None, str]:
    """R8.6 by flash's exit code: ('adopt', attempt, why), ('rescued',
    attempt, why), ('void', None, why) or ('halt', attempt?, why)."""
    atts = (flash_rec or {}).get("attempts") or []
    last = atts[-1] if atts else None
    fa = batch.get("flash_attempt")
    pair = (slot_state or {}).get("pair")
    if rc == 0:
        if not last or last.get("result") != "booted" or last.get("attempt_id") != fa:
            return "halt", fa, "flash exited 0 but its last attempt is not booted or not batch.json's"
        if not pair or pair.get("batch") != batch_id or not pair.get("attempts") or pair["attempts"][-1] != fa:
            return "halt", fa, "flash exited 0 but the slot pair does not name this attempt"
        if last.get("target") != pair.get("target"):
            return "halt", fa, "the attempt's target is not the pair's target"
        return "adopt", fa, "flash attempt adopted"
    if rc == 4:
        if not fa or not last or last.get("attempt_id") != fa:
            return "halt", fa, "flash exited 4 but batch.json's attempt is not the last in flash.json"
        ok, why = rescue_evidence(slot_state, wipes, flash_rec, batch_id, fa)
        return ("rescued" if ok else "halt"), fa, why
    if rc == 1 and not fa and not atts and not pair:
        return "void", None, "flash refused before it touched the phone"
    return "halt", fa, f"flash exited {rc} with an attempt or a pair"


# --- R8.5 binding ----------------------------------------------------------------------

def bind(results: dict | None, h6: dict | None, batch_id: str, attempt_id: str, harness: str,
         slot_state: dict) -> tuple[bool, str]:
    """A QA result counts only if every binding holds (R8.5)."""
    if not results or not h6:
        return False, "no results.json or no H6 line"
    pair = (slot_state or {}).get("pair") or {}
    checks = [("mode", results.get("mode") == "batch"), ("batch", results.get("batch") == batch_id),
              ("run_id", results.get("run_id") == h6.get("run_id")),
              ("flash_attempt", results.get("flash_attempt") == attempt_id),
              ("harness_version", results.get("harness_version") == harness),
              ("serial", results.get("serial") == "ZY225DPT7F"),
              ("pair", pair.get("batch") == batch_id)]
    bad = [n for n, ok in checks if not ok]
    return (not bad), ("bound" if not bad else "unbound: " + ", ".join(bad))


def failing_checks(results: dict) -> list[str]:
    req = results.get("required") or []
    res = results.get("results") or {}
    return sorted(c for c in req if (res.get(c) or {}).get("state") != "PASS")


# --- R9.5 desk suspect --------------------------------------------------------------------

def desk_suspect(last_fail: dict | None, members: list[str], checks: list[str], content_key: str) -> str | None:
    """The shared check that makes this fail a desk suspect, or None."""
    if not last_fail or content_key == last_fail.get("content_key"):
        return None
    shared = sorted(set(checks) & set(last_fail.get("checks") or []))
    if not shared:
        return None
    if set(members) <= set(last_fail.get("members") or []):
        return None                     # a split of the same batch
    return shared[0]


# --- applying an outcome to the entries -----------------------------------------------------

def members_of(st: dict, running: dict) -> list[dict]:
    out = []
    for m in running.get("members") or []:
        e = S.entry(st, m["name"])
        if e and e["status"] == "in-batch" and e.get("batch") == running["id"]:
            out.append(e)
    return out


def return_members(st: dict, running: dict, reason: str, *, solo: bool | None = None) -> None:
    """Members go back to ``queued`` at their positions, nothing recorded.
    Eligibility is left to the claim, which runs it before any member is
    used (R5.4: a return path that cannot complete the check)."""
    for e in members_of(st, running):
        if solo is not None:
            e.setdefault("flags", {})["solo"] = solo
        e["batch"] = None
        S.set_status(e, "queued", reason)


def apply(st: dict, running: dict, outcome: dict) -> None:
    """Apply a committed outcome of a run that will not merge (R9.2-R9.5,
    R11.2 step 2d). ``outcome``: {"class", "checks", "evidence", "reason",
    "desk_check"?}."""
    cls = outcome.get("class")
    purpose = running.get("purpose")
    rid = running["id"]
    if purpose not in ("batch", "experiment"):
        return                                              # qualify, stage: borrowed entries untouched
    mem = members_of(st, running)
    if purpose == "experiment" and cls in (PASS, FAIL):
        for e in mem:                                       # R9.2: always rollback; Zak decides
            e["batch"] = None
            S.set_status(e, "waiting-zak", f"experiment {rid}: {cls}; " + (outcome.get("reason") or ""))
            e["experiment_result"] = outcome.get("evidence")
        return
    if cls == DESK:
        return_members(st, running, f"desk suspect in {rid}")
        S.set_pause(st, outcome.get("reason") or "desk suspect", step="decided")
        return
    if cls == FAIL:
        st["voids_in_a_row"] = 0
        if len(mem) > 1:                                    # R9.3: split
            return_members(st, running, f"{rid} failed with {len(mem)} changes: split, runs alone", solo=True)
            return
        for e in mem:                                       # R9.4: one change failed
            idx = S.add_failure(st, e, {**(outcome.get("evidence") or {}), "batch": rid})
            e["batch"] = None
            e["last_evidence_index"] = idx
            S.set_status(e, "failed", f"failed in {rid}: {', '.join(outcome.get('checks') or [])}")
        return
    if cls == TRANSPORT:
        return_members(st, running, f"{rid} aborted: GitHub or the network gave no answer")
        if running.get("reached_prs_open"):
            st["strikes"]["transport_prs"] += 1
            if st["strikes"]["transport_prs"] >= 2:
                S.set_pause(st, "GitHub failed twice after batch PRs opened", step="transport")
        return
    # void, an aborted pass that never reached merge_started, anything else
    return_members(st, running, f"{rid} void: {outcome.get('reason') or 'aborted'}")
    st["voids_in_a_row"] += 1
    if st["voids_in_a_row"] >= 2:
        S.set_pause(st, f"two voids in a row (last: {rid}: {outcome.get('reason') or 'aborted'})", step="void")


def decide_fail(st: dict, running: dict, checks: list[str], evidence: dict) -> dict:
    """The phase-10 decision for a ``fail`` (R9.5 included): returns the
    outcome to commit and updates ``last_fail``."""
    if running.get("purpose") != "batch":
        return {"class": FAIL, "checks": checks, "evidence": evidence}
    names = [m["name"] for m in running.get("members") or []]
    shared = desk_suspect(st.get("last_fail"), names, checks, running.get("content_key"))
    if shared:
        lf = st["last_fail"]
        return {"class": DESK, "checks": checks, "evidence": evidence,
                "reason": f"desk suspect: {shared} (failed in {lf['batch']} and {running['id']} with different "
                          "changes)"}
    st["last_fail"] = {"members": names, "checks": checks, "batch": running["id"],
                       "content_key": running.get("content_key")}
    return {"class": FAIL, "checks": checks, "evidence": evidence}
