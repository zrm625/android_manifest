"""One run: member checks, compose and claim, then phases 2-9 and 11 of
R8.3 (also used by qualification and `batch stage`). Each phase is committed
before its step (R11.1). A failure before merge_started raises ``Abort``
with its outcome class; the caller ends the run through R11.2 step 2."""

from __future__ import annotations

import json
import re
import shutil

from riverqa import bench as rbench, regression, stage
from riverqa.core import now_iso, read_json

from . import compose, eligibility, kb, outcomes as O, paths, records, state as S
from .ctx import (Abort, Ctx, Halt, Pause, bench_result, branch, clean, commit_phase, head, image_id, river_build, update_running)

MANIFEST = compose.MANIFEST
INTERMEDIATES = ("KERNEL_OBJ", "DTBO_OBJ", "DTB_OBJ", "RECOVERY_KERNEL_OBJ")   # R7.5 (E1), from kernel.mk


# --- base and known-good ---------------------------------------------------------------

def remote_base(ctx: Ctx, fork_paths: list[str]) -> dict[str, str]:
    """The base (section 1): zrm625/river-22.2 of every fork and
    origin/river-22.2 of the manifest, after a fetch."""
    from .ctx import remote_git
    base = {}
    for p in fork_paths + [MANIFEST]:
        r = "origin" if p == MANIFEST else "zrm625"
        remote_git(ctx, p, "fetch", "-q", r, "river-22.2")
        base[p] = ctx.git.out(p, "rev-parse", f"{r}/river-22.2").strip()
    return base


def kg_cameras(kg: dict) -> list[int]:
    return sorted(((kg or {}).get("latest") or {}).get("cameras") or [])


def base_verified(ctx: Ctx, base: dict[str, str], kg: dict) -> tuple[bool, list[str]]:
    """Verified base (section 1): every fork's river-22.2 tree equals the
    tree of known-good's commit for it, and the manifest XML equals the
    baseline XML."""
    cs = ((kg or {}).get("latest") or {}).get("commit_set") or {}
    why = []
    for p, sha in base.items():
        if p == MANIFEST:
            continue
        want = cs.get(p)
        if not want:
            why.append(f"{p}: not in known-good's commit set")
            continue
        rc, out = ctx.git.try_(p, "rev-parse", f"{want}^{{tree}}")
        if rc != 0:
            from .ctx import remote_git
            remote_git(ctx, p, "fetch", "-q", "zrm625", want)
            out = ctx.git.out(p, "rev-parse", f"{want}^{{tree}}")
        if ctx.git.out(p, "rev-parse", f"{sha}^{{tree}}").strip() != out.strip():
            why.append(f"{p}: river-22.2 tree differs from known-good's")
    if compose.manifest_xml(base[MANIFEST], ctx.git) != baseline_xml(ctx, kg):
        why.append("manifest XML differs from the baseline XML")
    return (not why), why


def baseline_xml(ctx: Ctx, kg: dict) -> dict[str, bytes]:
    rev = (((kg or {}).get("latest") or {}).get("commit_set") or {}).get(MANIFEST)
    if not rev:
        raise Halt("known-good.json names no manifest commit")
    rc, _ = ctx.git.try_(MANIFEST, "cat-file", "-e", f"{rev}^{{commit}}")
    if rc != 0:
        from .ctx import remote_git
        remote_git(ctx, MANIFEST, "fetch", "-q", "origin", rev)
    return compose.manifest_xml(rev, ctx.git)


# --- member checks and claim (R7.2, R7.3) -----------------------------------------------

def entry_record(e: dict) -> tuple[dict | None, list[str]]:
    return records.load_bundle(e["name"], e["record_sha256"])


