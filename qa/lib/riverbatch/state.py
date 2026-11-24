"""The runner's state file, $BENCH_HOME/runner/state.json (R4).

Every change is one **transaction**: ``flock`` on ``runner/state.lock``,
read, apply one transition, write a temp file, fsync, rename, fsync the
directory (riverqa.core.write_json). A line then goes to
``runner/events.jsonl``, which nothing reads back. Outside readers use
riverqa.runnerstate (shared lock, fail closed).

Schema (version 6)::

    {"version": 6, "next_batch": 1, "next_qual": 1, "migrated": null,
     "halt": null, "pause": null, "last_error": null, "voids_in_a_row": 0,
     "last_fail": null,
     "strikes": {"recover": 0, "journal": 0, "setup": 0, "transport_prs": 0, "lookup": 0},
     "setup_wipe_seq": null, "rebaseline": null,
     "failure_history": {},       # canonical identity (sorted PR keys) -> [evidence], append-only (I13)
     "aliases": {},               # PR key -> change name, append-only (I13)
     "cleanup": [], "reports_pending": [], "lease": null, "running": null, "entries": [],
     "blocker": null}             # why the last tick could not run a batch: {"kind", "message", "at", "report"}

Every entry has an immutable ``id`` (``e<position>``; positions are never
reused and entries are never deleted). A run's members name it: ``{"name",
"id", "rev", "record_sha256"}``. Every compare-and-update of an entry checks
the ID and the rev (Sol SB1): a withdrawn entry followed by a new one under
the same name is another entry. An older state file gets the IDs on read
(``_backfill``).

A cleanup item (R13.3, I12)::

    {"batch": "b0007", "branch": "batch/b0007",
     "tested": {path: sha},          # the composed (tested) head of every touched repo, kept to the end
     "branch_repos": {path: sha},    # batch branches still to delete, at that SHA only
     "pr_repos": {path: {"repo", "number"}},       # batch PRs known open
     "expected_merged": {path: {"repo", "number"}},  # batch PRs that landed through the runner or a settle
     "landed_open": {path: {"repo", "number"}},      # landed (ancestry) but GitHub still says OPEN: wait,
                                                     # never close, keep the branch (Sol SM1)
     "hold": null | {"settle": "b0007", "why": ...}}   # Zak's repair first (R13.4): nothing is closed
                                                       # or deleted while that re-baseline is owed
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os

from riverqa.core import append_jsonl, now_iso, write_json

from . import paths

VERSION = 6
FINAL = ("merged", "withdrawn")
STATUSES = ("queued", "in-batch", "failed", "merged", "tried", "needs-reapproval", "waiting-zak", "withdrawn")
STRIKES = ("recover", "journal", "setup", "transport_prs", "lookup")


class StateError(RuntimeError):
    pass


class Refused(RuntimeError):
    """A command refused: exit 1, nothing written."""


def empty() -> dict:
    return {"version": VERSION, "next_batch": 1, "next_qual": 1, "migrated": None,
            "halt": None, "pause": None, "last_error": None, "voids_in_a_row": 0, "last_fail": None,
            "strikes": {k: 0 for k in STRIKES}, "setup_wipe_seq": None, "rebaseline": None,
            "failure_history": {}, "aliases": {}, "cleanup": [], "reports_pending": [],
            "lease": None, "running": None, "entries": [], "blocker": None}


def _parse(text: str) -> dict:
    try:
        st = json.loads(text)
    except ValueError as e:
        raise StateError(f"{paths.state_file()} does not parse: {e}") from e
    if not isinstance(st, dict) or st.get("version") != VERSION:
        raise StateError(f"{paths.state_file()} is not a version {VERSION} state file")
    base = empty()
    for k, v in base.items():
        st.setdefault(k, copy.deepcopy(v))
    for k in STRIKES:
        st["strikes"].setdefault(k, 0)
    _backfill(st)
    return st


def _backfill(st: dict) -> None:
    """IDs for entries and running members written before they existed
    (Sol SB1). An entry's ID is ``e<position>``; a member without one is the
    entry this run claimed (in-batch under this run's ID), else none."""
    for i, e in enumerate(st["entries"]):
        if not e.get("id"):
            e["id"] = f"e{e['position']}" if isinstance(e.get("position"), int) else f"x{i}"
    run = st.get("running")
    for m in (run or {}).get("members") or []:
        if not m.get("id"):
            x = next((e for e in reversed(st["entries"]) if e["name"] == m.get("name")
                      and e.get("status") == "in-batch" and e.get("batch") == run.get("id")), None)
            m["id"] = x["id"] if x else None
            if x and "record_sha256" not in m:
                m["record_sha256"] = x.get("record_sha256")


def read() -> dict:
    """Read without the lock (the runner itself, between transactions)."""
    p = paths.state_file()
    if not p.exists():
        return empty()
    return _parse(p.read_text())


@contextlib.contextmanager
def transaction(event: str | None = None, **info):
    """``with transaction("claim", batch=...) as st:`` mutate ``st``; it is
    written atomically on a clean exit, never on an exception."""
    d = paths.runner()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.state_lock(), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        st = read()
        yield st
        write_json(paths.state_file(), st)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    if event:
        try:
            append_jsonl(paths.events_file(), {"at": now_iso(), "event": event, **info})
        except OSError:
            pass


def cleanup_item(bid: str, tested: dict, branch_repos: dict | None = None, pr_repos: dict | None = None,
                 expected_merged: dict | None = None, hold: dict | None = None,
                 landed_open: dict | None = None) -> dict:
    return {"batch": bid, "branch": f"batch/{bid}", "tested": dict(tested),
            "branch_repos": dict(branch_repos or {}), "pr_repos": dict(pr_repos or {}),
            "expected_merged": dict(expected_merged or {}), "hold": hold,
            "landed_open": dict(landed_open or {})}


# --- entries -------------------------------------------------------------------

def entry(st: dict, name: str) -> dict | None:
    """The newest entry under ``name`` (a withdrawn one may be followed by a new one)."""
    found = [e for e in st["entries"] if e["name"] == name]
    return found[-1] if found else None


def live_entry(st: dict, name: str) -> dict | None:
    e = entry(st, name)
    return e if e and e["status"] not in FINAL else None


def entry_by_id(st: dict, eid: str | None) -> dict | None:
    if not eid:
        return None
    return next((e for e in st["entries"] if e.get("id") == eid), None)


def same_entry(st: dict, ref: dict) -> dict | None:
    """The entry ``ref`` (an entry or a run member) names, by its immutable
    ID, if it still has ``ref``'s name, rev and record (Sol SB1); else
    None."""
    x = entry_by_id(st, ref.get("id"))
    if not x or x["name"] != ref.get("name") or x.get("rev") != ref.get("rev"):
        return None
    if "record_sha256" in ref and x.get("record_sha256") != ref["record_sha256"]:
        return None
    return x


def member_entry(st: dict, run: dict, m: dict) -> dict | None:
    """The entry of run member ``m``: by ID, still claimed by this run and
    under the record it was claimed with; else None (Sol SB1)."""
    # A member written before IDs (none after _backfill on read) is the
    # entry of its name that this run claimed.
    x = entry_by_id(st, m["id"]) if "id" in m else entry(st, m.get("name"))
    if not x or x["name"] != m.get("name") or x.get("batch") != run.get("id"):
        return None
    if m.get("record_sha256") is not None and x.get("record_sha256") != m["record_sha256"]:
        return None
    return x


def new_entry_id(st: dict, position: int) -> str:
    eid = f"e{position}"
    if entry_by_id(st, eid):
        raise StateError(f"entry ID {eid} is taken")
    return eid


def set_status(e: dict, status: str, reason: str | None = None, *, by: str = "runner", action: str | None = None,
               decision_reason: str | None = None, evidence_index: int | None = None) -> None:
    if status not in STATUSES:
        raise StateError(f"unknown status {status!r}")
    e["status"], e["status_at"], e["reason"] = status, now_iso(), reason
    e["rev"] = e.get("rev", 0) + 1
    if action:
        e.setdefault("decisions", []).append({"at": now_iso(), "by": by, "action": action,
                                              "reason": decision_reason, "evidence_index": evidence_index})


def next_position(st: dict) -> int:
    return max([e["position"] for e in st["entries"]] + [0]) + 1


def failures(st: dict, name: str, ident: str | None = None) -> list[dict]:
    """Every failure evidence record of the change (I13): under every
    identity ever linked to ``name`` (through ``aliases``), plus ``ident``."""
    idents = {k for k in st["failure_history"]
              if any(st["aliases"].get(p) == name for p in k.split("+"))}
    idents |= {e["identity"] for e in st["entries"] if e["name"] == name and e.get("identity")}
    if ident:
        idents.add(ident)
    out = []
    for k in sorted(idents):
        out.extend(st["failure_history"].get(k, []))
    return out


def alias_conflicts(st: dict, name: str, pr_keys: list[str]) -> list[str]:
    """PR keys already associated with another change (I13: enqueue refuses
    an alias)."""
    return [f"{k} belongs to {st['aliases'][k]}" for k in pr_keys if st["aliases"].get(k) not in (None, name)]


def link_aliases(st: dict, name: str, pr_keys: list[str]) -> None:
    for k in pr_keys:
        st["aliases"].setdefault(k, name)


def add_failure(st: dict, e: dict, evidence: dict) -> int:
    """Append failure evidence (R9.4, I13). Each record names the approval
    record it failed under and a stable reference, ``<identity>#<index>``
    (the list is append-only), for reports and decisions (Sol minor)."""
    ident = e["identity"]
    lst = st["failure_history"].setdefault(ident, [])
    lst.append({**evidence, "name": e["name"], "record_sha256": e.get("record_sha256"),
                "ref": f"{ident}#{len(lst)}"})
    return len(lst) - 1


def add_report(st: dict, rid: str) -> None:
    """Idempotent (I21 m9): a report is listed once."""
    if rid not in st["reports_pending"]:
        st["reports_pending"].append(rid)


def set_pause(st: dict, reason: str, step: str | None = None, advice: str | None = None) -> None:
    """Set the pause and queue its report in the same transaction (Sol SM8:
    no pause is ever written without one)."""
    import time
    st["pause"] = {"at": now_iso(), "step": step, "reason": reason, "message": reason, "advice": advice}
    add_report(st, f"pause-{int(time.time())}")


def set_halt(st: dict, reason: str, step: str | None = None, advice: str | None = None) -> None:
    st["halt"] = {"at": now_iso(), "step": step, "reason": reason, "message": reason, "advice": advice}


def waiting_on_zak(st: dict, now_ts: float | None = None) -> list[str]:
    """The waiting-on-Zak list (R15.1): HALT and pause, waiting-zak
    entries, failed entries older than 24 h, a re-baseline owed."""
    import datetime as dt
    out = []
    if st.get("halt"):
        out.append(f"HALT: {st['halt'].get('reason')}" + (f" ({st['halt']['advice']})" if st['halt'].get('advice')
                                                          else ""))
    if st.get("pause"):
        out.append(f"paused: {st['pause'].get('reason')}")
    if st.get("rebaseline"):
        out.append(f"re-baseline owed after the settle of {st['rebaseline'].get('settle')}")
    now = dt.datetime.now(dt.timezone.utc) if now_ts is None else dt.datetime.fromtimestamp(now_ts, dt.timezone.utc)
    for e in st["entries"]:
        if e["status"] == "waiting-zak":
            out.append(f"{e['name']}: waiting on Zak: {e.get('reason')}")
        elif e["status"] == "failed":
            try:
                age = now - dt.datetime.fromisoformat(e["status_at"])
            except (KeyError, ValueError, TypeError):
                age = dt.timedelta(days=1)
            if age >= dt.timedelta(hours=24):
                out.append(f"{e['name']}: failed {age.days}d ago and not decided (requeue or tried)")
    return out
