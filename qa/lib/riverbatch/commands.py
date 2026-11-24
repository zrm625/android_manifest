"""Queue commands: enqueue (R5.5, R5.6, I13, I14), withdraw, requeue and
tried (R9.4), resume, cleanup drop, status. Each is one state
transaction; GitHub is read before it, outside the lock."""

from __future__ import annotations

import json
from pathlib import Path

from riverqa.core import read_json

from . import compose, eligibility, paths, records, state as S
from .ctx import Ctx, Locks
from .github import BRANCH, url
from .state import Refused

RIVER = compose.RIVER


def repo_paths() -> dict[str, str]:
    """owner/name -> path for the 9 forks and the manifest."""
    from riverqa import stage
    return {v: k for k, v in stage.repo_slugs(paths.MANIFEST_DIR / "default.xml").items()}


def check_record_prs(rec: dict) -> list[str]:
    rp = repo_paths()
    errs = []
    for p in rec["prs"]:
        repo, _n = records.split_key(p["key"])
        if repo not in rp:
            errs.append(f"{p['key']}: not one of the 9 forks or the manifest")
        elif rp[repo] != p["path"]:
            errs.append(f"{p['key']}: path {p['path']!r} is not {rp[repo]!r}")
    return errs


def remote_base(ctx: Ctx) -> dict[str, str]:
    """The base from GitHub alone (R5.4: eligibility never fetches)."""
    from riverqa import stage
    out = {}
    for path, s in stage.repo_slugs(paths.MANIFEST_DIR / "default.xml").items():
        sha = ctx.gh.ls_remote(url(s), f"refs/heads/{BRANCH}")
        if not sha:
            raise Refused(f"{s} has no {BRANCH}")
        out[path] = sha
    return out


def slugs_all() -> dict[str, str]:
    from riverqa import stage
    return stage.repo_slugs(paths.MANIFEST_DIR / "default.xml")


def kg_cameras() -> list[int]:
    kg = read_json(paths.known_good_file())
    if not kg or not (kg.get("latest") or {}).get("cameras"):
        raise Refused("no known-good.json with a camera set: run the baseline first")
    return sorted(kg["latest"]["cameras"])


def eligibility_of(ctx: Ctx, rec: dict | None, errs: list[str]) -> eligibility.Outcome:
    try:
        return eligibility.eligible(ctx.gh, rec, errs, remote_base(ctx), slugs_all(), kg_cameras())
    except eligibility.BaseCameraMismatch as e:
        raise Refused(str(e)) from e


def new_entry(st: dict, rec: dict, sha: str) -> dict:
    heads = records.approved_heads(rec)
    return {"name": rec["name"], "branch": rec["branch"], "record_sha256": sha, "kind": rec["kind"],
            "identity": records.identity(rec),
            "prs": [{"key": p["key"], "path": p["path"], "head": heads[p["key"]]} for p in rec["prs"]],
            "position": S.next_position(st),
            "flags": {"alone": bool(rec.get("alone")), "solo": False,
                      "upstream": rec["branch"].startswith("upstream-")},
            "status": None, "status_at": None, "reason": None, "rev": 0, "decisions": [],
            "records_before": [], "batch": None, "merged": None, "followup": None}


def enqueue(ctx: Ctx, record_file: Path) -> str:
    rec, raw = records.load_draft(record_file)
    errs = records.validate(rec, record_file.parent, kinds=("change", "experiment"))
    if errs:
        raise Refused("invalid record: " + "; ".join(errs[:6]))
    errs = check_record_prs(rec)
    if errs:
        raise Refused("; ".join(errs))
    name = rec["name"]
    keys = [p["key"] for p in rec["prs"]]
    st0 = S.read()
    _admissible(st0, rec, keys)
    out = eligibility_of(ctx, rec, [])                            # GitHub first, outside the lock
    if out.kind == eligibility.REFUSED:
        raise Refused(out.reason)
    if out.kind == eligibility.REAPPROVE:
        raise Refused(f"the PR set check fails: {out.reason}")
    sha = records.store_bundle(rec, raw, record_file.parent)      # R5.1: before the transaction
    with S.transaction("enqueue", name=name) as st:
        old = _admissible(st, rec, keys)
        if old is not None:                                       # R5.6: replace
            e = old
            e["records_before"].append(e["record_sha256"])
            heads = records.approved_heads(rec)
            e.update(record_sha256=sha, branch=rec["branch"], kind=rec["kind"], identity=records.identity(rec),
                     prs=[{"key": p["key"], "path": p["path"], "head": heads[p["key"]]} for p in rec["prs"]])
            e["flags"]["solo"] = True
            e["flags"]["alone"] = bool(rec.get("alone"))
            action = "replace"
        else:
            e = new_entry(st, rec, sha)
            st["entries"].append(e)
            action = "enqueue"
        S.link_aliases(st, name, keys)
        status, reason = _admitted(st, e, out)
        S.set_status(e, status, reason, by="agent", action=action, decision_reason=reason)
        return status