def member_checks(ctx: Ctx, members: list[dict], base: dict[str, str], slugs: dict[str, str], kg: dict,
                  write: bool = True) -> list[tuple[dict, str, str]]:
    """R7.2: eligibility and the fetched PR heads of every member. Returns
    the confirmed failures [(entry, new status, reason)]; with ``write`` each
    is committed in its own transaction. Raises BaseCameraMismatch and
    Unknown."""
    bad = []
    for e in members:
        rec, errs = entry_record(e)
        out = eligibility.eligible(ctx.gh, rec, errs, base, slugs, kg_cameras(kg))
        if out.kind != eligibility.OK:
            bad.append((e, out.kind if out.kind != eligibility.REFUSED else eligibility.WAITING, out.reason))
            continue
        for pr in e["prs"]:
            n = records.split_key(pr["key"])[1]
            ref = f"refs/river-batches/pr/{pr['path'].replace('/', '_')}/{n}"
            from .ctx import remote_git
            remote_git(ctx, pr["path"], "fetch", "-q", "zrm625", f"+refs/pull/{n}/head:{ref}")
            got = ctx.git.out(pr["path"], "rev-parse", ref).strip()
            if got != pr["head"]:
                bad.append((e, eligibility.REAPPROVE, f"{pr['key']} head is {got[:12]}, approved {pr['head'][:12]}"))
                break
    if write:
        for e, status, why in bad:
            with S.transaction("member-check", name=e["name"], status=status) as st:
                x = S.entry(st, e["name"])
                if x and x["status"] == "queued" and x["rev"] == e["rev"]:
                    S.set_status(x, status, why)
    return bad


def new_running(bid: str, purpose: str, versions: dict, base: dict, verified: bool, comp: compose.Composed,
                ckey: str, members: list[dict], borrowed: list[str]) -> dict:
    return {"id": bid, "purpose": purpose, "phase": "claimed", "intent": None, "started_at": now_iso(),
            "members": [{"name": e["name"], "rev": e["rev"] + (1 if purpose in ("batch", "experiment") else 0)}
                        for e in members] if purpose in ("batch", "experiment") else [],
            "borrowed": borrowed, "versions": versions, "base": base, "base_verified": verified,
            "composed": {p: {"sha": base[p] if p not in comp.commits else comp.commits[p], "tree": t}
                         for p, t in comp.trees.items()},
            "touched": sorted(comp.commits), "content_key": ckey, "attempt_id": None, "qa": None, "ram": None,
            "merge": {"prs": {}, "merged": {}, "tag": None}, "outcome": None, "reached_prs_open": False}


def claim(running: dict, members: list[dict], next_field: str) -> bool:
    """The claim transaction (R7.3): every member still queued at its
    checked rev; record the run; advance the ID; members in-batch. False:
    nothing written, select again."""
    with S.transaction("claim", batch=running["id"]) as st:
        if st["running"] or st["lease"]:
            return False
        xs = []
        for m in members:
            x = S.entry(st, m["name"])
            if not x or x["status"] != "queued" or x["rev"] != m["rev"]:
                return False
            xs.append(x)
        for x in xs:
            x["batch"] = running["id"]
            S.set_status(x, "in-batch", f"claimed by {running['id']}")
        st[next_field] += 1
        st["running"] = running
    return True


# --- phase 2: checkout --------------------------------------------------------------------

def expected_heads(run: dict) -> dict[str, str]:
    return {p: v["sha"] for p, v in run["composed"].items()} | {MANIFEST: run["base"][MANIFEST]}


def base_heads(run: dict) -> dict[str, str]:
    return dict(run["base"])


def new_lease(bid: str, expect: dict[str, str], base: dict[str, str]) -> dict:
    """The tree lease (R7.4, R7.8): ``expect`` the checked-out commit of
    each leased fork, ``base`` its river-22.2 commit, and ``restore_to``
    the HEAD of **every** fork and the manifest once restored (Gemini GB2,
    Opus OB1): the restore can run TC from the lease alone, with or
    without ``running``."""
    return {"batch": bid, "since": now_iso(), "expect": dict(expect), "base": {p: base[p] for p in expect},
            "restore_to": dict(base)}


