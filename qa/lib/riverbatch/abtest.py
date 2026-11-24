"""Runner qualification part 1: A->B->A builds (R14.2, S-M4, I8).

Each B is local, never-pushed commits on ``batch/abtest-<n>`` (made with a
temporary index, ``write-tree`` and ``commit-tree``), built with the runner's
own checkout, TC, image check, intermediate clearing, build and restore,
then staged (``phone-qa stage <qid>-ab-<label> --kind calibration``) so the
kernel is read from the staged ``boot.img``. After each build:

- every file under ``product/system`` and ``product/vendor`` is hashed, with
  the ``build.prop`` lines of ``abtest_masks.json`` removed and the files of
  ``abtest_allowlist.json`` skipped;
- **I8, Sol B1:** ``system/product/etc/build-manifest.xml`` names each
  project's HEAD. Its producer (vendor/lineage
  ``build/tasks/build-manifest_xml.mk``) is ``repo manifest -r`` piped
  through ``grep -Ev "proprietary_$(MANIFEST_EXCLUDES)"``, so the
  ``proprietary_*`` projects (the two Motorola vendor forks) are never in
  it. Each fork the producer emits must be there at its commit-set
  revision, and only those expected values are blanked before the content
  comparison; a filtered fork must be absent, and its HEAD is checked in
  the trial checkout instead; a missing XML fails the trial;
- the product ``dtbo.img``, every river overlay ``.dtbo`` under
  ``DTBO_OBJ``, and ``stage.boot_info`` of the staged ``boot.img``;
- the kernel extracted from the staged ``boot.img`` (header kernel section,
  gunzipped, appended DTBs cut off) is searched for the B1 strings.

``abtest_config.json`` names every edit; the config and both lists are
``runner_build`` inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import tempfile
import time
from pathlib import Path

from riverqa import children as rchildren, stage as rstage
from riverqa.core import now_iso

from . import compose, outcomes as O, paths, phases, state as S
from .ctx import Abort, Busy, Ctx, children_and_container, commit_phase, head

MANIFEST = compose.MANIFEST
BUILD_MANIFEST = "system/product/etc/build-manifest.xml"
STOP_WAIT_S = 600


def load(name: str) -> dict | list:
    return json.loads((paths.PKG / name).read_text())


# --- pure measurement ---------------------------------------------------------------

def mask_prop(text: str, masks: list[str]) -> str:
    """Drop the build.prop lines whose key fully matches a mask (Opus
    minor: ``ro.build.host`` must not mask ``ro.build.hostname``)."""
    rx = [re.compile(m) for m in masks]
    return "\n".join(ln for ln in text.splitlines()
                     if not any(r.fullmatch(ln.split("=", 1)[0].strip()) for r in rx))


PRODUCER_DROPS = re.compile(r"proprietary_")      # build-manifest_xml.mk's grep -Ev (MANIFEST_EXCLUDES unset)


def producer_excluded(paths_: list[str], manifest: Path | None = None) -> set[str]:
    """The commit-set projects whose default.xml ``<project`` line the
    producer's ``grep -Ev "proprietary_"`` drops from build-manifest.xml."""
    import xml.etree.ElementTree as ET
    root = ET.parse(manifest or paths.MANIFEST_DIR / "default.xml").getroot()
    out = set()
    for pj in root.iter("project"):
        path = pj.get("path") or pj.get("name")
        if path in paths_ and PRODUCER_DROPS.search(f'name="{pj.get("name")}" path="{path}"'):
            out.add(path)
    return out


