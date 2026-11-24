"""HTML reports (R15.1): `runner/reports/<id>.html` and `index.html`,
self-contained, no secrets. A transaction that ends an event lists the
report in ``reports_pending`` (idempotently, I21 m9); it is written after
the transaction and removed from the list once written, at the start and
end of every tick, halted or paused too. `batch report` regenerates one."""

from __future__ import annotations

import html
import json

from riverqa.core import now_iso, read_json, read_jsonl, write_json

from . import paths, state as S

MEASURED = ("run_id", "host_boot_seconds", "used_ram_kb", "system_used_kb", "vendor_used_kb")


def snapshot(ctx, run: dict, outcome: dict, final: bool) -> None:
    """Sol SM8: ``batches/<id>/outcome.json``, the run's outcome with its
    evidence, QA result, the candidate's measurements and the known-good
    reference it was decided against, written at the decision and again
    when the run ends, so a split, a void or a finish never loses it once
    ``running`` is cleared. Reports render it."""
    from riverqa import bench as rbench
    bid = run.get("id")
    try:
        d = paths.batch_dir(bid)
    except ValueError:
        return
    b = read_json(d / "batch.json", {}) or {}
    try:
        rows = rbench.attempt_rows(b) if b.get("batch") else []
    except (OSError, KeyError, ValueError):
        rows = []
    old = read_json(d / "outcome.json", {}) or {}
    kg = read_json(paths.known_good_file(), {}) or {}
    latest = kg.get("latest") or {}
    snap = {"batch": bid, "purpose": run.get("purpose"), "at": now_iso(), "final": final,
            "outcome": outcome, "members": run.get("members") or [], "borrowed": run.get("borrowed") or [],
            "attempt_id": run.get("attempt_id"), "content_key": run.get("content_key"),
            "base_verified": run.get("base_verified"), "qa": run.get("qa"), "ram": run.get("ram"),
            "measurements": [{k: r.get(k) for k in MEASURED} for r in rows],
            # the reference as it stood at the decision; the final write keeps it
            "reference": old.get("reference") or {
                "ram_reference": kg.get("ram_reference"),
                "known_good": {k: latest.get(k) for k in ("batch", "tag", "host_boot_seconds", "system_used_kb",
                                                          "vendor_used_kb")}}}
    try:
        d.mkdir(parents=True, exist_ok=True)
        write_json(d / "outcome.json", snap)
    except OSError:
        pass


def _outcome_section(rid: str) -> list[str]:
    try:
        snap = read_json(paths.batch_dir(rid) / "outcome.json")
    except (ValueError, OSError):
        snap = None
    if not snap:
        return []
    o = snap.get("outcome") or {}
    ev = o.get("evidence") or {}
    rows = [("class", e(o.get("class"))), ("reason", e(o.get("reason"))),
            ("failing checks", e(", ".join(o.get("checks") or []))), ("rescued", e(o.get("rescued"))),
            ("step", e(ev.get("step"))), ("QA run", e((snap.get("qa") or {}).get("run_id"))),
            ("QA exit / eligible", e(f"{(snap.get('qa') or {}).get('exit')} / {(snap.get('qa') or {}).get('eligible')}")),
            ("flash attempt", e(snap.get("attempt_id"))), ("content key", e(snap.get("content_key"))),
            ("members", e(", ".join(m.get("name", "?") for m in snap.get("members") or []))),
            ("final", e(snap.get("final")))]
    if ev.get("report"):
        rows.append(("phone-qa report", f"<a href='file://{e(ev['report'])}'>{e(ev['report'])}</a>"))
    for k in ("readback", "fingerprints", "why", "exit", "not_eligible_because"):
        if ev.get(k):
            rows.append((k, f"<code>{e(json.dumps(ev[k])[:600])}</code>"))
    out = ["<h2>Outcome and evidence</h2><table>" + _rows(rows) + "</table>"]
    ms = snap.get("measurements") or []
    if ms:
        out.append("<h2>Candidate measurements</h2><table><tr>" + "".join(f"<th>{e(k)}</th>" for k in MEASURED)
                   + "</tr>" + _rows([[e(r.get(k)) for k in MEASURED] for r in ms]) + "</table>")
    ref = snap.get("reference") or {}
    kgr = ref.get("known_good") or {}
    out.append("<h2>Reference at the decision</h2><table>" + _rows([
        ("known-good batch", e(kgr.get("batch"))), ("tag", e(kgr.get("tag"))),
        ("boot s", e(kgr.get("host_boot_seconds"))),
        ("RAM reference MB", e((ref.get("ram_reference") or {}).get("median_mb"))),
        ("/system KB", e(kgr.get("system_used_kb"))), ("/vendor KB", e(kgr.get("vendor_used_kb")))]) + "</table>")
    return out