def checkout(ctx: Ctx, run: dict) -> None:
    lease = new_lease(run["id"], {p: run["composed"][p]["sha"] for p in run["touched"]}, run["base"])
    with S.transaction("phase", phase="checking-out") as st:
        st["running"]["phase"] = "checking-out"
        st["lease"] = lease
    for p in run["touched"]:
        ctx.git.out(p, "checkout", "-q", f"batch/{run['id']}")
        from .ctx import remote_git
        remote_git(ctx, p, "lfs", "pull")                 # a network failure is Unknown, not a runner error
    r = ctx.tc_check(expected_heads(run))
    if not r.ok:
        raise Abort({"class": O.VOID, "reason": f"tree-disturbed after checkout: {r.summary()}"},
                    pause=f"tree-disturbed: {r.summary()}")


# --- phase 3: build (R7.5, R7.6) -------------------------------------------------------------

def clear_intermediates() -> None:
    obj = paths.product_dir() / "obj"
    for d in INTERMEDIATES:
        shutil.rmtree(obj / d, ignore_errors=True)
    left = [d for d in INTERMEDIATES if (obj / d).exists()]
    if left:
        raise Abort({"class": O.VOID, "reason": f"cannot clear {', '.join(left)}"})


def image_check(ctx: Ctx, pin: str) -> None:
    got = image_id(ctx)
    if got != pin:
        raise Abort({"class": O.VOID, "reason": f"build image is {got or 'missing'}, pinned {pin}"},
                    pause="build image changed: requalify, R14.4")


def build(ctx: Ctx, run: dict) -> None:
    commit_phase("building")
    pin = run["versions"]["image_id"]
    logs = []
    for attempt in (1, 2):
        image_check(ctx, pin)
        clear_intermediates()
        name = f"build-{attempt}"
        r = ctx.tool("build", ["--installclean", "--batch", run["id"]], run["id"], name,
                     env={"RIVER_IMAGE_ID": pin})
        log = (paths.batch_dir(run["id"]) / "runner-logs" / f"{name}.log")
        logs.append(log)
        low = ctx.free_gb(paths.home()) < ctx.cfg["free_space_floor_gb"]
        cls = O.build_class(r.rc, bench_result(), low)
        if cls == "ok":
            break
        if cls == "void":
            raise Abort({"class": O.VOID, "reason": f"build infrastructure failure (exit {r.rc}, "
                                                   f".bench-result {bench_result()!r})"})
        if attempt == 2:
            fps = [O.fingerprint(lg.read_text(errors="replace")) if lg.exists() else {} for lg in logs]
            raise Abort({"class": O.FAIL, "checks": ["build"],
                         "evidence": {"step": "build", "logs": [str(x) for x in logs], "fingerprints": fps}})
    info = river_build() or {}
    cs = {p: v["sha"] for p, v in run["composed"].items()} | {MANIFEST: run["base"][MANIFEST]}
    want = f"{run['id']}-{stage.commit_set_hash(cs)}"
    if info.get("image_id") != pin:
        raise Abort({"class": O.VOID, "reason": f"river-build.json image {info.get('image_id')} is not the pin"},
                    pause="build image changed: requalify, R14.4")
    if info.get("build_id") != want or info.get("commit_set") != cs:
        raise Abort({"class": O.VOID, "reason": f"river-build.json names {info.get('build_id')}, want {want}"})


# --- phase 4: stage (R7.7) ---------------------------------------------------------------------

def stage_run(ctx: Ctx, run: dict, kind: str, experiment: bool, kg: dict | None) -> None:
    commit_phase("staging")
    r = ctx.tc_check(expected_heads(run))
    if not r.ok:
        raise Abort({"class": O.VOID, "reason": f"tree-disturbed before stage: {r.summary()}"},
                    pause=f"tree-disturbed: {r.summary()}")
    args = ["stage", run["id"], "--kind", kind] + (["--experiment"] if experiment else [])
    res = ctx.tool("phone-qa", args, run["id"], "stage")
    if res.rc != 0:
        raise Abort({"class": O.VOID, "reason": f"phone-qa stage exited {res.rc}"})
    b = ctx.batch_json(run["id"]) or {}
    if kg is not None and sorted((b.get("cameras") or {}).get("set") or []) != kg_cameras(kg):
        raise Abort({"class": O.VOID, "reason": "staged camera set differs from known-good"},
                    pause="camera set differs from known-good; needs Zak")