def _admissible(st: dict, rec: dict, keys: list[str]) -> dict | None:
    """The entry a replacing enqueue replaces, or None for a new one;
    raises Refused otherwise (R5.5, R5.6, I13)."""
    name = rec["name"]
    bad = S.alias_conflicts(st, name, keys)
    if bad:
        raise Refused("these PRs already belong to another change (I13): " + "; ".join(bad))
    e = S.live_entry(st, name)
    if e is None:
        return None
    if e["status"] == "tried":
        if records.approved_heads(rec) == {p["key"]: p["head"] for p in e["prs"]}:
            raise Refused(f"{name} is tried: a new record must approve different heads (a fix and a new round)")
        return e
    if e["status"] == "needs-reapproval":
        return e
    raise Refused(f"{name} is {e['status']}: only a tried or needs-reapproval entry can be replaced")


def _admitted(st: dict, e: dict, out: eligibility.Outcome) -> tuple[str, str | None]:
    """Eligibility's outcome plus the third-failure rule (R9.4)."""
    if out.kind == eligibility.OK:
        n = len(S.failures(st, e["name"], e["identity"]))
        if n >= 3:
            return "waiting-zak", f"failed {n} times: queueing it needs Zak"
        return "queued", None
    return out.kind, out.reason


def withdraw(name: str) -> None:
    with S.transaction("withdraw", name=name) as st:
        e = S.live_entry(st, name)
        if not e:
            raise Refused(f"no live entry {name}")
        if e["status"] == "in-batch":
            raise Refused(f"{name} is in-batch; it leaves only by finish or abort")
        S.set_status(e, "withdrawn", "withdrawn", by="agent", action="withdraw")


def _reason(text: str) -> str:
    t = (text or "").strip()
    if len(t) < 20:
        raise Refused("--reason needs at least one sentence that names the evidence it relied on")
    return t


def requeue(ctx: Ctx, name: str, reason: str, zak: bool) -> str:
    reason = _reason(reason)
    st0 = S.read()
    e0 = S.live_entry(st0, name)
    if not e0 or e0["status"] not in (("failed", "waiting-zak") if zak else ("failed",)):
        raise Refused(f"{name} is {e0['status'] if e0 else 'absent'}; requeue takes a failed entry"
                      + (" or a waiting-zak one" if zak else " (waiting-zak needs --zak)"))
    rec, errs = records.load_bundle(name, e0["record_sha256"])
    out = eligibility_of(ctx, rec, errs)
    with S.transaction("requeue", name=name, zak=zak) as st:
        e = S.live_entry(st, name)
        if not e or e["rev"] != e0["rev"]:
            raise Refused(f"{name} changed meanwhile; run requeue again")
        idx = e.get("last_evidence_index")
        if out.kind == eligibility.OK:
            n = len(S.failures(st, name, e["identity"]))
            if n >= 3 and not zak:
                status, why = "waiting-zak", f"failed {n} times: queueing it needs Zak"
            else:
                status, why = "queued", None
                e["flags"]["solo"] = True
        else:
            status, why = (eligibility.WAITING if out.kind == eligibility.REFUSED else out.kind), out.reason
        S.set_status(e, status, why, by="zak" if zak else "tester", action="requeue", decision_reason=reason,
                     evidence_index=idx)
        return status


def tried(name: str, reason: str) -> None:
    reason = _reason(reason)
    with S.transaction("tried", name=name) as st:
        e = S.live_entry(st, name)
        if not e or e["status"] != "failed":
            raise Refused(f"{name} is {e['status'] if e else 'absent'}; tried takes a failed entry")
        S.set_status(e, "tried", reason, by="tester", action="tried", decision_reason=reason,
                     evidence_index=e.get("last_evidence_index"))


def resume() -> dict:
    """Clear a HALT or a pause; reset what R9.1, R9.5 and R11.6 say resume
    resets."""
    with S.transaction("resume") as st:
        was = {"halt": st.get("halt"), "pause": st.get("pause")}
        if not was["halt"] and not was["pause"]:
            raise Refused("nothing to resume: no HALT and no pause")
        step = (st.get("pause") or {}).get("step")
        st["halt"] = st["pause"] = None
        st["voids_in_a_row"] = 0
        st["last_fail"] = None
        st["strikes"]["transport_prs"] = 0
        if step in st["strikes"]:
            st["strikes"][step] = 0
        return was