def normalize_build_manifest(text: str, commit_set: dict[str, str],
                             excluded: set[str] = frozenset()) -> tuple[str, list[str]]:
    """I8, Sol B1: check each emitted fork's recorded revision against the
    build's commit set and blank exactly those expected values. A fork the
    producer filters (``excluded``) must not appear; every other fork must."""
    errs, seen = [], set()

    def fix(m: re.Match) -> str:
        tag = m.group(0)
        pm = re.search(r'\bpath="([^"]+)"', tag) or re.search(r'\bname="([^"]+)"', tag)
        rv = re.search(r'\brevision="([0-9a-f]{40})"', tag)
        path = pm.group(1) if pm else None
        if path in excluded:
            errs.append(f"build-manifest.xml names {path}, which its producer filters out: the producer changed")
            return tag
        if path in commit_set:
            seen.add(path)
            if not rv or rv.group(1) != commit_set[path]:
                errs.append(f"build-manifest.xml: {path} at {rv.group(1)[:12] if rv else '?'}, built from "
                            f"{commit_set[path][:12]}")
                return tag
            return tag.replace(rv.group(0), 'revision="EXPECTED"')
        return tag

    out = re.sub(r"<project\b[^>]*>", fix, text)
    missing = sorted(p for p in commit_set if p != MANIFEST and p not in seen and p not in excluded)
    if missing:
        errs.append("build-manifest.xml lacks " + ", ".join(missing))
    return out, errs


def hash_tree(product: Path, masks: list[str], allow: list[str], commit_set: dict[str, str],
              excluded: set[str] = frozenset()) -> tuple[dict, list]:
    files, errs = {}, []
    for part in ("system", "vendor"):
        root = product / part
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for n in sorted(filenames):
                f = Path(dirpath) / n
                rel = str(f.relative_to(product))
                if rel in allow:
                    continue
                if f.is_symlink():
                    files[rel] = "link:" + os.readlink(f)
                    continue
                data = f.read_bytes()
                if n == "build.prop":
                    data = mask_prop(data.decode(errors="replace"), masks).encode()
                if rel == BUILD_MANIFEST:
                    text, e = normalize_build_manifest(data.decode(errors="replace"), commit_set, excluded)
                    errs += e
                    data = text.encode()
                files[rel] = hashlib.sha256(data).hexdigest()
    if BUILD_MANIFEST not in files and BUILD_MANIFEST not in allow:
        errs.append(f"{BUILD_MANIFEST} is missing: the build's revisions cannot be checked (I8)")
    return files, errs


def boot_kernel(img: Path) -> bytes | None:
    """The kernel of a v0-v2 boot image, gunzipped, appended DTBs cut off."""
    try:
        data = img.read_bytes()
    except OSError:
        return None
    if len(data) < 1632 or data[:8] != b"ANDROID!":
        return None
    ksize, page = struct.unpack_from("<I", data, 8)[0], struct.unpack_from("<I", data, 36)[0]
    if page not in (2048, 4096, 16384) or page + ksize > len(data):
        return None
    image, _tail = rstage.split_kernel(data[page:page + ksize])
    return image


def measure(product: Path, staged: Path, cfg: dict, commit_set: dict[str, str],
            checkout_heads: dict[str, str] | None = None) -> dict:
    """``checkout_heads``: HEAD of each producer-filtered fork in the trial
    checkout, read right after the build (Sol B1)."""
    excluded = producer_excluded(list(commit_set))
    files, errs = hash_tree(product, load("abtest_masks.json")["props"], load("abtest_allowlist.json")["files"],
                            commit_set, excluded)
    for p in sorted(excluded):
        got = (checkout_heads or {}).get(p)
        if got != commit_set[p]:
            errs.append(f"{p} (not in build-manifest.xml): the trial checkout was at {str(got)[:12]}, built from "
                        f"{commit_set[p][:12]}")
    dtbo = product / "dtbo.img"
    ovl_dir = product / "obj" / "DTBO_OBJ" / "arch/arm64/boot/dts/qcom"
    overlays = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(ovl_dir.glob("sdm632-river-*-overlay.dtbo"))} if ovl_dir.is_dir() else {}
    k = boot_kernel(staged / "images" / "boot.img")
    strs = cfg["B1"]["kernel"]
    return {"files": files, "errors": errs,
            "dtbo": hashlib.sha256(dtbo.read_bytes()).hexdigest() if dtbo.exists() else None,
            "overlays": overlays, "boot_info": rstage.boot_info(staged / "images" / "boot.img"),
            "kernel_old": None if k is None else strs["old"].encode() in k,
            "kernel_new": None if k is None else strs["new"].encode() in k}