# --- phase 5: restore (R7.8) -------------------------------------------------------------------

def restore(ctx: Ctx) -> tuple[bool, list[str]]:
    """Restore the tree from ``lease`` alone; clear it only after every fork
    is restored or accepted and TC passes against the lease's
    ``restore_to`` (every fork and the manifest). Never forces, resets,
    cleans or stashes."""
    st = S.read()
    lease = st.get("lease")
    if not lease:
        return True, []
    found = []
    if not isinstance(lease.get("restore_to"), dict) or MANIFEST not in lease["restore_to"]:
        return False, [f"the lease of {lease.get('batch')} has no restore_to: Zak checks the tree by hand"]
    for p, want in sorted(lease["expect"].items()):
        try:
            h, b, c = head(ctx, p), branch(ctx, p), clean(ctx, p)
        except Exception as e:  # noqa: BLE001 - reported, never forced
            found.append(f"{p}: unreadable ({e})")
            continue
        if h == want and c:
            rc, out = ctx.git.try_(p, "checkout", "-q", "river-22.2")
            if rc != 0:
                found.append(f"{p}: checkout river-22.2 failed: {out.strip()[-120:]}")
            continue
        if b == "river-22.2" and h == lease["base"][p] and c:
            continue
        found.append(f"{p}: {'dirty' if not c else ''} {('on ' + b) if b else 'detached'} at {h[:12]}".strip())
    if found:
        return False, found
    r = ctx.tc_check(dict(lease["restore_to"]))
    if not r.ok:
        return False, r.blocking
    with S.transaction("lease-cleared", batch=lease["batch"]) as st2:
        st2["lease"] = None
    return True, []


def restore_or_void(ctx: Ctx) -> None:
    ok, found = restore(ctx)
    if not ok:
        raise Abort({"class": O.VOID, "reason": "tree-disturbed: " + "; ".join(found[:6])},
                    pause="tree-disturbed: " + "; ".join(found[:6]))


# --- phase 6: flash (R8.6) -------------------------------------------------------------------------

def flash(ctx: Ctx, run: dict) -> None:
    b = ctx.batch_json(run["id"]) or {}
    if b.get("flash_attempt"):
        raise Halt(f"{run['id']}: batch.json already has a flash attempt; a batch is never flashed twice")
    commit_phase("flashing")
    r = ctx.tool("flash", ["--batch", run["id"], "--wipe"], run["id"], "flash")
    if r.rc == 3:
        raise Halt(f"flash --batch {run['id']} halted (exit 3)")
    verdict, attempt, why = O.adopt_flash(r.rc, ctx.batch_json(run["id"]) or {}, ctx.flash_rec(run["id"]),
                                          ctx.slot_state() or {}, ctx.wipes(), run["id"])
    if verdict == "adopt":
        update_running(attempt_id=attempt)
        return
    if verdict == "rescued":
        update_running(attempt_id=attempt)
        raise Abort({"class": O.FAIL, "checks": ["flash.boot"], "rescued": True,
                     "evidence": {"step": "flash", "attempt_id": attempt, "why": why}})
    if verdict == "void":
        raise Abort({"class": O.VOID, "reason": why})
    raise Halt(f"flash --batch {run['id']} exited {r.rc}: {why}")


# --- phase 7: bench (R8.4) ---------------------------------------------------------------------

