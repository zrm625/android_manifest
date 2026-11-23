"""phone-qa: the QA harness for river batches (DESIGN.md)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import traceback
from pathlib import Path

from . import bootsig, evidence, flashing, journal as jmod, reboots, regression, stage
from .bench import read_rows
from .core import (BENCH_HOME, CODENAME, QA_DIR, SCRIPTS_DIR, SERIAL, batch_dir, bench_path, log, new_id,
                   now_iso, read_json, sha256_file, write_json, append_jsonl)
from .device import Device, GuardError
from .lock import DeviceLock, LockError
from .results import GROUPS
from .results import (FAIL, INFO, PASS, QUICK_REQUIRED, REQUIRED, UNVERIFIED, BASELINE_REQUIRED,
                      GateInputs, Result, eligibility, exit_code, fill_missing, low_ram_check, worst)

QA_HOME = bench_path("qa")
GROUPS_IDENTITY = GROUPS["identity"]
FIXTURES_JSON = QA_DIR / "fixtures.json"
THRESHOLDS_JSON = QA_DIR / "thresholds.json"
KNOWN_GOOD = QA_HOME / "known-good.json"
CONTROLS_FILE = QA_HOME / "controls.json"
CONTROL_OVERRIDE = QA_HOME / "control-override.json"
HISTORY = QA_HOME / "history.jsonl"
TELEPHONY_RE = re.compile(r"feature:android\.hardware\.telephony")
BAD_DROPBOX = ("system_server_crash", "system_app_crash", "data_app_crash", "SYSTEM_TOMBSTONE",
               "system_server_native_crash", "system_app_native_crash", "data_app_native_crash")
WATCHED_PROCS = {
    "system_server": "system_server",
    "cameraserver": "cameraserver",
    "audioserver": "audioserver",
    "camera_provider": "android.hardware.camera.provider@2.4-service",
    "audio_hal": "android.hardware.audio.service",
    "fingerprint_hal": "android.hardware.biometrics.fingerprint@2.1-service-ets",
}


# --- harness version and controls ------------------------------------------------

def harness_version() -> str:
    """Content hash of phone-qa, the riverqa sources, the helper sources, the
    thresholds, the allowance A and the known-good seed."""
    h = hashlib.sha256()
    files = [SCRIPTS_DIR / "phone-qa", THRESHOLDS_JSON, FIXTURES_JSON]
    files += sorted((QA_DIR / "lib" / "riverqa").rglob("*.py"))
    files += sorted(p for p in (QA_DIR / "helper").rglob("*") if p.is_file())
    for f in files:
        if f.exists():
            h.update(str(f.relative_to(QA_DIR.parent)).encode())
            h.update(f.read_bytes())
    timing = reboots.load_timing()
    h.update(json.dumps({p: (timing.get("paths") or {}).get(p, {}).get("A") for p in reboots.PATHS},
                        sort_keys=True).encode())
    kg = read_json(KNOWN_GOOD, {}) or {}
    ref = kg.get("ram_reference") or {}
    h.update(json.dumps(ref if ref.get("seed") else None, sort_keys=True).encode())
    return h.hexdigest()[:16]


def controls_status(version: str) -> tuple[bool, str]:
    from .controls import CONTROLS
    data = read_json(CONTROLS_FILE, {}) or {}
    got = (data.get(version) or {}).get("results") or {}
    missing = [c for c in CONTROLS if not (got.get(c) or {}).get("qualified")]
    if missing:
        return False, f"{len(missing)}/{len(CONTROLS)} controls not qualified for harness {version}"
    return True, ""


def control_overrides() -> list[str]:
    found = [k for k in os.environ if k.startswith("RIVERQA_")]
    if CONTROL_OVERRIDE.exists():
        found.append(str(CONTROL_OVERRIDE))
    return found


# --- the run -----------------------------------------------------------------------

class Run:
    def __init__(self, mode: str, batch_id: str | None, only: list[str], skip: list[str]):
        self.mode = mode
        self.batch_id = batch_id
        self.only, self.skip = only, skip
        self.run_id = new_id(f"qa-{mode}")
        self.dir = QA_HOME / "runs" / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dev = Device(SERIAL)
        self.results: dict[str, Result] = {}
        self.batch = stage.load(batch_id) if batch_id else None
        self.thresholds = read_json(THRESHOLDS_JSON, {}) or {}
        self.journal = jmod.Journal(QA_HOME / "journal.json", QA_HOME)
        self.snapshot: dict = {}
        self.started = time.time()
        self.version = harness_version()
        self.flash_attempt = self._flash_attempt()
        self.server = None
        self.helper = None
        self.halted: str | None = None
        self.logcat = None
        self.logcat_file = None
        self.logcat_dead: str | None = None
        self.logcat_restarts: list = []

    def _flash_attempt(self) -> dict | None:
        if not self.batch_id:
            return None
        frec = read_json(batch_dir(self.batch_id) / "flash.json", {}) or {}
        booted = [a for a in frec.get("attempts", []) if a.get("result") == "booted"]
        return booted[-1] if booted else None

    def add(self, r: Result) -> Result:
        self.results[r.id] = r
        mark = {"PASS": "PASS", "FAIL": "FAIL", "UNVERIFIED": "UNVR", "INFO": "INFO"}[r.state]
        print(f"  {mark:4}  {r.id:24} {r.summary}", flush=True)
        return r

    def wanted(self, cid: str) -> bool:
        grp = cid.split(".", 1)[0]
        if self.only and cid not in self.only and grp not in self.only:
            return False
        if cid in self.skip or grp in self.skip:
            return False
        return True

    def guard(self) -> None:
        self.dev.guard()

    def attempt_id(self) -> str | None:
        return (self.flash_attempt or {}).get("attempt_id")


def run_check(run: Run, cid: str, fn, *args, **kw) -> None:
    """Run one check; exceptions become UNVERIFIED (never PASS)."""
    if not run.wanted(cid):
        return
    t0 = time.monotonic()
    try:
        out = fn(run, *args, **kw)
    except GuardError:
        raise
    except Exception as e:  # noqa: BLE001
        (run.dir / "errors.log").open("a").write(f"--- {cid}\n{traceback.format_exc()}\n")
        out = Result(cid, UNVERIFIED, f"check raised {type(e).__name__}: {e}")
    for r in out if isinstance(out, list) else [out]:
        r.seconds = round(time.monotonic() - t0, 1)
        run.add(r)


# --- preflight ---------------------------------------------------------------------

def check_fixtures(run: Run) -> Result:
    spec = read_json(FIXTURES_JSON)
    if not spec:
        return Result("preflight.fixtures", UNVERIFIED, "qa/fixtures.json missing")
    fdir = Path(os.path.expandvars(spec.get("dir", "$BENCH_HOME/qa/fixtures")).replace("$BENCH_HOME", str(BENCH_HOME)))
    bad = []
    for name, f in spec["files"].items():
        p = fdir / f["file"]
        if not p.exists():
            bad.append(f"{f['file']} missing")
        elif sha256_file(p) != f["sha256"]:
            bad.append(f"{f['file']} hash mismatch")
    if bad:
        return Result("preflight.fixtures", UNVERIFIED, "; ".join(bad))
    run.fixtures_dir = fdir
    run.fixtures = spec
    return Result("preflight.fixtures", PASS, f"{len(spec['files'])} fixtures, hashes match")


def check_controls(run: Run) -> Result:
    found = control_overrides()
    if run.mode in ("batch", "baseline") and found:
        return Result("preflight.controls", FAIL, "control override present in a " + run.mode + " run: " +
                      ", ".join(found))
    if run.mode == "control":
        return Result("preflight.controls", PASS, f"control mode ({os.environ.get('RIVERQA_CONTROL')})")
    return Result("preflight.controls", PASS, "no control overrides")


def check_journal(run: Run) -> Result:
    from .restore import recover_journal
    restored, dropped, failed = recover_journal(run)
    if failed:
        return Result("preflight.journal", FAIL, "journal recovery failed: " + "; ".join(failed))
    return Result("preflight.journal", PASS, f"restored {len(restored)}, dropped {len(dropped)} after a wipe")


def proc_info(dev: Device) -> dict:
    """PID and start time (field 22 of /proc/<pid>/stat) of the watched processes."""
    out = {}
    for key, name in WATCHED_PROCS.items():
        pids = dev.sh(f"pidof {name}").split()
        if not pids:
            out[key] = None
            continue
        pid = pids[0]
        stat = dev.sh(f"cat /proc/{pid}/stat")
        try:
            start = stat.rsplit(")", 1)[1].split()[19]
        except IndexError:
            start = None
        out[key] = {"pid": pid, "start": start, "name": name}
    return out


def check_snapshot(run: Run) -> Result:
    dev = run.dev
    st = dev.boot_status()
    if st is None:
        return Result("preflight.snapshot", UNVERIFIED, "cannot read boot status")
    reads = diagnostic_reads(dev)
    bad = [f"{k}: {v[2]}" for k, v in reads.items() if not v[0]]
    if bad:
        return Result("preflight.snapshot", UNVERIFIED, "diagnostics unreadable at preflight: " + "; ".join(bad))
    run.snapshot = {
        "boot_id": st.boot_id, "uptime": st.uptime,
        "procs": proc_info(dev),
        "dropbox": bootsig.dropbox_tags(reads["dropbox"][1]),
        "tombstones": sorted(reads["tombstones"][1].split()),
        "pstore": hashlib.sha256(reads["pstore"][1]).hexdigest(),
        "crash": reads["crash"][1],
        "hal_deaths": hal_deaths(dev),
    }
    write_json(run.dir / "snapshot.json", run.snapshot)
    start_logcat(run)
    missing = [k for k, v in run.snapshot["procs"].items() if v is None]
    return Result("preflight.snapshot", PASS if not missing else UNVERIFIED,
                  f"boot_id {st.boot_id[:8]}, uptime {st.uptime:.0f} s" +
                  (f"; not running: {', '.join(missing)}" if missing else ""),
                  {"procs": run.snapshot["procs"]})


def diagnostic_reads(dev: Device) -> dict[str, tuple[bool, object, str]]:
    """The reads preflight snapshots and the final checks compare: each is
    (ok, content, why). Content is text, except pstore (bytes)."""
    out = {}
    for key, kind, cmd in (("dropbox", "dropbox", "dumpsys dropbox"), ("tombstones", "tombstones", "ls /data/tombstones"),
                           ("crash", "crash", "logcat -b crash -d")):
        ok, text, why = evidence.checked_read(dev, kind, cmd)
        out[key] = (ok, text, why)
    out["pstore"] = evidence.read_pstore(dev, "*")
    return out


# --- the streamed logcat (A3) -------------------------------------------------------
# The stream dies when USB re-enumerates (the USB check). It is restarted right
# after that check from the phone time taken before it, appending to the same
# file. A stream found dead at any other point was not restarted, and the
# final health check is then UNVERIFIED.

def phone_epoch(dev: Device) -> str | None:
    r = dev.read("date +%s.%N")
    t = r.out.strip()
    return t[:t.index(".") + 4] if r.ok and re.match(r"^\d+\.\d+$", t) else None


def start_logcat(run: Run, since: str | None = None) -> None:
    import subprocess
    fresh = since is None
    f = open(run.dir / "logcat.txt", "w" if fresh else "a")
    if not fresh:
        f.write(f"--------- phone-qa: stream restarted from {since}\n")
        f.flush()
    run.logcat_file = f
    run.logcat = subprocess.Popen(["adb", "-s", run.dev.serial, "logcat", "-b", "crash,kernel,main", "-v", "threadtime",
                                   "-T", since or "1"], stdout=f, stderr=subprocess.DEVNULL)


def logcat_alive(run: Run) -> bool:
    p = getattr(run, "logcat", None)
    return p is not None and p.poll() is None


def logcat_check(run: Run, where: str) -> None:
    """Record a stream that died where it wasn't expected to."""
    if getattr(run, "logcat", None) is not None and not logcat_alive(run):
        run.logcat_dead = run.logcat_dead or f"streamed logcat died before {where} and was not restarted"