def compare(m: dict, cfg: dict) -> list[str]:
    """The pass criteria of R14.2 over the measurements A, B1, A1, B2, A2,
    B3, A3, B4 (if it finished) and A4."""
    f = []
    a = m["A"]
    for lab in ("A1", "A2", "A3", "A4"):
        x = m[lab]
        if x["files"] != a["files"]:
            diff = sorted(k for k in set(x["files"]) | set(a["files"]) if x["files"].get(k) != a["files"].get(k))
            f.append(f"{lab} differs from A in {', '.join(diff[:6])}")
        for k in ("dtbo", "overlays", "boot_info"):
            if x[k] != a[k]:
                f.append(f"{lab}: {k} differs from A")
    for lab, x in m.items():
        f += [f"{lab}: {e}" for e in x["errors"]]
    b1 = m["B1"]
    diff = {k for k in set(b1["files"]) | set(a["files"]) if b1["files"].get(k) != a["files"].get(k)}
    want = set(cfg["B1"]["expect_changed"]) | set(cfg["B1"]["expect_new"])
    if diff != want:
        f.append(f"A->B1 changed {sorted(diff)}, expected exactly {sorted(want)}")
    for lab in ("B1", "B4"):
        x = m.get(lab)
        if x is None:
            continue
        if not (x["kernel_new"] is True and x["kernel_old"] is False):
            f.append(f"{lab}'s kernel does not have the new string only")
    for lab in ("A1", "A4"):
        if not (m[lab]["kernel_old"] is True and m[lab]["kernel_new"] is False):
            f.append(f"{lab}'s kernel does not have the old string only")
    if m["B2"]["dtbo"] == a["dtbo"] or m["B2"]["overlays"] == a["overlays"]:
        f.append("B2's dtbo.img or overlays equal A's: the DTS edit does not feed dtbo.img")
    if (m["B3"]["boot_info"] or {}).get("ikconfig_sha256") == (a["boot_info"] or {}).get("ikconfig_sha256"):
        f.append("B3's ikconfig equals A's")
    return f


# --- orchestration -------------------------------------------------------------------

def edit_commits(ctx: Ctx, spec: dict, base: dict[str, str], label: str) -> dict[str, str]:
    """Commits on top of base with the spec's edits; never pushed."""
    by_repo: dict[str, list] = {}
    forks = sorted(compose.forks(), key=len, reverse=True)
    for kind in ("edits", "new_files", "appends"):
        for e in spec.get(kind) or []:
            repo = next(r for r in forks if e["path"].startswith(r + "/"))
            by_repo.setdefault(repo, []).append((kind, e["path"][len(repo) + 1:], e))
    out = {}
    for repo, items in by_repo.items():
        with tempfile.TemporaryDirectory() as t:
            env = {"GIT_INDEX_FILE": str(Path(t) / "index")} | compose.GIT_ID
            ctx.git.out(repo, "read-tree", base[repo], env=env)
            for kind, rel, e in items:
                if kind == "new_files":
                    data = e["content"].encode()
                else:
                    data = ctx.git.raw(repo, "show", f"{base[repo]}:{rel}")
                    if kind == "edits":
                        old, new = e["old"].encode(), e["new"].encode()
                        if data.count(old) != 1 or len(old) != len(new):
                            raise Abort({"class": O.VOID, "reason": f"abtest edit of {e['path']} does not apply"})
                        data = data.replace(old, new)
                    else:
                        data = data + e["text"].encode()
                tmp = Path(t) / "blob"
                tmp.write_bytes(data)
                blob = ctx.git.out(repo, "hash-object", "-w", str(tmp)).strip()
                ctx.git.out(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}", env=env)
            tree = ctx.git.out(repo, "write-tree", env=env).strip()
            out[repo] = ctx.git.out(repo, "commit-tree", tree, "-p", base[repo], "-m", f"abtest {label}",
                                    env=compose.GIT_ID).strip()
    return out