CSS = ("body{font:14px system-ui,sans-serif;margin:16px;max-width:980px;color:#1b1b1b;background:#fff}"
       "h1{font-size:20px}h2{font-size:16px;margin-top:20px}table{border-collapse:collapse;width:100%}"
       "td,th{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left;vertical-align:top}"
       ".zak{background:#fff4e5;border:1px solid #f0a020;padding:8px}code{font-size:12px}"
       "@media (prefers-color-scheme: dark){body{background:#151515;color:#e6e6e6}.zak{background:#3a2a10}"
       "td,th{border-color:#333}}")


def e(x) -> str:
    return html.escape(str(x) if x is not None else "")


def _rows(items) -> str:
    return "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in items)


def render(rid: str, st: dict) -> str:
    kg = read_json(paths.known_good_file(), {}) or {}
    ss = read_json(paths.slot_state_file(), {}) or {}
    waiting = S.waiting_on_zak(st)
    parts = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>River batch {e(rid)}</title><style>{CSS}</style><h1>River batch report: {e(rid)}</h1>",
             f"<p>Written {e(now_iso())}.</p>", "<div class=zak><h2>Waiting on Zak</h2>"]
    parts.append("<ul>" + "".join(f"<li>{e(w)}</li>" for w in waiting) + "</ul>" if waiting else "<p>Nothing.</p>")
    b = st.get("blocker")
    if b:
        parts.append(f"<p><b>No batch runs: {e(b.get('kind'))}</b> since {e(b.get('at'))}: {e(b.get('message'))}</p>")
    for c in st.get("cleanup") or []:
        parts.append(f"<p>Cleanup owed for {e(c['batch'])}: branches {e(sorted(c.get('branch_repos') or {}))}, "
                     f"PRs {e(c.get('pr_repos'))}</p>")
    parts.append("</div>")
    settle = (st.get("settles") or {}).get(rid.removeprefix("settle-"))
    if rid.startswith("settle-") and settle:
        parts.append("<h2>Settle</h2><table>" + _rows([
            ("promoted", e(settle["promoted"])), ("known-good.json written", e(settle["known_good_written"])),
            ("changes landed", e(settle["landed"])), ("tags present in", e(settle["tags"])),
            ("re-baseline owed", e(settle["rebaseline"])), ("drifted forks", e(settle.get("drifted"))),
            ("change PRs left open", e(", ".join(settle.get("change_prs_open") or []) or "none")),
            ("advice", e(settle.get("advice")))]) + "</table>")
    events = [x for x in read_jsonl(paths.events_file()) if x.get("batch") == rid][-40:]
    members = [x for x in st["entries"] if x.get("batch") == rid
               or any(d.get("reason") and rid in str(d.get("reason")) for d in x.get("decisions") or [])
               or rid in str(x.get("reason") or "")]
    outcome = next((x for x in reversed(events) if x.get("event") in ("decided", "finish", "abort", "settle")), None)
    parts.append(f"<h2>Outcome</h2><p>{e(outcome.get('event') + ' ' + str(outcome.get('cls') or '') if outcome else 'see events')}</p>")
    parts.extend(_outcome_section(rid))
    for x in members:
        if x.get("experiment_result"):                    # Sol SM8: the experiment's result, for Zak
            parts.append(f"<h2>Experiment result: {e(x['name'])}</h2><p><code>"
                         f"{e(json.dumps(x['experiment_result'])[:1200])}</code></p>")
    if members:
        parts.append("<h2>Changes</h2><table><tr><th>change</th><th>status</th><th>PRs</th><th>reason</th></tr>")
        parts.append(_rows([(e(x["name"]), e(x["status"]),
                             "<br>".join(f"<a href='https://github.com/{e(p['key'].split('#')[0])}/pull/"
                                         f"{e(p['key'].split('#')[1])}'>{e(p['key'])}</a> <code>{e(p['head'][:12])}</code>"
                                         for p in x["prs"]), e(x.get("reason"))) for x in members]) + "</table>")
    fails = [(k, ev) for k, lst in st.get("failure_history", {}).items() for ev in lst if ev.get("batch") == rid]
    if fails:
        parts.append("<h2>Failure evidence</h2><table>")
        for k, ev in fails:
            rep = ev.get("report")
            parts.append(_rows([(e(ev.get("name")), e(", ".join(ev.get("checks") or [])),
                                 f"<a href='file://{e(rep)}'>phone-qa report</a>" if rep else "",
                                 e(json.dumps(ev.get("fingerprints"))[:300] if ev.get("fingerprints") else ""))]))
        parts.append("</table>")
    lf = st.get("last_fail")
    if lf:
        parts.append(f"<p>Previous failing batch: {e(lf.get('batch'))}: {e(', '.join(lf.get('checks') or []))}</p>")
    latest = kg.get("latest") or {}
    parts.append("<h2>Known-good</h2><table>" + _rows([
        ("batch", e(latest.get("batch"))), ("tag", e(latest.get("tag"))),
        ("boot s", e(latest.get("host_boot_seconds"))), ("RAM reference MB", e((kg.get("ram_reference") or {}).get("median_mb"))),
        ("/system KB", e(latest.get("system_used_kb"))), ("/vendor KB", e(latest.get("vendor_used_kb"))),
        ("known-good slot", e(ss.get("known_good_slot"))), ("pair", e((ss.get("pair") or {}).get("batch")))]) + "</table>")
    parts.append("<h2>Queue</h2><table><tr><th>#</th><th>change</th><th>status</th><th>flags</th></tr>")
    parts.append(_rows([(e(x["position"]), e(x["name"]), e(x["status"]),
                         e(",".join(k for k, v in (x.get("flags") or {}).items() if v)))
                        for x in sorted(st["entries"], key=lambda y: y["position"])
                        if x["status"] not in ("merged", "withdrawn")]) + "</table>")
    if events:
        parts.append("<h2>Events</h2><table>" + _rows([(e(x.get("at")), e(x.get("event")),
                                                         e({k: v for k, v in x.items() if k not in ("at", "event")}))
                                                        for x in events]) + "</table>")
    return "".join(parts)


def write(rid: str, st: dict | None = None) -> None:
    st = st or S.read()
    d = paths.reports_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.html").write_text(render(rid, st))
    names = sorted((p.stem for p in d.glob("*.html") if p.stem != "index"), reverse=True)
    (d / "index.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>River batch reports</title><style>{CSS}</style>"
        "<h1>River batch reports</h1><ul>" + "".join(f"<li><a href='{e(n)}.html'>{e(n)}</a></li>" for n in names)
        + "</ul>")


def flush(ctx=None) -> list[str]:
    """Write every pending report, then drop it from the list."""
    try:
        st = S.read()
    except S.StateError:
        return []
    done = []
    for rid in list(st.get("reports_pending") or []):
        try:
            write(rid, st)
        except OSError:
            continue
        done.append(rid)
    if done:
        with S.transaction() as st2:
            st2["reports_pending"] = [r for r in st2["reports_pending"] if r not in done]
    return done