def logcat_restart(run: Run, since: str | None, where: str) -> None:
    """After the USB check: restart the stream from ``since`` (phone time
    taken before the check). Without a start time the gap can't be covered."""
    if getattr(run, "logcat", None) is None:
        return
    if logcat_alive(run):
        return
    run.logcat_file.close()
    if since is None:
        run.logcat_dead = run.logcat_dead or f"streamed logcat died at {where}; no phone time to restart from"
        return
    start_logcat(run, since)
    run.logcat_restarts.append({"after": where, "since": since})


def stop_logcat(run: Run) -> None:
    """Terminate the stream (every exit path). Safe to call twice."""
    p = getattr(run, "logcat", None)
    if p is not None:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except Exception:  # noqa: BLE001
                p.kill()
        run.logcat = None
    f = getattr(run, "logcat_file", None)
    if f is not None and not f.closed:
        f.close()


def hal_deaths(dev: Device) -> str | None:
    m = re.search(r"HAL deaths since last reboot: (\d+)", dev.sh("dumpsys fingerprint"))
    return m.group(1) if m else None


def check_helper(run: Run) -> Result:
    from .helperapk import ensure_helper
    try:
        apk, key = ensure_helper()
    except Exception as e:  # noqa: BLE001
        return Result("preflight.helper", UNVERIFIED, f"helper build failed: {e}")
    run.helper_apk = apk
    return Result("preflight.helper", PASS, f"helper {apk.name} (cache key {key[:12]})")