def _bench(ctx: Ctx, run: dict, boots: int, name: str) -> None:
    r = ctx.tool("device-bench", ["current", "--boots", str(boots), "--batch", run["id"]], run["id"], name)
    if r.rc == 0:
        return
    if r.rc == 3:
        raise Halt(f"device-bench halted (exit 3) in {run['id']}")
    if r.rc == 4:
        ok, why = O.rescue_evidence(ctx.slot_state() or {}, ctx.wipes(), ctx.flash_rec(run["id"]), run["id"],
                                    run["attempt_id"])
        if not ok:
            raise Halt(f"device-bench rescued {run['id']} but the rescue is not verified: {why}")
        raise Abort({"class": O.FAIL, "checks": ["bench.boot"], "rescued": True,
                     "evidence": {"step": "bench", "attempt_id": run["attempt_id"], "why": why}})
    raise Abort({"class": O.VOID, "reason": f"device-bench exited {r.rc}"})


def bench(ctx: Ctx, run: dict, kg: dict | None) -> None:
    commit_phase("benching")
    _bench(ctx, run, 1, "bench-1")
    b = ctx.batch_json(run["id"]) or {}
    rows = rbench.attempt_rows(b)
    required, _why, _med = regression.ram_decision(rows, kg)
    if required and len(rows) < 3:
        _bench(ctx, run, 3 - len(rows), "bench-2")
        rows = rbench.attempt_rows(ctx.batch_json(run["id"]) or {})
    update_running(ram={"rows": [r.get("run_id") for r in rows], "three": bool(required)})


# --- phase 8 and R13.2 step 7: setup and readback -------------------------------------------------

def readback(ctx: Ctx, dev) -> tuple[kb.Check, list[str]]:
    """R13.2 step 7 readback, non-elevating: wizard flags, Wi-Fi on the
    configured SSID, wlan0 address, features.SETUP_SETTINGS (H18)."""
    from riverqa import features
    want_ssid = _ssid(ctx)
    fixtures = read_json(paths.MANIFEST_DIR / "qa" / "fixtures.json", {}) or {}
    addr = ((fixtures.get("server") or {}).get("phone_wifi_addr"))
    reads = {"setup": "settings get secure user_setup_complete", "prov": "settings get global device_provisioned",
             "wifi": "cmd wifi status", "ip": "ip -o -4 addr show wlan0"}
    reads.update({k: v[0] for k, v in features.SETUP_SETTINGS.items()})
    got, bad = {}, []
    for k, cmd in reads.items():
        c, out = kb.checked_read(dev, cmd)
        if not c.ok:
            return c, []
        got[k] = out.strip()
    if got["setup"] != "1" or got["prov"] != "1":
        bad.append(f"wizard flags user_setup_complete={got['setup']} device_provisioned={got['prov']}")
    if f'Wifi is connected to "{want_ssid}"' not in got["wifi"]:
        bad.append("Wi-Fi is not connected to the configured SSID")
    if not addr or f" {addr}/" not in got["ip"]:
        bad.append(f"wlan0 address is not {addr}")
    for k, (_cmd, want) in features.SETUP_SETTINGS.items():
        if got[k] != want:
            bad.append(f"{k} is {got[k]!r}, want {want!r}")
    return kb.Check(kb.OK), bad


def _ssid(ctx: Ctx) -> str:
    f = ctx.cfg.get("wifi_ssid_file")
    if not f:
        raise Pause("runner/config.json has no wifi_ssid_file")
    from pathlib import Path
    return Path(f).read_text().strip()


def phone_setup(ctx: Ctx, bid: str | None) -> int:
    ssid, psk = ctx.cfg.get("wifi_ssid_file"), ctx.cfg.get("wifi_psk_file")
    if not ssid or not psk:
        raise Pause("runner/config.json has no Wi-Fi SSID and PSK files")
    return ctx.tool("phone-setup", ["--wifi", ssid, psk], bid, "phone-setup").rc