def cleanup_drop(ctx: Ctx, bid: str) -> None:
    with Locks(ctx):
        with S.transaction("cleanup-drop", batch=bid) as st:
            if not any(c["batch"] == bid for c in st["cleanup"]):
                raise Refused(f"no cleanup item for {bid}")
            st["cleanup"] = [c for c in st["cleanup"] if c["batch"] != bid]


def status(as_json: bool) -> str:
    from riverqa import runnerstate
    st = runnerstate.read_shared(paths.home()) or S.empty()
    waiting = S.waiting_on_zak(st)
    if as_json:
        return json.dumps({"waiting_on_zak": waiting, "halt": st.get("halt"), "pause": st.get("pause"),
                           "running": st.get("running"), "lease": st.get("lease"), "cleanup": st.get("cleanup"),
                           "entries": [{k: e.get(k) for k in ("name", "status", "position", "flags", "reason",
                                                              "batch", "status_at")} for e in st.get("entries", [])]},
                          indent=2)
    lines = ["Waiting on Zak:"] + ([f"  - {w}" for w in waiting] or ["  (nothing)"])
    run = st.get("running")
    lines.append(f"Running: {run['id']} ({run['purpose']}, phase {run['phase']})" if run else "Running: none")
    if st.get("lease"):
        lines.append(f"Tree lease: {st['lease']['batch']} (do not sync, check out or merge into river-22.2)")
    lines.append("Queue:")
    for e in sorted(st.get("entries", []), key=lambda x: x["position"]):
        if e["status"] in ("merged", "withdrawn"):
            continue
        flags = ",".join(k for k, v in (e.get("flags") or {}).items() if v)
        lines.append(f"  {e['position']:>3} {e['name']:<28} {e['status']:<17} {flags:<12} {e.get('reason') or ''}")
    return "\n".join(lines)


def migrate(ctx: Ctx, record_files: list[Path]) -> dict:
    """R5.7: queue/approved.jsonl becomes records and entries through
    exactly R5.4 and R5.5, in one transaction with ``migrated``; any failure
    writes nothing. Each approved change needs a council record (with its
    briefs and replies, I14): a reply that cannot be found needs a new
    round (a new full round for the initial one)."""
    import hashlib
    import os
    st0 = S.read()
    src = paths.queue_file()
    if st0.get("migrated"):
        if src.exists():
            os.replace(src, src.with_name(src.name + ".migrated"))
        return {"migrated": st0["migrated"], "renamed": True}
    if not src.exists():
        raise Refused(f"{src} does not exist")
    raw_src = src.read_bytes()
    approved = [json.loads(line) for line in raw_src.decode().splitlines() if line.strip()]
    names = [a["change"] for a in approved if a.get("status", "queued") == "queued"]
    by_name = {}
    for f in record_files:
        rec, raw = records.load_draft(f)
        by_name[rec.get("name")] = (rec, raw, f)
    missing = [n for n in names if n not in by_name]
    if missing:
        raise Refused("no council record for " + ", ".join(missing) + " (a missing reply needs a new round)")
    prepared = []
    for n in names:
        rec, raw, f = by_name[n]
        errs = records.validate(rec, f.parent, kinds=("change", "experiment")) + check_record_prs(rec)
        if errs:
            raise Refused(f"{n}: invalid record: " + "; ".join(errs[:5]))
        out = eligibility_of(ctx, rec, [])
        if out.kind in (eligibility.REFUSED, eligibility.REAPPROVE):
            raise Refused(f"{n}: {out.kind}: {out.reason}")
        prepared.append((rec, raw, f, out))
    shas = [records.store_bundle(rec, raw, f.parent) for rec, raw, f, _ in prepared]
    with S.transaction("migrate", names=names) as st:
        if st.get("migrated") or st["entries"]:
            raise Refused("the state file already has entries; migration runs once, on an empty queue")
        for (rec, _raw, _f, out), sha in zip(prepared, shas):
            e = new_entry(st, rec, sha)
            st["entries"].append(e)
            S.link_aliases(st, rec["name"], [p["key"] for p in rec["prs"]])
            status, reason = _admitted(st, e, out)
            S.set_status(e, status, reason, by="migrate", action="migrate")
        st["migrated"] = {"at": __import__("riverqa.core", fromlist=["now_iso"]).now_iso(),
                          "source_sha256": hashlib.sha256(raw_src).hexdigest()}
        st["setup_wipe_seq"] = None
    os.replace(src, src.with_name(src.name + ".migrated"))
    return {"migrated": names}