def _run_for(ctx: Ctx, bid: str, base: dict, commits: dict, pin: str) -> dict:
    run = {"id": bid, "purpose": "qualify", "phase": "claimed", "intent": None, "members": [], "borrowed": [],
           "versions": {"image_id": pin}, "base": base, "base_verified": True,
           "composed": {p: {"sha": commits.get(p, base[p]), "tree": None} for p in base if p != MANIFEST},
           "touched": sorted(commits), "content_key": None, "attempt_id": None, "qa": None, "ram": None,
           "merge": {"prs": {}, "merged": {}, "tag": None}, "outcome": None}
    with S.transaction("claim", batch=bid, purpose="abtest") as st:
        st["running"] = run
    return run


def delete_abtest_branches(ctx: Ctx) -> list[str]:
    """Delete every local batch/abtest-* branch; returns the ones git kept
    (a checked-out branch is never deleted)."""
    kept = []
    for p in compose.forks():
        rc, out = ctx.git.try_(p, "for-each-ref", "--format=%(refname:short)", "refs/heads/batch/abtest-*")
        for b in out.split() if rc == 0 else []:
            if ctx.git.try_(p, "branch", "-q", "-D", b)[0] != 0:
                kept.append(f"{p}: {b}")
    return kept


def one_build(ctx: Ctx, qid: str, label: str, base: dict, commits: dict, pin: str, cfg: dict,
              interrupt: dict | None = None) -> dict | None:
    """One trial build. A failure (Abort) ends the run the normal way while
    ``running`` is still set: R11.2 step 2 restores the tree from the lease
    and clears both (Sol M8, Gemini GB3, Opus OB1). Any other interruption
    leaves running and the lease for the next entry point."""
    bid = f"{qid}-ab-{label}"
    for p, c in commits.items():
        ctx.git.out(p, "branch", "-f", f"batch/abtest-{label}", c)
    run = _run_for(ctx, bid, base, {p: c for p, c in commits.items()}, pin)
    try:
        if commits:
            with S.transaction("phase", phase="checking-out") as st:
                st["running"]["phase"] = "checking-out"
                st["lease"] = phases.new_lease(bid, commits, base)
            for p in commits:
                ctx.git.out(p, "checkout", "-q", f"batch/abtest-{label}")
        r = ctx.tc_check(phases.expected_heads(run))
        if not r.ok:
            raise Abort({"class": O.VOID, "reason": "TC: " + r.summary()})
        if interrupt:
            finished = interrupted_build(ctx, run, pin, interrupt)
            if not finished:
                phases.restore_or_void(ctx)
                with S.transaction("abtest-done", batch=bid) as st:
                    st["running"] = None
                return None
        else:
            phases.build(ctx, run)
        cs = phases.expected_heads(run)
        heads = {p: head(ctx, p) for p in producer_excluded(list(cs))}
        phases.stage_run(ctx, run, "calibration", False, None)
        commit_phase("restoring")
        phases.restore_or_void(ctx)
        meas = measure(paths.product_dir(), paths.batch_dir(bid), cfg, cs, heads)
    except Abort as a:
        with S.transaction("decided", batch=bid) as st:
            st["running"]["outcome"] = a.outcome
        from . import recover
        recover.abort(ctx)                     # restore from the lease, then clear running (may Pause)
        raise
    with S.transaction("abtest-done", batch=bid) as st:
        st["running"] = None
    return meas