def setup_candidate(ctx: Ctx, run: dict) -> None:
    commit_phase("setting-up")
    rc = phone_setup(ctx, run["id"])
    if rc != 0:
        raise Abort({"class": O.FAIL, "checks": ["setup"], "evidence": {"step": "setup", "exit": rc}})
    c, bad = readback(ctx, ctx.phone())
    if not c.ok:
        if c.kind == kb.HALT:
            raise Halt(c.message)
        raise Abort({"class": O.FAIL, "checks": ["setup"], "evidence": {"step": "setup", "readback": c.message}})
    if bad:
        raise Abort({"class": O.FAIL, "checks": ["setup"], "evidence": {"step": "setup", "readback": bad}})
    seq = ctx.wipe_seq()
    with S.transaction("phase", phase="testing") as st:
        st["running"]["phase"] = "testing"
        st["running"]["intent"] = None
        st["setup_wipe_seq"] = seq


# --- phase 9: QA (R8.5) ---------------------------------------------------------------------------

# The gate-state reasons of results.eligibility, matched whole (Opus minor):
# the harness itself is not ready, so the run is void, never the change's fault.
GATE_REASONS = (re.compile(r"thresholds\.json is not calibrated"),
                re.compile(r"\d+/\d+ clean --calibrate runs for this harness version"),
                re.compile(r"controls not qualified( \(.*\))?"))


def gate_reasons(results: dict) -> list[str]:
    return [r for r in results.get("not_eligible_because") or [] if any(g.fullmatch(r) for g in GATE_REASONS)]


def test(ctx: Ctx, run: dict) -> dict:
    """Run phone-qa and classify: returns the outcome to commit at phase 10."""
    r = ctx.tool("phone-qa", ["--batch", run["id"]], run["id"], "phone-qa")
    try:
        h6 = json.loads(r.last_line())
        if not isinstance(h6, dict):
            raise ValueError
    except ValueError:
        h6 = None
    results = read_json(h6["results"]) if h6 and h6.get("results") else None
    if r.rc == 3 or (results or {}).get("halted"):
        raise Halt(f"phone-qa halted in {run['id']}: {(results or {}).get('halted')}")
    bound, why = O.bind(results, h6, run["id"], run["attempt_id"], run["versions"]["harness"],
                        ctx.slot_state() or {})
    update_running(qa={"run_id": (h6 or {}).get("run_id"), "exit": r.rc, "eligible": (results or {}).get("eligible"),
                       "halted": (results or {}).get("halted"), "results": (h6 or {}).get("results"),
                       "bound": bound})
    if not bound:
        return {"class": O.VOID, "reason": f"QA result {why}"}
    evidence = {"step": "qa", "run_ids": [h6["run_id"]], "results": h6.get("results"),
                "report": str(paths.home() / "qa" / "runs" / h6["run_id"] / "report.html"),
                "attempt_id": run["attempt_id"], "not_eligible_because": results.get("not_eligible_because")}
    if r.rc == 0 and results.get("eligible") is True:
        return {"class": O.PASS}
    gate = gate_reasons(results)
    if r.rc == 0 and gate:
        return {"class": O.VOID, "reason": "gate not qualified: " + "; ".join(gate), "pause": "gate not qualified"}
    if r.rc == 0 and run.get("purpose") == "experiment" and not O.failing_checks(results):
        # An experiment is never merge-eligible (results.eligibility says
        # "experiment batch"); a clean run is its pass, for Zak (R9.2).
        return {"class": O.PASS, "evidence": evidence}
    if r.rc in (0, 1, 2):
        checks = O.failing_checks(results) or ["qa"]
        return {"class": O.FAIL, "checks": checks, "evidence": evidence}
    return {"class": O.VOID, "reason": f"phone-qa exited {r.rc}"}


def fail_evidence(run: dict, outcome: dict, harness: str) -> dict:
    """R9.4 evidence fields."""
    ev = dict(outcome.get("evidence") or {})
    ev.update({"at": now_iso(), "checks": outcome.get("checks") or [], "attempt_id": run.get("attempt_id"),
               "base_verified": run.get("base_verified"), "content_key": run.get("content_key"),
               "harness_version": harness})
    return ev