def preflight(run: Run, lock_result: Result) -> bool:
    run.add(lock_result)
    try:
        run.guard()
        run.add(Result("preflight.guards", PASS, f"{SERIAL} is {CODENAME} in adb device mode"))
    except GuardError as e:
        run.add(Result("preflight.guards", FAIL, str(e)))
        return False
    run.dev.root()
    run_check(run, "preflight.controls", check_controls)
    if run.mode != "baseline":
        run_check(run, "preflight.fixtures", check_fixtures)
        if run.results.get("preflight.fixtures") and run.results["preflight.fixtures"].state != PASS:
            return False  # a missing fixture: nothing else runs
    run_check(run, "preflight.journal", check_journal)
    if run.mode != "baseline":
        run_check(run, "preflight.helper", check_helper)
        run_check(run, "preflight.snapshot", check_snapshot)
    return True


# --- identity ------------------------------------------------------------------------

def check_build_id(run: Run) -> Result:
    want = run.batch.get("build_id")
    s, v = flashing.partition_build_ids(run.dev)
    d = {"system": s, "vendor": v, "batch": want}
    if not want:
        return Result("identity.build_id", UNVERIFIED, "batch.json has no build ID (unstamped build)", d)
    if s == want and v == want and run.batch.get("vendor_build_id") == want:
        return Result("identity.build_id", PASS, f"system and vendor build.prop say {want}", d)
    return Result("identity.build_id", FAIL, f"system {s}, vendor {v}, batch {want}", d)


def check_boot_hash(run: Run) -> Result:
    img = run.batch["images"]["boot"]
    slot = (run.flash_attempt or {}).get("target") or run.dev.getprop("ro.boot.slot_suffix").lstrip("_")
    try:
        got = flashing.boot_partition_sha(run.dev, slot, int(img["size"]))
    except RuntimeError as e:
        return Result("identity.boot_hash", FAIL, str(e))
    ok = got == img["sha256"]
    return Result("identity.boot_hash", PASS if ok else FAIL,
                  f"first {img['size']} bytes of boot_{slot}: {got[:12]} vs staged {img['sha256'][:12]}")