def interrupted_build(ctx: Ctx, run: dict, pin: str, spec: dict) -> bool:
    """B4: SIGTERM the build's holder once B1's driver object exists, is
    newer than the build's start and holds B1's string; then wait for the
    holder, its child record and the container to be gone. True if the
    build finished first (part 1 is then inconclusive)."""
    commit_phase("building")
    phases.image_check(ctx, pin)
    phases.clear_intermediates()
    obj = paths.product_dir() / "obj" / "KERNEL_OBJ" / spec["object"]
    log = paths.batch_dir(run["id"]) / "runner-logs"
    log.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    proc = ctx.tools.start([str(paths.SCRIPTS / "build"), "--installclean", "--batch", run["id"]], run["id"],
                           log / "build-b4.log", {"RIVER_IMAGE_ID": pin})
    needle = spec["string"].encode()
    stopped = False
    while proc.poll() is None:
        try:
            if obj.exists() and obj.stat().st_mtime > t0 and needle in obj.read_bytes():
                ctx.tools.stop()
                stopped = True
                break
        except OSError:
            pass
        ctx.sleep(1)
    proc.wait()
    for _ in range(STOP_WAIT_S):
        if not rchildren.live_children(paths.home()) and not _container_running(ctx):
            break
        ctx.sleep(1)
    else:
        raise Abort({"class": O.VOID, "reason": f"B4: the build's holder, child or container is still alive after "
                                                 f"{STOP_WAIT_S} s"})
    # Gemini GB3: an exited lineageos-build container would collide with
    # A4's; remove it (never --force), or stop if one still runs.
    try:
        children_and_container(ctx)
    except Busy as e:
        raise Abort({"class": O.VOID, "reason": f"B4: {e.message}"}) from e
    if stopped:
        return False
    if proc.returncode != 0:
        raise Abort({"class": O.VOID, "reason": f"B4's build failed (exit {proc.returncode}) before the driver "
                                               "object appeared"})
    return True


def _container_running(ctx: Ctx) -> bool:
    r = ctx.podman.run("ps", "--filter", "name=^lineageos-build$", "--format", "{{.State}}")
    return any(s.strip().lower() == "running" for s in (r.stdout or "").splitlines())


def run(ctx: Ctx, base: dict, pin: str) -> dict:
    cfg = load("abtest_config.json")
    qid = f"q{S.read()['next_qual']:04d}"
    with S.transaction() as st:
        st["next_qual"] += 1
    delete_abtest_branches(ctx)
    forks_base = {p: base[p] for p in base}
    m = {}
    failures = []
    try:
        m["A"] = one_build(ctx, qid, "A", forks_base, {}, pin, cfg)
        for n, b in ((1, "B1"), (2, "B2"), (3, "B3")):
            m[b] = one_build(ctx, qid, b, forks_base, edit_commits(ctx, cfg[b], forks_base, b), pin, cfg)
            m[f"A{n}"] = one_build(ctx, qid, f"A{n}", forks_base, {}, pin, cfg)
        b4 = edit_commits(ctx, cfg["B1"], forks_base, "B4")
        got = one_build(ctx, qid, "B4", forks_base, b4, pin, cfg, interrupt=cfg["B4"])
        if got is not None:
            failures.append("B4's build finished before it could be interrupted: part 1 is inconclusive")
            m["B4"] = got
        m["A4"] = one_build(ctx, qid, "A4", forks_base, {}, pin, cfg)
        failures += compare(m, cfg)
    except Abort as a:
        failures.append(f"a build failed: {a.outcome.get('reason') or a.outcome.get('checks')}")
    # Not in a finally (Sol M8): an interrupted run leaves its branches for
    # the recovery that restores the tree; a finished or failed one cleans up.
    kept = delete_abtest_branches(ctx)
    if kept:
        failures.append("abtest branches left: " + "; ".join(kept))
    from . import version
    return {"passed": not failures, "failures": failures, "at": now_iso(), "qid": qid,
            "runner_build": version.runner_build(), "image_id": pin,
            "summary": {k: {"dtbo": v["dtbo"], "boot_info": v["boot_info"], "kernel_new": v["kernel_new"]}
                        for k, v in m.items() if v}}