def check_props(run: Run) -> Result:
    dev = run.dev
    have = {"build": dev.getprop("ro.build.date.utc"), "vendor": dev.getprop("ro.vendor.build.date.utc"),
            "bootimage": dev.getprop("ro.bootimage.build.date.utc"),
            "slot": dev.getprop("ro.boot.slot_suffix").lstrip("_")}
    want = dict(run.batch["dates"])
    if not run.flash_attempt:
        return Result("identity.props", UNVERIFIED, "no successful flash record for this batch", have)
    want["slot"] = run.flash_attempt["target"]
    bad = [f"{k} {have[k]} != {want.get(k)}" for k in have if have[k] != want.get(k)]
    if any(want.get(k) in (None, "") for k in want):
        return Result("identity.props", UNVERIFIED, "batch.json lacks a date", {"have": have, "want": want})
    return Result("identity.props", FAIL if bad else PASS, "; ".join(bad) or
                  f"dates and slot {have['slot']} match batch.json and the flash record", {"have": have})


def check_low_ram(run: Run) -> Result:
    st, msg = low_ram_check(run.dev.getprop("ro.config.low_ram"), (run.batch or {}).get("experiment"))
    return Result("identity.low_ram", st, msg)


def batch_rows(run: Run) -> list[dict]:
    runs = run.batch.get("bench_runs") or []
    return [r for r in read_rows() if r.get("run_id") in runs]


def check_bench_rows(run: Run) -> Result:
    rows = batch_rows(run)
    if not rows:
        return Result("identity.bench_rows", UNVERIFIED, "no device-bench rows for this batch")
    want = run.batch["dates"]
    slot = (run.flash_attempt or {}).get("target")
    bad = []
    for r in rows:
        k = f"{r['run_id']}#{r['boot']}"
        if r.get("batch") != run.batch_id:
            bad.append(f"{k}: batch {r.get('batch')!r}")
        if (r.get("build_date_utc"), r.get("vendor_date_utc"), r.get("bootimage_date_utc")) != \
                (want["build"], want["vendor"], want["bootimage"]):
            bad.append(f"{k}: dates differ")
        if r.get("slot") != slot:
            bad.append(f"{k}: slot {r.get('slot')} != {slot}")
        if run.batch.get("build_id") and r.get("build_id") != run.batch["build_id"]:
            bad.append(f"{k}: build ID {r.get('build_id')}")
        comp = read_json(BENCH_HOME / (r.get("evidence") or "none") / "completion.json")
        if not comp or comp.get("boot_id") != r.get("boot_id"):
            bad.append(f"{k}: boot_id not bound to its completion record")
    if bad:
        return Result("identity.bench_rows", FAIL, "; ".join(bad[:6]), {"bad": bad})
    return Result("identity.bench_rows", PASS, f"{len(rows)} rows match batch, dates, slot and build ID")


def check_image_hashes(run: Run) -> Result:
    if not run.flash_attempt:
        return Result("identity.image_hashes", UNVERIFIED, "no successful flash record")
    rec = run.flash_attempt.get("image_sha256") or {}
    want = {k: v["sha256"] for k, v in run.batch["images"].items()}
    if rec != want or not run.flash_attempt.get("hashes_checked"):
        return Result("identity.image_hashes", FAIL, "flash record hashes differ from batch.json")
    return Result("identity.image_hashes", PASS, "flash checked and wrote exactly the staged images")


NO_BATCH = "no batch (probe run): nothing to compare against"


def identity(run: Run) -> None:
    run.guard()
    if run.batch is None:
        for cid in GROUPS_IDENTITY:
            if run.wanted(cid):
                run.add(Result(cid, UNVERIFIED, NO_BATCH))
        return
    for cid, fn in (("identity.build_id", check_build_id), ("identity.boot_hash", check_boot_hash),
                    ("identity.props", check_props), ("identity.low_ram", check_low_ram),
                    ("identity.bench_rows", check_bench_rows), ("identity.image_hashes", check_image_hashes)):
        run_check(run, cid, fn)


# --- health ----------------------------------------------------------------------------

def check_boot(run: Run) -> Result:
    st = run.dev.boot_status()
    ok = bool(st and st.completed)
    return Result("health.boot", PASS if ok else FAIL,
                  f"boot completed (uptime {st.uptime:.0f} s)" if ok else "sys.boot_completed is not 1")


def check_selinux(run: Run) -> Result:
    mode = run.dev.sh("getenforce").strip()
    return Result("health.selinux", PASS if mode == "Enforcing" else FAIL, mode or "unreadable")


def evidence_dirs(run: Run) -> list[tuple[str, Path, str, bool]]:
    """(label, dir, reboot path, first boot) for the first boot after the wipe
    and every measured boot."""
    out = []
    bdir = batch_dir(run.batch_id)
    if run.flash_attempt and (run.flash_attempt.get("first_boot") or {}).get("evidence"):
        out.append(("first boot", bdir / run.flash_attempt["first_boot"]["evidence"], "fastboot", True))
    else:
        out.append(("first boot", bdir / "boots" / "missing-first-boot", "fastboot", True))
    for r in batch_rows(run):
        out.append((f"bench {r['run_id']}#{r['boot']}", BENCH_HOME / (r.get("evidence") or "missing"), "adb", False))
    return out


def check_boot_evidence(run: Run) -> Result:
    if run.batch is None:
        return Result("health.boot_evidence", UNVERIFIED, NO_BATCH)
    states, notes, det = [], [], {}
    for label, d, _path, first in evidence_dirs(run):
        st, msg, details = evidence.judge(d, first_boot=first)
        # judge() binds collection boot_id to the completion boot_id it was
        # given; for bench boots also check completion.json.
        if not first and st == PASS:
            comp = read_json(d / "completion.json")
            meta = read_json(d / "meta.json") or {}
            if not comp or comp.get("boot_id") != meta.get("boot_id_at_collection"):
                st, msg = UNVERIFIED, "boot_id differs from the one recorded at completion"
        if first and st == PASS:
            meta = read_json(d / "meta.json") or {}
            want = ((run.flash_attempt or {}).get("first_boot") or {}).get("boot_id")
            if meta.get("boot_id_at_collection") != want or meta.get("flash_attempt") != run.attempt_id():
                st, msg = UNVERIFIED, "first-boot evidence not bound to the flash record's boot_id and attempt"
        states.append(st)
        notes.append(f"{label}: {st} {msg}" if st != PASS else f"{label}: clean")
        det[label] = {"state": st, "summary": msg, **details}
    return Result("health.boot_evidence", worst(states), "; ".join(notes), det)


def check_boot_signals(run: Run) -> Result:
    if run.batch is None:
        return Result("health.boot_signals", UNVERIFIED, NO_BATCH)
    timing = reboots.load_timing()
    states, notes = [], []
    for label, d, path, _first in evidence_dirs(run):
        sig = read_json(d / "signals.json")
        if sig is None:
            states.append(UNVERIFIED)
            notes.append(f"{label}: no signals recorded")
            continue
        st, msg = reboots.combined_verdict(sig, path, timing)
        states.append(st)
        notes.append(f"{label}: {st} ({msg})")
    return Result("health.boot_signals", worst(states), " | ".join(notes))


def check_dropbox(run: Run, since: list[str] | None = None) -> Result:
    dev = run.dev
    cid = "health.dropbox" if since is None else "final.dropbox"
    ok, dump, why = evidence.checked_read(dev, "dropbox", "dumpsys dropbox")
    if not ok:
        return Result(cid, UNVERIFIED, f"dumpsys dropbox unreadable: {why}")
    tags = bootsig.dropbox_tags(dump)
    pool = list(tags)
    if since is not None:
        # Entries compared by name and count, never by timestamp.
        for t in since:
            if t in pool:
                pool.remove(t)
    bad = [t for t in pool if t in BAD_DROPBOX or t.endswith("_anr")]
    kmsg = [t for t in pool if t == "SYSTEM_LAST_KMSG"]
    panic = False
    if kmsg:
        r = dev.read("dumpsys dropbox --print SYSTEM_LAST_KMSG", timeout=120)
        if not r.ok:
            return Result(cid, UNVERIFIED, f"cannot print SYSTEM_LAST_KMSG (exit {r.rc})")
        panic = bool(re.search(r"Kernel panic|Internal error: Oops", r.out))
    if bad or panic:
        return Result(cid, FAIL, "dropbox: " + ", ".join(sorted(set(bad)) + (["SYSTEM_LAST_KMSG panic"] if panic else [])),
                      {"tags": tags})
    return Result(cid, PASS, f"{len(pool)} entries{' new' if since is not None else ''}, no crashes, ANRs or panics")


def check_tombstones(run: Run) -> Result:
    rc, out, err = run.dev.sh_rc("ls /data/tombstones")
    if rc != 0:
        return Result("health.tombstones", UNVERIFIED, f"cannot list /data/tombstones: {err.strip()}")
    files = [f for f in out.split() if f.startswith("tombstone")]
    return Result("health.tombstones", FAIL if files else PASS,
                  f"{len(files)} tombstones" if files else "/data/tombstones is empty")


def check_pstore(run: Run) -> Result:
    rc, out, err = run.dev.sh_rc("ls /sys/fs/pstore")
    if rc != 0:
        return Result("health.pstore", UNVERIFIED, "cannot read /sys/fs/pstore")
    ok, data, why = evidence.read_pstore(run.dev, "*")
    if not ok:
        return Result("health.pstore", UNVERIFIED, why)
    files = out.split()
    hit = re.search(rb"Kernel panic|Internal error: Oops|Unable to handle kernel", data)
    if hit or any(f.startswith("dmesg-ramoops") for f in files):
        return Result("health.pstore", FAIL, f"pstore shows a panic or oops ({', '.join(files)})")
    return Result("health.pstore", PASS, f"no panic or oops ({', '.join(files) or 'empty'})")


def check_services(run: Run) -> Result:
    ok, dmesg, why = evidence.checked_read(run.dev, "dmesg", "dmesg")
    if not ok:
        return Result("health.services", UNVERIFIED, f"dmesg unreadable: {why}")
    crashed, loops = evidence.service_problems(dmesg)
    if crashed:
        return Result("health.services", FAIL, "crashed: " + ", ".join(crashed))
    if loops:
        return Result("health.services", FAIL, "restarting: " + ", ".join(loops))
    return Result("health.services", PASS, "no crashes or restart loops in dmesg")


def check_telephony(run: Run) -> Result:
    ok, text, why = evidence.checked_read(run.dev, "features", "pm list features")
    if not ok:
        return Result("health.telephony", UNVERIFIED, f"pm list features unreadable: {why}")
    feats = TELEPHONY_RE.findall(text)
    return Result("health.telephony", FAIL if feats else PASS,
                  f"{len(feats)} telephony features" if feats else "no telephony features (Wi-Fi only)")


def check_regression(run: Run) -> Result:
    if run.batch is None:
        return Result("health.regression", UNVERIFIED, NO_BATCH)
    rows = batch_rows(run)
    if getattr(run, "control_drop_rows_after", None):
        rows = rows[:run.control_drop_rows_after]
    if run.mode in ("calibrate", "baseline"):
        b1 = rows[0] if rows else None
        if len(rows) < 3 or not b1:
            return Result("health.regression", UNVERIFIED, f"record mode needs 3 measured boots, have {len(rows)}")
        rams = [regression.mb(r["used_ram_kb"]) for r in rows[:3]]
        return Result("health.regression", PASS,
                      f"recorded: boot 1 {float(b1['host_boot_seconds']):.1f} s, RAM median "
                      f"{regression.median3(rams):.1f} MB, /system {b1['system_used_kb']} KB, "
                      f"/vendor {b1['vendor_used_kb']} KB, cameras {run.batch['cameras']['set']}",
                      {"rows": [r["run_id"] + "#" + r["boot"] for r in rows]})
    v = regression.evaluate(rows, read_json(KNOWN_GOOD))
    return Result("health.regression", v.state, v.summary, v.details)


def health(run: Run) -> None:
    run.guard()
    for cid, fn in (("health.boot", check_boot), ("health.selinux", check_selinux),
                    ("health.boot_evidence", check_boot_evidence), ("health.boot_signals", check_boot_signals),
                    ("health.dropbox", check_dropbox), ("health.tombstones", check_tombstones),
                    ("health.pstore", check_pstore), ("health.services", check_services),
                    ("health.telephony", check_telephony), ("health.regression", check_regression)):
        run_check(run, cid, fn)


# --- quick mode --------------------------------------------------------------------------

def quick_crashes(run: Run) -> Result:
    ok, text, why = evidence.checked_read(run.dev, "crash", "logcat -b crash -d")
    if not ok:
        return Result("health.crashes", UNVERIFIED, f"logcat -b crash unreadable: {why}")
    procs = evidence.crash_processes(text)
    return Result("health.crashes", FAIL if procs else PASS,
                  ("crashes since boot: " + ", ".join(procs)) if procs else "none since boot")


def quick_tombstones(run: Run) -> Result:
    up = float(run.dev.sh("cat /proc/uptime").split()[0])
    now = int(run.dev.sh("date +%s").strip())
    boot = now - up
    rc, listing, err = run.dev.sh_rc("ls /data/tombstones")
    if rc != 0:
        return Result("health.tombstones", UNVERIFIED, f"cannot list /data/tombstones: {err.strip()}")
    out = ""
    names = [f for f in listing.split() if f.startswith("tombstone")]
    if names:
        rc, out, err = run.dev.sh_rc("stat -c '%Y %n' " + " ".join(f"/data/tombstones/{n}" for n in names))
        if rc != 0:
            return Result("health.tombstones", UNVERIFIED, f"cannot stat tombstones: {err.strip()}")
    new, old = [], []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            (new if int(parts[0]) >= boot else old).append(parts[1])
    if new:
        return Result("health.tombstones", FAIL, f"{len(new)} tombstones since boot")
    return Result("health.tombstones", PASS, "none since boot" + (f" ({len(old)} older)" if old else ""))


def quick_presence(run: Run) -> list[Result]:
    """phone-check's presence lines, reported as INFO (the health group gates)."""
    dev, out = run.dev, []
    built = stage.read_prop(BENCH_HOME / "out-stripped/target/product/river/system/build.prop", "ro.build.date.utc")
    have = dev.getprop("ro.build.date.utc")
    out.append(Result("info.build", INFO, "runs the latest out-stripped build" if built == have else
                      f"phone build {have}, out-stripped {built or 'none'}"))
    wifi = dev.sh("cmd wifi status")
    m = re.search(r'Wifi is connected to "([^"]*)"', wifi)
    out.append(Result("info.wifi", INFO, f"connected to {m.group(1)}" if m else "not connected"))
    bt = dev.sh("dumpsys bluetooth_manager")
    out.append(Result("info.bluetooth", INFO, "on" if re.search(r"^\s*state: ON", bt, re.M) else "off"))
    m = re.search(r"GNSS Hardware Model Name: (.*)", dev.sh("dumpsys location", timeout=60))
    out.append(Result("info.gps", INFO, f"GNSS HAL up ({m.group(1).strip()})" if m else "no GNSS HAL"))
    m = re.search(r"Number of camera devices: (\d+)", dev.sh("dumpsys media.camera", timeout=60))
    out.append(Result("info.cameras", INFO, f"{m.group(1) if m else 0} cameras"))
    m = re.search(r"Total (\d+) h/w sensors", dev.sh("dumpsys sensorservice", timeout=60))
    out.append(Result("info.sensors", INFO, f"{m.group(1) if m else 0} hardware sensors"))
    out.append(Result("info.fingerprint", INFO, "HAL up" if "FingerprintProvider" in dev.sh("dumpsys fingerprint")
                      else "no fingerprint HAL"))
    out.append(Result("info.audio", INFO, "output streams open" if "Output thread" in
                      dev.sh("dumpsys media.audio_flinger", timeout=60) else "no audio output"))
    return out


def quick(run: Run, wait: bool) -> int:
    dev = run.dev
    st = dev.state()
    if st != "device":
        log(f"error: {SERIAL} is not connected in adb device mode (state: {st or 'none'})")
        return 1
    if wait:
        try:
            dev.wait_boot_completed(900)
        except TimeoutError:
            pass
    try:
        run.guard()
        run.add(Result("preflight.guards", PASS, f"{SERIAL} is {CODENAME}"))
    except GuardError as e:
        run.add(Result("preflight.guards", FAIL, str(e)))
        return finish(run, QUICK_REQUIRED)
    dev.root()
    print(f"Phone {SERIAL}: {dev.getprop('ro.lineage.display.version')}, slot {dev.getprop('ro.boot.slot_suffix')}, "
          f"build ID {flashing.partition_build_ids(dev)[0] or 'unstamped'}")
    for cid, fn in (("health.boot", check_boot), ("health.selinux", check_selinux),
                    ("health.crashes", quick_crashes), ("health.tombstones", quick_tombstones),
                    ("health.services", check_services), ("health.telephony", check_telephony)):
        run_check(run, cid, fn)
    run_check(run, "info", quick_presence)
    return finish(run, QUICK_REQUIRED)


# --- baseline ------------------------------------------------------------------------------

def baseline_close(run: Run) -> None:
    """Write known-good.json and close the baseline's pair, only if every
    required baseline check passed."""
    from .results import BASELINE_REQUIRED as req
    bad = [c for c in req if (run.results.get(c) is None or run.results[c].state != PASS)]
    if bad:
        log("Baseline not clean (" + ", ".join(bad) + "): known-good.json not written. "
            "Roll back with: flash --rollback, then run the baseline again.")
        return
    rows = batch_rows(run)
    kg = regression.baseline_record(None, run.batch_id, run.batch.get("commit_set") or {},
                                    [r["run_id"] for r in rows], rows[:3], run.batch["cameras"]["set"])
    st = flashing.load_state()
    sha = flashing.boot_partition_sha(run.dev, run.flash_attempt["target"])
    flashing.slots.close_baseline(st, run.batch_id, run.batch["build_id"], sha, now_iso())
    flashing.save_state(st)
    write_json(KNOWN_GOOD, kg)
    log(f"Baseline closed: known_good_slot {st['known_good_slot']}, RAM reference "
        f"{kg['ram_reference']['median_mb']:.1f} MB (seed), known-good.json written.")
    run.add(Result("baseline.closed", INFO, f"known-good.json written; slot {st['known_good_slot']} is known-good"))


# --- reports ---------------------------------------------------------------------------------

def finish(run: Run, required: list[str], gate: GateInputs | None = None) -> int:
    results = fill_missing(run.results, [c for c in required if run.wanted(c)])
    for cid in required:
        if cid in results and cid not in run.results:
            print(f"  UNVR  {cid:24} {results[cid].summary}")
    code = exit_code(results, [c for c in required if run.wanted(c)])
    eligible, reasons = False, ["not a batch run"]
    if gate is not None:
        eligible, reasons = eligibility(results, gate)
        if eligible and code != 0:
            eligible, reasons = False, ["exit code not 0"]
    if run.mode == "quick":
        eligible, reasons = False, ["--quick is never merge-eligible"]
    doc = {
        "run_id": run.run_id, "mode": run.mode, "batch": run.batch_id, "harness_version": run.version,
        "started": run.started, "finished": time.time(), "exit_code": code,
        "eligible": eligible, "not_eligible_because": reasons, "required": required,
        "only": run.only, "skip": run.skip, "halted": run.halted,
        "results": {k: v.to_json() for k, v in results.items()},
        "flash_attempt": run.attempt_id(), "serial": SERIAL,
    }
    write_json(run.dir / "results.json", doc)
    from .report import write_html
    write_html(run.dir / "report.html", doc, run.batch)
    if run.mode == "batch":
        append_jsonl(HISTORY, {"run_id": run.run_id, "batch": run.batch_id, "exit_code": code,
                               "eligible": eligible, "at": now_iso(),
                               "states": {k: v.state for k, v in results.items()}})
    counts = {s: sum(1 for r in results.values() if r.state == s) for s in (PASS, FAIL, UNVERIFIED, INFO)}
    print(f"\n{counts[PASS]} passed, {counts[FAIL]} failed, {counts[UNVERIFIED]} unverified, {counts[INFO]} info. "
          f"Exit {code}. eligible: {str(eligible).lower()}")
    if not eligible and run.mode in ("batch", "calibrate", "control"):
        print("Not eligible: " + "; ".join(reasons[:8]) + (" ..." if len(reasons) > 8 else ""))
    print(f"Report: {run.dir / 'report.html'}")
    return code


# --- full runs -------------------------------------------------------------------------------

def full_run(run: Run, lock_result: Result) -> int:
    from .features import cleanup_and_final, run_features
    ok = preflight(run, lock_result)
    if ok and run.mode != "baseline" and run.results.get("preflight.helper", Result("x", PASS, "")).state == PASS:
        try:
            identity(run)
            health(run)
            if run.mode != "baseline":
                run_features(run)
        except GuardError as e:
            run.halted = f"guard failed: {e}"
            log(f"HALT: {e}")
        finally:
            if run.mode != "baseline":
                cleanup_and_final(run)
    elif ok and run.mode == "baseline":
        identity(run)
        health(run)
    if run.mode == "baseline":
        baseline_close(run)
        return finish(run, BASELINE_REQUIRED)
    ctrl_ok, ctrl_reason = controls_status(run.version)
    gate = GateInputs(mode=run.mode, full_set=not run.only and not run.skip,
                      experiment=(run.batch or {}).get("experiment"),
                      low_ram=run.dev.getprop("ro.config.low_ram") if run.dev.state() == "device" else "",
                      controls_qualified=ctrl_ok, controls_reason=ctrl_reason, batch_id=run.batch_id)
    code = finish(run, REQUIRED, gate)
    if run.mode == "control":
        from .controls import record_control
        record_control(run, code)
    return code


USAGE = """\
phone-qa: QA for river batches on the Moto G7 test phone (serial ZY225DPT7F,
fixed in the code). Design: qa/DESIGN.md. Usage notes: qa/README.md.

Usage:
  phone-qa --quick [--wait]            Health checks on the running build (the
                                       edit-flash loop). Never merge-eligible.
  phone-qa --batch ID [--only IDS] [--skip IDS]
                                       Full QA of a staged, flashed, benched batch.
  phone-qa --calibrate --batch ID      Every check; regression only records. Never eligible.
  phone-qa --probe [--only IDS]        Every check on whatever the phone runs, with no batch:
                                       batch-bound checks are UNVERIFIED. Never eligible.
  phone-qa --baseline --batch ID       Identity and health only; on a clean run writes
                                       known-good.json and closes the baseline pair.
  phone-qa --control NAME --batch ID   Run one deliberately broken control (qa/README.md).
  phone-qa stage ID [--product DIR] [--kind batch|baseline|calibration|experiment] [--experiment]
                                       Copy a build's images into $BENCH_HOME/batches/ID.
  phone-qa boot-timing [--normal N]    Calibration step 1: reboot-path timing and the
                                       allowance A, with no flash.
  phone-qa controls                    Show which controls are qualified for this harness.
  phone-qa journal                     Show leftovers in the state journal.
  phone-qa helper                      Build (or verify) the river-qa helper APK.
  phone-qa version                     Print the harness version.

IDS is a comma-separated list of check IDs or groups (preflight, identity, health,
radios, hardware, system, apps). Results: $BENCH_HOME/qa/runs/<run-id>/results.json
and report.html. Exit: 1 if any check FAILed, else 2 if any is UNVERIFIED, else 0.
The tester merges only on exit 0, eligible: true, mode batch, matching batch ID.
"""


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    sub = argv[0]
    if sub == "version":
        print(harness_version())
        return 0
    if sub == "stage":
        return cmd_stage(argv[1:])
    if sub == "boot-timing":
        from .calibrate import boot_timing_main
        return boot_timing_main(argv[1:])
    if sub == "controls":
        from .controls import show_controls
        return show_controls(harness_version())
    if sub == "journal":
        j = jmod.Journal(QA_HOME / "journal.json", QA_HOME)
        print(json.dumps(j.entries, indent=2) if j.entries else "Journal is empty.")
        return 0
    if sub == "helper":
        from .helperapk import ensure_helper
        apk, key = ensure_helper(verbose=True)
        print(f"{apk} (cache key {key})")
        return 0
    mode, batch_id, only, skip, wait, control = None, None, [], [], False, None
    it = iter(argv)
    for a in it:
        if a == "--quick":
            mode = "quick"
        elif a == "--batch":
            batch_id = next(it)
            mode = mode or "batch"
        elif a == "--probe":
            mode = "probe"
        elif a == "--calibrate":
            mode = "calibrate"
        elif a == "--baseline":
            mode = "baseline"
        elif a == "--control":
            mode, control = "control", next(it)
        elif a == "--only":
            only += [x for x in next(it).split(",") if x]
        elif a == "--skip":
            skip += [x for x in next(it).split(",") if x]
        elif a == "--wait":
            wait = True
        else:
            log(f"unknown option: {a}")
            print(USAGE)
            return 1
    if mode is None:
        print(USAGE)
        return 1
    if mode not in ("quick", "probe") and not batch_id:
        log(f"error: --{mode} needs --batch ID")
        return 1
    if mode == "control":
        from .controls import CONTROLS
        if control not in CONTROLS:
            log(f"error: unknown control {control!r}; see phone-qa controls")
            return 1
        os.environ["RIVERQA_CONTROL"] = control
    elif control_overrides() and mode == "batch":
        pass  # preflight.controls reports it as a FAIL
    try:
        lock = DeviceLock().acquire()
    except LockError as e:
        log(f"error: {e}")
        return 1
    try:
        run = Run(mode, batch_id, only, skip)
        print(f"phone-qa {mode} run {run.run_id}" + (f", batch {batch_id}" if batch_id else "") +
              f", harness {run.version}")
        if mode == "quick":
            return quick(run, wait)
        lock_result = Result("preflight.lock", PASS, "holding the device lock" +
                             (" (token mode)" if not lock.owned else ""))
        if mode == "control":
            from .controls import run_control
            return run_control(run, control, lock_result)
        return full_run(run, lock_result)
    finally:
        lock.release()


def cmd_stage(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="phone-qa stage")
    ap.add_argument("batch_id")
    ap.add_argument("--product", type=Path, default=BENCH_HOME / "out-stripped/target/product/river")
    ap.add_argument("--kind", default="batch")
    ap.add_argument("--experiment", action="store_true")
    a = ap.parse_args(argv)
    b = stage.stage(a.batch_id, a.product, a.kind, a.experiment or a.kind == "experiment")
    print(json.dumps({k: b[k] for k in ("batch", "kind", "build_id", "dates", "cameras")}, indent=2))
    print(f"Staged in {batch_dir(a.batch_id)}")
    return 0
