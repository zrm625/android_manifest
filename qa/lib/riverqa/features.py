"""The feature checks (radios, hardware, system, apps), cleanup and the
final checks. Each check returns Results; the pass rules are the table in
DESIGN.md ("Checks"). Thresholds come from qa/thresholds.json, which can
never go below the hard floors in FLOORS."""

from __future__ import annotations

import fnmatch
import json
import re
import secrets
import shlex
import statistics
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from . import analysis, helper, journal as jmod
from .controls import active as control_active
from .core import BENCH_HOME, batch_dir, read_json, run as host_run
from .results import FAIL, INFO, PASS, UNVERIFIED, Result, worst

FLOORS = {"audio_min_db": 15.0, "audio_gap_db": 10.0, "video_psnr_db": 30.0, "camera_luma_std": 4.0,
          "camera_frame_diff": 0.0, "extra_dim_drop": 0.30, "torch_delta": 5.0}
PHONE = jmod.PHONE
HOST = jmod.HOST
RES = jmod.RESOURCE
TYPE_BUILTIN_SPEAKER, TYPE_BUILTIN_MIC = "2", "15"
BOND_BONDED, BOND_NONE = "12", "10"
SCAN_MODE_CONNECTABLE_DISCOVERABLE = 23


def injected(run, name: str) -> None:
    """Mark that a control's fault really went in (A1). record_control
    qualifies a control only with this marker and the expected verdict."""
    run.control_injected = name


def th(run, key, default):
    """A threshold, never below its hard floor."""
    v = (run.thresholds.get("values") or {}).get(key, default)
    floor = FLOORS.get(key)
    if floor is not None:
        v = max(float(v), floor)
    return v


def param(run, key, default):
    v = (run.thresholds.get("params") or {}).get(key)
    return default if v is None else v


def server_cfg(run) -> dict:
    return run.fixtures.get("server", {})


def journal_setting(run, ns: str, name: str) -> str | None:
    cur = run.dev.sh(f"settings get {ns} {shlex.quote(name)}").strip()
    run.journal.record(PHONE, "setting", f"{ns}/{name}", None if cur == "null" else cur,
                       attempt_id=run.attempt_id(), run_id=run.run_id)
    return cur


def put_setting(run, ns, name, value):
    journal_setting(run, ns, name)
    run.dev.sh(f"settings put {ns} {shlex.quote(name)} {shlex.quote(str(value))}")


def restore_now(run, where, kind, key) -> tuple[bool, str]:
    from .restore import restore_entry
    e = run.journal.find(where, kind, key)
    if e is None:
        return True, "nothing to restore"
    return restore_entry(run, e)


def helper_result(cid: str, res: helper.HelperResult) -> Result | None:
    """The common outcomes of a helper run that end the check early."""
    if not res.terminal:
        return Result(cid, UNVERIFIED, f"helper: {res.error}", helper_terminal=False)
    fg = helper.fg_problem(res)
    if fg:
        return Result(cid, UNVERIFIED, f"helper not confirmed in front: {fg}", helper_terminal=True)
    if res.get("result") == "ERROR":
        return Result(cid, FAIL, f"helper error: {res.error}", {"stack": res.get("stack")}, helper_terminal=True)
    return None


def top_activity(dev) -> str:
    """The resumed top activity (`dumpsys activity top` lists tasks bottom to top)."""
    return helper.resumed_top(dev.sh("dumpsys activity top", timeout=60))[0]


def screen_off(dev, wait_s: float = 20) -> tuple[bool, str]:
    dev.sh("input keyevent KEYCODE_SLEEP")
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        p = dev.sh("dumpsys power", timeout=30)
        d = dev.sh("dumpsys display", timeout=30)
        wake = re.search(r"mWakefulness=(\w+)", p)
        off = re.search(r"(Display State=OFF|mState=OFF)", d)
        if wake and wake.group(1) in ("Dozing", "Asleep") and off:
            return True, f"wakefulness {wake.group(1)}, display OFF"
        time.sleep(1)
    return False, "screen did not reach Dozing/Asleep with display OFF"


# =========================================================================== radios

def check_wifi(run) -> Result:
    cid = "radios.wifi"
    dev, cfg = run.dev, server_cfg(run)
    if control_active() == "wifi-off":
        run.journal.record(PHONE, "wifi", "wifi", "enabled", attempt_id=run.attempt_id(), run_id=run.run_id)
        dev.sh("cmd wifi set-wifi-enabled disabled")
        time.sleep(3)
        if "Wifi is disabled" in dev.sh("cmd wifi status"):
            injected(run, "wifi-off")
    status = dev.sh("cmd wifi status")
    if run.server.lan_error:
        return Result(cid, UNVERIFIED, f"LAN test listener: {run.server.lan_error}")
    phone_ip = cfg.get("phone_wifi_addr")
    rounds, notes = [], []
    unbound = control_active() == "wifi-unbound"
    if unbound:
        injected(run, "wifi-unbound")
    for rnd in (1, 2):
        if rnd == 2:
            ok, msg = screen_off(dev)
            notes.append(msg)
            if not ok:
                return Result(cid, FAIL, msg)
            time.sleep(30)
            helper.wake(dev)
        token = secrets.token_hex(12)
        lan_url = (f"http://127.0.0.1:{cfg['local_port']}/lan/{token}" if unbound
                   else f"http://{cfg['lan_addr']}:{cfg['lan_port']}/lan/{token}")
        res = helper.instrument(run, "wifi", {"lan_url": lan_url,
                                              "gen_url": "https://connectivitycheck.gstatic.com/generate_204",
                                              "bind": "0" if unbound else "1"}, timeout=90)
        early = helper_result(cid, res)
        if early:
            return early
        hits = run.server.find("lan", token)
        r = {"round": rnd, "network": res.get("network"), "wifi_ip": res.get("wifi_ip"),
             "lan_code": res.get("lan_code"), "lan_error": res.get("lan_error"), "gen_code": res.get("gen_code"),
             "gen_error": res.get("gen_error"), "clients": [h["client"] for h in hits]}
        rounds.append(r)
        if res.get("network") == "none":
            return Result(cid, FAIL, f"round {rnd}: no Wi-Fi network ({status.splitlines()[0] if status else ''})",
                          {"rounds": rounds})
        body_ok = token in (res.get("lan_body") or "")
        from_wifi = bool(hits) and all(h["client"] in (res.get("wifi_ip"), phone_ip) for h in hits) and \
            all(h["client"] == res.get("wifi_ip") for h in hits)
        gen_ok = res.get("gen_code") == "204"
        lan_err = (res.get("lan_error") or "").lower()
        if not body_ok and gen_ok and not hits and ("timeout" in lan_err or "timed out" in lan_err):
            return Result(cid, UNVERIFIED, f"round {rnd}: forge's LAN listener unreachable while generate_204 "
                          "works: the ufw rule is probably missing (qa/README.md)", {"rounds": rounds})
        if not (body_ok and from_wifi and gen_ok):
            return Result(cid, FAIL, f"round {rnd}: page token {'ok' if body_ok else 'missing'}, request from "
                          f"{r['clients'] or 'nowhere'} (Wi-Fi {res.get('wifi_ip')}), generate_204 "
                          f"{res.get('gen_code') or res.get('gen_error')}", {"rounds": rounds})
    if control_active() == "wifi-off":
        restore_now(run, PHONE, "wifi", "wifi")
    return Result(cid, PASS, f"page and generate_204 over Wi-Fi ({rounds[0]['wifi_ip']}) before and after "
                  f"the screen cycle ({notes[0] if notes else ''})", {"rounds": rounds}, helper_terminal=True)


def bt_host(*args, timeout=20) -> str:
    return host_run(["bluetoothctl", *args], timeout=timeout).stdout


class BtSession:
    """A dedicated bluetoothctl with its own NoInputNoOutput agent. It never
    calls default-agent, so the desktop agent stays the default."""

    def __init__(self):
        self.p = subprocess.Popen(["bluetoothctl", "--agent", "NoInputNoOutput"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.out: list[str] = []
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            with self.lock:
                self.out.append(re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\x01|\x02", "", line))

    def send(self, cmd: str):
        self.p.stdin.write(cmd + "\n")
        self.p.stdin.flush()

    def wait_for(self, pattern: str, seconds: float) -> bool:
        end = time.monotonic() + seconds
        rx = re.compile(pattern, re.I)
        while time.monotonic() < end:
            with self.lock:
                if any(rx.search(l) for l in self.out):
                    return True
            time.sleep(0.2)
        return False

    def text(self) -> str:
        with self.lock:
            return "".join(self.out)

    def close(self):
        try:
            self.send("quit")
            self.p.wait(5)
        except Exception:  # noqa: BLE001
            self.p.kill()


def bt_rfkill_stub_took(real: str, stubbed: str) -> bool:
    """The real `bluetoothctl show` was a valid report of a powered adapter
    and the stub removed "Powered: yes"."""
    return (bool(re.search(r"Controller [0-9A-Fa-f:]{17}", real)) and "Powered: yes" in real
            and "Powered: yes" not in stubbed)


def check_bluetooth(run) -> list[Result]:
    cid = "radios.bluetooth"
    dev, cfg = run.dev, server_cfg(run)
    forge = cfg.get("forge_bt_addr", "").upper()
    show = bt_host("show")
    if control_active() == "bt-rfkill":
        # Control: only the adapter report is stubbed, as an rfkilled adapter
        # prints it; the parsing below is the real check. It counts as
        # injected only if the stub replaced a valid report of a powered
        # adapter; empty or missing output proves nothing (review round 2).
        real = show
        show = re.sub(r"Powered: yes", "Powered: no", show)
        show = re.sub(r"PowerState: on", "PowerState: off-blocked", show)
        if bt_rfkill_stub_took(real, show):
            injected(run, "bt-rfkill")
    if "Powered: yes" not in show:
        # The harness never powers the host adapter itself.
        return [Result(cid, UNVERIFIED, "host Bluetooth adapter is not powered (fixture problem)")]
    m = re.search(r"Controller ([0-9A-F:]{17})", show)
    if m and forge and m.group(1).upper() != forge:
        return [Result(cid, UNVERIFIED, f"host adapter is {m.group(1)}, fixtures.json says {forge}")]
    was_on = "enabled: true" in dev.sh("dumpsys bluetooth_manager")
    run.journal.record(PHONE, "bt_enabled", "bt", "true" if was_on else "false", attempt_id=run.attempt_id(),
                       run_id=run.run_id)
    dev.sh("cmd bluetooth_manager enable; cmd bluetooth_manager wait-for-state:STATE_ON", timeout=40)
    phone = dev.sh("settings get secure bluetooth_address").strip().upper()
    if not re.match(r"^[0-9A-F:]{17}$", phone):
        return [Result(cid, FAIL, f"phone Bluetooth address unreadable after STATE_ON ({phone!r})")]
    disc = "yes" if "Discoverable: yes" in show else "no"
    run.journal.record(HOST, "bt_discoverable", "forge", disc, run_id=run.run_id)
    bt_host("remove", phone)  # that address is only ever the test phone: any bond is QA's
    details: dict = {"phone": phone, "forge": forge}
    # Discovery: forge discoverable, the helper scans.
    bt_host("discoverable", "on")
    found, attempts = False, 0
    for attempts in (1, 2):  # environmental retry: forge not seen in the window
        res = helper.instrument(run, "bt_scan", {"addr": forge, "timeout_s": "30"}, timeout=90)
        early = helper_result(cid, res)
        if early:
            restore_now(run, HOST, "bt_discoverable", "forge")
            return [early]
        found = res.get("found") == "true"
        details[f"scan{attempts}"] = {"found": found, "seen": res.get("seen")}
        if found:
            break
    ok_disc, _ = restore_now(run, HOST, "bt_discoverable", "forge")
    if not found:
        return [Result(cid, FAIL, "forge not found in two scans", details, attempts=attempts, helper_terminal=True)]
    # Make the phone visible: snapshot the scan mode, relax hidden_api_policy.
    sm = helper.instrument(run, "bt_scan_mode", {}, timeout=30, need_foreground=False)
    if not sm.terminal:
        return [Result(cid, UNVERIFIED, f"helper: {sm.error}", helper_terminal=False)]
    run.journal.record(PHONE, "bt_scan_mode", "bt", sm.get("scan_mode"), attempt_id=run.attempt_id(),
                       run_id=run.run_id)
    put_setting(run, "global", "hidden_api_policy", "1")
    run.journal.record(RES, "bt_bond", forge, None, run_id=run.run_id, extra={"phone_addr": phone})
    s = BtSession()
    paired = False
    try:
        r = helper.instrument(run, "bt_set_scan_mode", {"mode": SCAN_MODE_CONNECTABLE_DISCOVERABLE,
                                                        "timeout_s": "120"}, timeout=30, need_foreground=False)
        details["discoverable"] = r.values
        s.send("scan bredr")
        seen = s.wait_for(re.escape(phone), 60)
        s.send("scan off")
        if not seen:
            return [Result(cid, FAIL, "forge never saw the phone while it was discoverable", details,
                           helper_terminal=True)]

        def on_stage(stage):
            if stage == "pair-ready":
                s.send(f"pair {phone}")
            return None
        pr = helper.instrument(run, "bt_accept_pair", {"addr": forge, "timeout_s": "60"}, timeout=90,
                               on_stage=on_stage, need_foreground=False)
        s.wait_for(r"Pairing successful|Failed to pair", 15)
        details["pair"] = pr.values
        info = bt_host("info", phone)
        paired = pr.get("bond_state") == BOND_BONDED and "Paired: yes" in info
        details["host_paired"] = "Paired: yes" in info
        if not pr.terminal:
            return [Result(cid, UNVERIFIED, f"helper: {pr.error}", details, helper_terminal=False)]
    finally:
        s.close()
        # Cleanup, verified on both ends: unbond, scan mode, policy, on/off.
        ok_bond, bmsg = restore_now(run, RES, "bt_bond", forge)
        ok_scan, smsg = restore_now(run, PHONE, "bt_scan_mode", "bt")
        ok_pol, _ = restore_now(run, PHONE, "setting", "global/hidden_api_policy")
        ok_on, _ = restore_now(run, PHONE, "bt_enabled", "bt")
    show2 = bt_host("show")
    disc_ok = f"Discoverable: {disc}" in show2
    details["cleanup"] = {"bond": bmsg, "scan_mode": smsg, "policy": ok_pol, "bt_onoff": ok_on,
                          "host_discoverable": disc_ok}
    if not paired:
        return [Result(cid, FAIL, f"pairing failed (phone bond {details.get('pair', {}).get('bond_state')}, "
                       f"host paired {details.get('host_paired')})", details, helper_terminal=True)]
    if not (ok_bond and ok_scan and ok_pol and ok_on and disc_ok and ok_disc):
        return [Result(cid, FAIL, f"paired, but cleanup not verified: {details['cleanup']}", details,
                       helper_terminal=True)]
    return [Result(cid, PASS, f"forge found (attempt {attempts}), paired and bonded both ends, cleaned up",
                   details, attempts=attempts, helper_terminal=True)]


NFC_PHONE_FILES = ["/vendor/bin/hw/android.hardware.nfc@1.2-service", "/vendor/etc/libnfc-nxp.conf",
                   "/vendor/etc/libnfc-nci.conf", "/vendor/lib64/nfc_nci_nxp.so", "/odm/etc/vintf/manifest_nfc.xml"]
NFC_BUILD = ["/system/apex/com.android.nfcservices.capex", "/system/priv-app/Tag/Tag.apk",
             "/vendor/odm/etc/permissions/sku_nfc/*.xml", "/vendor/odm/etc/vintf/manifest_nfc.xml",
             "/vendor/lib64/nfc_nci_nxp.so"]


def check_nfc(run) -> Result:
    cid = "radios.nfc"
    dev = run.dev
    miss = []
    if "com.android.nfcservices" not in dev.sh("ls /apex"):
        miss.append("nfcservices APEX not active")
    if "com.android.apps.tag" not in dev.sh("pm list packages com.android.apps.tag"):
        miss.append("Tag not installed")
    for f in NFC_PHONE_FILES:
        if dev.sh(f"[ -e {f} ] && echo y").strip() != "y":
            miss.append(f"{f} missing")
    if not dev.sh("ls /vendor/etc/init/ | grep -i nfc").strip():
        miss.append("NFC HAL init .rc missing")
    if not dev.sh("ls /odm/etc/permissions/sku_nfc/*.xml 2>/dev/null").strip():
        miss.append("/odm/etc/permissions/sku_nfc/*.xml missing")
    if run.batch is None:
        return Result(cid, UNVERIFIED, "no batch: cannot check the staged installed-files", {"phone": miss})
    names = set()
    bdir = batch_dir(run.batch_id)
    for part, rec in (run.batch.get("installed_files") or {}).items():
        p = bdir / rec["file"]
        from .core import sha256_file
        if not p.exists() or sha256_file(p) != rec["sha256"]:
            return Result(cid, UNVERIFIED, f"staged {rec['file']} missing or changed")
        names |= {e.get("Name", "") for e in json.loads(p.read_text())}
    if not names:
        return Result(cid, UNVERIFIED, "batch has no staged installed-files copies")
    for pat in NFC_BUILD:
        if not any(fnmatch.fnmatch(n, pat) for n in names):
            miss.append(f"build: {pat} missing")
    if miss:
        return Result(cid, FAIL, "; ".join(miss))
    return Result(cid, PASS, "NFC stack present on the phone and in the staged build (no NFC chip to use it)")


def check_gps(run) -> Result:
    cid = "radios.gps"
    dev = run.dev
    cur = dev.sh("cmd location is-location-enabled").strip()
    run.journal.record(PHONE, "location", "location", cur, attempt_id=run.attempt_id(), run_id=run.run_id)
    if control_active() == "location-off":
        dev.sh("cmd location set-location-enabled false")
        if dev.sh("cmd location is-location-enabled").strip() == "false":
            injected(run, "location-off")
    else:
        dev.sh("cmd location set-location-enabled true")
    res, attempts = None, 0
    try:
        for attempts in (1, 2):
            res = helper.instrument(run, "gps", {"timeout_s": "120"}, timeout=200)
            early = helper_result(cid, res)
            if early:
                return early
            started = int(res.get("started_ms") or -1) >= 0
            cn0 = float(res.get("max_cn0") or 0)
            # Environmental: the engine started but no satellite with Cn0 > 0 yet.
            if not (started and cn0 <= 0):
                break
    finally:
        restore_now(run, PHONE, "location", "location")
    v = res.values
    started = int(v.get("started_ms") or -1) >= 0
    cbs = int(v.get("status_callbacks") or 0)
    cn0 = float(v.get("max_cn0") or 0)
    d = {k: v.get(k) for k in ("location_enabled", "started_ms", "status_callbacks", "max_cn0", "satellites",
                               "first_cn0_ms")}
    if started and cbs > 0 and cn0 > 0:
        return Result(cid, PASS, f"engine started, {v.get('satellites')} satellites, max Cn0 {cn0:.1f} dB-Hz "
                      f"after {int(v.get('first_cn0_ms')) / 1000:.0f} s", d, attempts=attempts, helper_terminal=True)
    return Result(cid, FAIL, f"started={started}, status callbacks={cbs}, max Cn0={cn0} "
                  f"(location enabled: {v.get('location_enabled')})", d, attempts=attempts, helper_terminal=True)


# =========================================================================== hardware

def expected_cameras(run) -> tuple[list[int] | None, str]:
    if run.batch is None:
        if run.mode == "probe":
            from .stage import BOOTSTRAP_CAMERAS
            return list(BOOTSTRAP_CAMERAS), "probe run: bootstrap set, no batch to confirm it"
        return None, "no batch.json"
    b = run.batch.get("cameras") or {}
    bset = b.get("set")
    if not isinstance(bset, list) or not bset:
        return None, "batch.json camera set missing or malformed"
    kg = (read_json(BENCH_HOME / "qa" / "known-good.json", {}) or {}).get("latest") or {}
    kset = kg.get("cameras")
    prop = run.dev.getprop("ro.vendor.river.cameras")
    pset = sorted(int(x) for x in re.findall(r"\d+", prop)) if prop else None
    change = run.batch.get("camera_set_change")
    if change:
        ok, why = verify_camera_change(change, run.batch)
        if not ok:
            return None, f"camera_set_change not confirmed: {why}"
        new = sorted(change.get("set") or [])
        if sorted(bset) != new or (pset is not None and pset != new):
            return None, "approved camera set differs from the device switches or ro.vendor.river.cameras"
        return new, f"Zak-approved change ({change.get('url')})"
    if kset is None and run.mode not in ("calibrate",):
        return None, "known-good.json has no camera set"
    sets = {"batch.json": sorted(bset)}
    if kset is not None:
        sets["known-good.json"] = sorted(kset)
    if pset is not None:
        sets["ro.vendor.river.cameras"] = pset
    if len({tuple(s) for s in sets.values()}) != 1:
        return None, f"camera set changed, needs Zak: {sets}"
    return sorted(bset), ", ".join(sets)


APPROVAL_URL_RE = re.compile(r"^https://github\.com/(zrm625/[A-Za-z0-9._-]+)/pull/(\d+)"
                             r"#(issuecomment|pullrequestreview)-(\d+)$")
# One line only: [ \t] instead of \s, so neither side can run into the next line.
APPROVAL_LINE_RE = re.compile(r"(?im)^[ \t]*approve camera set:[ \t]*([0-9][0-9, \t]*?)[ \t]*\r?$")
APPROVER = "zrm625"


def verify_camera_change(change: dict, batch: dict, runner=None) -> tuple[bool, str]:
    """Zak's approval of a camera-set change (A7). The URL must be a comment
    or review on one of this batch's own PRs in a zrm625 repo (anchored
    match); its author, not the PR's, must be zrm625; and its body must name
    the new set on a line "approve camera set: 0,1,2" equal to change["set"]."""
    runner = runner or (lambda cmd: host_run(cmd, timeout=30))
    url = change.get("url") or ""
    m = APPROVAL_URL_RE.match(url)
    if not m:
        return False, "approval URL is not a comment or review on a zrm625 pull request"
    repo, num, kind, cid = m.groups()
    if {"repo": repo, "number": int(num)} not in [{"repo": p.get("repo"), "number": int(p.get("number", -1))}
                                                  for p in batch.get("prs") or []]:
        return False, f"{repo}#{num} is not one of this batch's PRs"
    api = (f"repos/{repo}/issues/comments/{cid}" if kind == "issuecomment"
           else f"repos/{repo}/pulls/{num}/reviews/{cid}")
    r = runner(["gh", "api", api])
    if r.returncode != 0:
        return False, "gh lookup failed"
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return False, "gh returned no JSON"
    login = (data.get("user") or {}).get("login")
    if login != APPROVER:
        return False, f"approval written by {login!r}, not {APPROVER}"
    parent = data.get("issue_url") if kind == "issuecomment" else data.get("pull_request_url")
    if not parent or not re.search(rf"/repos/{re.escape(repo)}/(issues|pulls)/{num}$", parent):
        return False, "the approval does not belong to that pull request"
    named = APPROVAL_LINE_RE.findall(data.get("body") or "")
    sets = [sorted(int(x) for x in re.findall(r"\d+", n)) for n in named]
    want = sorted(change.get("set") or [])
    if want not in sets:
        return False, f"the approval does not name the set {want} (found {sets or 'none'})"
    return True, f"approved by {login} for {want}"


def check_cameras(run) -> list[Result]:
    cid = "hardware.cameras"
    expected, why = expected_cameras(run)
    if expected is None:
        return [Result(cid, UNVERIFIED, why)]
    res = helper.instrument(run, "camera_list", {}, timeout=30, need_foreground=False)
    if not res.terminal:
        return [Result(cid, UNVERIFIED, f"helper: {res.error}", helper_terminal=False)]
    have = sorted(int(x) for x in (res.get("ids") or "").split(",") if x.strip().isdigit())
    if have != expected:
        return [Result(cid, FAIL, f"getCameraIdList {have} != expected {expected} ({why})", helper_terminal=True)]
    floor = th(run, "camera_frame_diff", 0.0)
    out, problems, details = [], [], {}
    for camid in expected:
        r = helper.instrument(run, "camera_live", {"id": camid, "frames": 10, "deadline_s": 20}, timeout=60)
        early = helper_result(cid, r)
        if early:
            return [early]
        ts = [int(x) for x in (r.get("timestamps") or "").split(",") if x]
        diffs = [float(x) for x in (r.get("diffs") or "").split(",") if x]
        inc = all(b > a for a, b in zip(ts, ts[1:]))
        details[f"camera{camid}"] = {"frames": len(ts), "increasing": inc, "min_diff": min(diffs) if diffs else None,
                                     "means": r.get("means"), "exposure_ns": r.get("exposure_ns"),
                                     "iso": r.get("iso"), "manual": r.get("manual")}
        if len(ts) != 10 or not inc or not diffs or min(diffs) <= floor:
            problems.append(f"camera {camid}: {len(ts)} frames, increasing {inc}, min diff "
                            f"{min(diffs) if diffs else 'n/a'} (floor {floor})")
    torch_detail = None
    if 0 in expected:
        iso = param(run, "camera0_photo_iso", 100)
        exp = param(run, "camera0_photo_exposure_ns", 20_000_000)
        led_note, held_nodes, took = None, [], False
        if control_active() == "torch-disabled":
            took, led_note, held_nodes = disable_torch(run)
        try:
            r = helper.instrument(run, "camera_photo", {"id": 0, "torch": 1, "iso": iso, "exposure_ns": exp},
                                  timeout=60)
        finally:
            if control_active() == "torch-disabled":
                # Read back straight after the capture, before restoring:
                # the cap must still be in place (review round 2).
                held, hold_note = torch_still_held(run, held_nodes)
                restore_led(run)
                led_note = f"{led_note}; after the capture: {hold_note}"
                if took and held:
                    injected(run, "torch-disabled")
        early = helper_result(cid, r)
        if early:
            return [early]
        fs = (r.get("flash_states") or "").split(",")
        l0, l1 = float(r.get("luma0") or 0), float(r.get("luma1") or 0)
        s1 = float(r.get("std1") or 0)
        delta_min = th(run, "torch_delta", 5.0)
        std_min = th(run, "camera_luma_std", 4.0)
        torch_detail = {"flash_states": fs, "luma_off": l0, "luma_on": l1, "std_on": s1, "iso": iso,
                        "exposure_ns": exp, "led_control": led_note}
        details["camera0_photo"] = torch_detail
        fired = len(fs) > 1 and fs[1] == "3"  # FLASH_STATE_FIRED
        if not (fired and l1 - l0 >= delta_min and l1 >= 20 and s1 >= std_min):
            problems.append(f"camera 0 photo: FLASH_STATE {fs[1] if len(fs) > 1 else '?'}, luma off {l0:.1f} on "
                            f"{l1:.1f} (need +{delta_min}), on >= 20, std {s1:.1f} (need {std_min})")
    for camid in [c for c in expected if c != 0]:
        r = helper.instrument(run, "camera_photo", {"id": camid, "torch": 0}, timeout=60)
        if r.ok:
            out.append(Result(f"info.camera{camid}_photo", INFO, f"camera {camid} photo luma {r.get('luma0')}, "
                              f"std {r.get('std0')}"))
        else:
            out.append(Result(f"info.camera{camid}_photo", INFO, f"camera {camid} photo failed: {r.error}"))
    if problems:
        out.insert(0, Result(cid, FAIL, "; ".join(problems), details, helper_terminal=True))
    elif run.batch is None:
        out.insert(0, Result(cid, UNVERIFIED, f"no batch to confirm the camera set; with the bootstrap set "
                             f"{expected} every check passed", details, helper_terminal=True))
    else:
        out.insert(0, Result(cid, PASS, f"cameras {expected} live" + (
            f"; torch photo luma {torch_detail['luma_off']:.0f} -> {torch_detail['luma_on']:.0f}, FIRED"
            if torch_detail else ""), details, helper_terminal=True))
    return out


def torch_leds(run) -> list[str]:
    return [p for p in run.dev.sh("ls -d /sys/class/leds/led:switch_* /sys/class/leds/led:torch_* "
                                  "/sys/class/leds/led:flash_* 2>/dev/null").split() if p]


def disable_torch(run) -> tuple[bool, str, list[str]]:
    """Control only: hold the torch dark below the HAL for the whole
    capture: (took, note, capped nodes).

    A switch write alone doesn't hold: on this driver the next positive
    brightness write, which the torch request makes, re-enables the switch
    (leds-qpnp-flash-v2.c:1753). So every led:torch_*/led:flash_* node also
    gets max_brightness 0. The LED core clamps every brightness write to
    max_brightness, the camera's trigger writes included (led-core.c), and
    only a sysfs write changes it. Each original value is journaled first.
    True only if every capped node reads max_brightness 0 and brightness 0."""
    notes = []
    leds = torch_leds(run)
    nodes = [led for led in leds if "/led:torch_" in led or "/led:flash_" in led]
    if not nodes:
        return False, "no led:torch_* or led:flash_* node", []
    for led in nodes:
        f = f"{led}/max_brightness"
        r = run.dev.read(f"cat {f}")
        if not r.ok or not r.out.strip().isdigit():
            return False, f"cannot read {f}", nodes
        run.journal.record(PHONE, "sysfs", f, r.out.strip(), attempt_id=run.attempt_id(), run_id=run.run_id)
        run.dev.sh(f"echo 0 > {f}")
        notes.append(f"{f}: {r.out.strip()} -> 0")
    for led in leds:
        if "/led:switch_" not in led:
            continue
        f = f"{led}/brightness"
        cur = run.dev.sh(f"cat {f}").strip()
        run.journal.record(PHONE, "sysfs", f, cur, attempt_id=run.attempt_id(), run_id=run.run_id)
        run.dev.sh(f"echo 0 > {f}")
        notes.append(f"{f}: {cur} -> 0")
    held, why = torch_still_held(run, nodes)
    return held, "; ".join(notes) + f"; {why}", nodes


def torch_still_held(run, nodes: list[str]) -> tuple[bool, str]:
    """Is every capped node still at max_brightness 0 and brightness 0? A
    failed read is not proof (False)."""
    if not nodes:
        return False, "no capped torch nodes"
    bad = []
    for led in nodes:
        for attr in ("max_brightness", "brightness"):
            r = run.dev.read(f"cat {led}/{attr}")
            if not r.ok or r.out.strip() != "0":
                bad.append(f"{led}/{attr}={r.out.strip() if r.ok else 'unreadable'}")
    return (not bad), ("held at 0" if not bad else "not held: " + ", ".join(bad))


def restore_led(run):
    for e in [e for e in run.journal.entries if e["kind"] == "sysfs" and "/leds/" in e["key"]]:
        from .restore import restore_entry
        restore_entry(run, e)


def check_audio(run) -> Result:
    cid = "hardware.audio"
    dev = run.dev
    out = dev.sh("cmd media_session volume --stream 3 --get")
    m = re.search(r"volume is (\d+)", out)
    if not m:
        return Result(cid, UNVERIFIED, "cannot read STREAM_MUSIC volume")
    run.journal.record(PHONE, "volume_music", "music", m.group(1), attempt_id=run.attempt_id(), run_id=run.run_id)
    level = 0 if control_active() == "audio-volume-0" else int(param(run, "audio_volume", 9))
    dev.sh(f"cmd media_session volume --stream 3 --set {level}")
    if level == 0 and control_active() == "audio-volume-0" and \
            "volume is 0 " in dev.sh("cmd media_session volume --stream 3 --get"):
        injected(run, "audio-volume-0")
    try:
        res = helper.instrument(run, "audio", {"amplitude": param(run, "audio_amplitude", 0.5)}, timeout=60)
    finally:
        restore_now(run, PHONE, "volume_music", "music")
    early = helper_result(cid, res)
    if early:
        return early
    pcm = run.upload_dir / "audio.pcm"
    if not pcm.exists():
        return Result(cid, UNVERIFIED, "no audio upload from the helper", helper_terminal=True)
    x = analysis.pcm16(pcm.read_bytes())
    m = analysis.analyse_audio(x, int(res.get("rate")), int(res.get("play_at")))
    m.update({"source": res.get("source"), "out_device": res.get("out_device"), "in_device": res.get("in_device"),
              "volume": level})
    routes_ok = res.get("out_device") == TYPE_BUILTIN_SPEAKER and res.get("in_device") == TYPE_BUILTIN_MIC
    ok, msg = analysis.audio_verdict(m, th(run, "audio_min_db", 15.0), th(run, "audio_gap_db", 10.0),
                                     float(param(run, "audio_level_floor_dbfs", -70.0)))
    if not routes_ok:
        return Result(cid, FAIL, f"routed to out {res.get('out_device')} in {res.get('in_device')}, expected "
                      "speaker (2) and built-in mic (15)", m, helper_terminal=True)
    return Result(cid, PASS if ok else FAIL, f"{res.get('source')}: {msg}", m, helper_terminal=True)


def check_fingerprint(run) -> Result:
    cid = "hardware.fingerprint"
    res = helper.instrument(run, "fingerprint", {}, timeout=40)
    early = helper_result(cid, res)
    if early:
        return early
    from .qa import proc_info, hal_deaths
    now = proc_info(run.dev).get("fingerprint_hal")
    was = run.snapshot.get("procs", {}).get("fingerprint_hal")
    deaths = hal_deaths(run.dev)
    hw = res.get("hardware_detected") == "true"
    same = bool(now and was and now["pid"] == was["pid"] and now["start"] == was["start"])
    d = {"hardware_detected": hw, "hal_now": now, "hal_preflight": was, "hal_deaths": deaths}
    if deaths is None:
        return Result(cid, UNVERIFIED, "HAL deaths line not found in dumpsys fingerprint", d, helper_terminal=True)
    ok = hw and same and deaths == "0"
    return Result(cid, PASS if ok else FAIL, f"isHardwareDetected {hw}, HAL {'unchanged' if same else 'changed/dead'}, "
                  f"HAL deaths {deaths}", d, helper_terminal=True)


def check_sensors(run) -> Result:
    cid = "hardware.sensors"
    res = helper.instrument(run, "sensors", {"ms": 3000}, timeout=40)
    early = helper_result(cid, res)
    if early:
        return early
    n = int(res.get("accel_n") or 0)
    med = float(res.get("accel_median") or "nan")
    lights = [float(x) for x in (res.get("light") or "").split(",") if x]
    prox = [float(x) for x in (res.get("prox") or "").split(",") if x]
    pmax = float(res.get("prox_max") or "nan")
    import math
    a_ok = n >= 20 and abs(med - 9.81) <= 1.5
    l_ok = any(math.isfinite(v) and v >= 0 for v in lights)
    p_ok = any(0 <= v <= pmax for v in prox)
    d = {"accel_n": n, "accel_median": med, "light": lights[:5], "prox": prox[:5], "prox_max": pmax}
    return Result(cid, PASS if a_ok and l_ok and p_ok else FAIL,
                  f"accel {n} samples median {med:.2f}; light {len(lights)} values; proximity {prox[:3]} "
                  f"in [0, {pmax}]", d, helper_terminal=True)


def check_vibration(run) -> Result:
    cid = "hardware.vibration"
    res = helper.instrument(run, "vibrate", {}, timeout=30)
    early = helper_result(cid, res)
    if early:
        return early
    req = int(res.get("requested_at_ms") or 0)
    dump = run.dev.sh("dumpsys vibrator_manager", timeout=30)
    tz = run.dev.sh("date +%z").strip()
    ok, line = vib_entry(dump, req, tz)
    return Result(cid, PASS if ok else FAIL, line, {"requested_at_ms": req, "phone_tz": tz}, helper_terminal=True)


def vib_entry(dump: str, req_ms: int, tz: str) -> tuple[bool, str]:
    """A river.qa entry in dumpsys vibrator_manager's recent vibrations, newer
    than the request, with status finished. Entry times are phone-local
    ("11-22 21:56:21.875 | effect | finished | ...")."""
    import datetime as dt
    m = re.match(r"([+-])(\d\d)(\d\d)$", tz)
    off = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3))) * (1 if m.group(1) == "+" else -1) if m \
        else dt.timedelta(0)
    req = dt.datetime.fromtimestamp(req_ms / 1000, dt.timezone.utc)
    best = None
    for line in dump.splitlines():
        if "river.qa" not in line:
            continue
        cols = [c.strip() for c in line.split("|")]
        t = re.match(r"(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\.(\d{3})", cols[0])
        if not t or len(cols) < 3:
            continue
        mo, d, hh, mi, ss, ms = map(int, t.groups())
        local = dt.datetime(req.year, mo, d, hh, mi, ss, ms * 1000, tzinfo=dt.timezone(off))
        if local - req > dt.timedelta(days=200):
            local = local.replace(year=req.year - 1)
        if best is None or local > best[0]:
            best = (local, cols[2])
    if best is None:
        return False, "no river.qa entry in dumpsys vibrator_manager"
    t, status = best
    newer = t >= req - dt.timedelta(seconds=2)
    ok = newer and status == "finished"
    return ok, (f"river.qa vibration {status}, {(t - req).total_seconds():+.1f} s after the request" if newer
                else f"latest river.qa vibration ({status}) is older than the request")


def backlight(dev) -> int | None:
    for p in ("/sys/class/leds/lcd-backlight/brightness", "/sys/class/backlight/panel0-backlight/brightness"):
        v = dev.sh(f"cat {p} 2>/dev/null").strip()
        if v.isdigit():
            return int(v)
    return None


def color_matrix_diag(dev, run=None) -> list[float] | None:
    """The diagonal of SurfaceFlinger's colorTransformMatrix."""
    if control_active() == "extra-dim-stub" and run is not None:
        injected(run, "extra-dim-stub")
        return [1.0, 1.0, 1.0]
    sf = dev.sh("dumpsys SurfaceFlinger", timeout=60)
    i = sf.find("colorTransformMatrix")
    if i < 0:
        return None
    nums = [float(x) for x in re.findall(r"-?\d+\.\d+", sf[i:i + 600])[:16]]
    if len(nums) < 16:
        return None
    return [nums[0], nums[5], nums[10]]


def check_screen(run) -> Result:
    cid = "hardware.screen"
    dev = run.dev
    helper.wake(dev)
    dev.sh(f"am start -W -n {helper.PKG}/.MainActivity")
    time.sleep(1)
    ok, fg = helper.foreground(dev)
    if not ok:
        return Result(cid, UNVERIFIED, f"helper not in front: {fg}")
    notes, d, bad, unv = [], {}, [], []
    put_setting(run, "system", "screen_brightness_mode", 0)
    journal_setting(run, "system", "screen_brightness")
    vals = {}
    for level in (40, 200):
        dev.sh(f"settings put system screen_brightness {level}")
        time.sleep(1.5)
        vals[level] = backlight(dev)
    d["backlight"] = vals
    if vals[40] is None or vals[200] is None:
        unv.append("lcd-backlight sysfs unreadable")
    elif not vals[200] > vals[40]:
        bad.append(f"backlight {vals[40]} -> {vals[200]} did not follow 40 -> 200")
    else:
        notes.append(f"backlight {vals[40]} -> {vals[200]}")
    journal_setting(run, "secure", "reduce_bright_colors_activated")
    diag = {}
    for v in (0, 1):
        dev.sh(f"settings put secure reduce_bright_colors_activated {v}")
        time.sleep(1.5)
        cd = dev.sh("dumpsys color_display", timeout=30)
        m = re.search(r"Reduce bright colors:.*?Activated:\s*(\w+)", cd, re.S)
        diag[v] = (m.group(1) if m else None, color_matrix_diag(dev, run))
    d["extra_dim"] = {str(k): v for k, v in diag.items()}
    if diag[0][0] != "false" or diag[1][0] != "true":
        bad.append(f"color_display Activated {diag[0][0]} -> {diag[1][0]}")
    if diag[0][1] is None or diag[1][1] is None:
        unv.append("colorTransformMatrix not found")
    else:
        off, on = statistics.mean(diag[0][1]), statistics.mean(diag[1][1])
        drop = 1 - on / off if off else 0
        need = th(run, "extra_dim_drop", 0.30)
        d["extra_dim_drop"] = round(drop, 3)
        if drop < need:
            bad.append(f"matrix diagonal {off:.2f} -> {on:.2f} drops {drop:.0%} < {need:.0%}")
        else:
            notes.append(f"Extra dim matrix {off:.2f} -> {on:.2f}")
    # Rotation, with the helper in front receiving configuration changes.
    cur = dev.sh("cmd window user-rotation").strip()
    run.journal.record(PHONE, "user_rotation", "rotation", cur, attempt_id=run.attempt_id(), run_id=run.run_id)
    seen = {}

    def on_stage(stage):
        def go():
            time.sleep(1)
            dev.sh("cmd window user-rotation lock 1")
            time.sleep(2.5)
            seen["after90"] = re.findall(r"mCurrentRotation=(ROTATION_\d+)", dev.sh("dumpsys window displays"))
            dev.sh("cmd window user-rotation lock 0")
            time.sleep(2.5)
            seen["after0"] = re.findall(r"mCurrentRotation=(ROTATION_\d+)", dev.sh("dumpsys window displays"))
        return go
    res = helper.instrument(run, "rotate", {"timeout_s": 20}, timeout=60, on_stage=on_stage)
    restore_now(run, PHONE, "user_rotation", "rotation")
    for key in ("system/screen_brightness", "system/screen_brightness_mode", "secure/reduce_bright_colors_activated"):
        restore_now(run, PHONE, "setting", key)
    early = helper_result(cid, res)
    if early:
        return early
    rots = res.get("rotations") or ""
    d["rotation"] = {"helper": rots, **seen}
    r90 = "ROTATION_90" in (seen.get("after90") or [])[:1]
    r0 = "ROTATION_0" in (seen.get("after0") or [])[:1]
    seq = [int(x) for x in rots.split(",") if x]
    cfg_ok = 1 in seq and 0 in seq[seq.index(1):] if 1 in seq else False
    if not (r90 and r0 and cfg_ok):
        bad.append(f"rotation: window {seen}, helper config changes {seq}")
    else:
        notes.append("rotation 90 and back")
    st = FAIL if bad else (UNVERIFIED if unv else PASS)
    return Result(cid, st, "; ".join(bad + unv + notes), d, helper_terminal=True)


def check_battery(run) -> Result:
    cid = "hardware.battery"
    dev = run.dev
    b = dev.sh("dumpsys battery")
    kv = dict(re.findall(r"^\s+([A-Za-z ]+): (.*)$", b, re.M))
    cap = dev.sh("cat /sys/class/power_supply/battery/capacity").strip()
    present = kv.get("present") == "true"
    level = int(kv.get("level", "-1"))
    status = kv.get("status")
    powered = [k for k in ("AC powered", "USB powered", "Wireless powered", "Dock powered") if kv.get(k) == "true"]
    volt = int(kv.get("voltage", "0"))
    ok = (present and 1 <= level <= 100 and cap.isdigit() and abs(level - int(cap)) <= 1 and
          (status in ("2", "5") or (status == "4" and powered)) and powered and 3400 <= volt <= 4500)
    return Result(cid, PASS if ok else FAIL, f"present {present}, level {level} (capacity {cap}), status {status}, "
                  f"powered {powered or 'none'}, {volt} mV", kv)


def usb_dev_dir(serial: str) -> Path | None:
    for d in Path("/sys/bus/usb/devices").iterdir():
        try:
            if (d / "serial").read_text().strip() == serial:
                return d
        except OSError:
            pass
    return None


def mtp_interface_count(root: Path = Path("/sys/bus/usb/devices")) -> list[str]:
    """Serials of USB devices with an MTP interface (class 6, subclass 1, or
    'MTP'). The entries under /sys/bus/usb/devices are symlinks; the
    interface's real directory sits inside its device's, so resolve it
    before looking at the parent's serial (A10)."""
    out = []
    for d in root.iterdir():
        if ":" in d.name:
            try:
                cls = (d / "bInterfaceClass").read_text().strip()
                sub = (d / "bInterfaceSubClass").read_text().strip()
                name = (d / "interface").read_text().strip() if (d / "interface").exists() else ""
                if (cls == "06" and sub == "01") or "MTP" in name:
                    parent = d.resolve().parent
                    out.append((parent / "serial").read_text().strip() if (parent / "serial").exists() else "?")
            except OSError:
                pass
    return out


def usb_reconnect_failed(timed_out: bool, state_after: str) -> bool:
    return timed_out or state_after != "device"


def check_usb(run) -> Result:
    cid = "hardware.usb"
    dev = run.dev
    if usb_dev_dir(dev.serial) is None:
        return Result(cid, UNVERIFIED, "phone not found by serial under /sys/bus/usb/devices")
    token = secrets.token_hex(6)
    marker = f"/sdcard/Download/river-qa-{token}.txt"
    run.journal.record(RES, "marker_file", marker, None, run_id=run.run_id)
    dev.sh(f"echo river-qa {token} > {marker}")
    dev.sh(f"content call --uri content://media/external/file --method scan_file --arg {marker}")
    rc, out, err = dev.sh_rc("svc usb getFunctions")
    saved = (out + err).strip()
    run.journal.record(PHONE, "usb_functions", "usb", saved, attempt_id=run.attempt_id(), run_id=run.run_id)
    # Watchdog: restores the functions after 180 s even if adb never comes back.
    wd = dev.sh(f"setsid nohup sh -c 'sleep 180; svc usb setFunctions {saved}' </dev/null >/dev/null 2>&1 & echo $!")
    pid = wd.strip().split()[-1] if wd.strip() else ""
    st = dev.boot_status()
    stat = dev.sh(f"cat /proc/{pid}/stat 2>/dev/null") if pid.isdigit() else ""
    if not stat:
        restore_now(run, RES, "marker_file", marker)
        return Result(cid, UNVERIFIED, "watchdog did not start")
    start = stat.rsplit(")", 1)[1].split()[19]
    run.journal.record(RES, "watchdog", pid, None, run_id=run.run_id, extra={"boot_id": st.boot_id, "start": start})
    listed, how, mtp_error = False, "", None
    try:
        try:
            dev.adb("shell", "svc usb setFunctions mtp", timeout=20)
        except TimeoutError:
            pass   # the switch drops the connection the command came in on
        time.sleep(3)
        reconnect_timeout = False
        try:
            dev.wait_for_device(60)
        except TimeoutError:
            reconnect_timeout = True
        if usb_reconnect_failed(reconnect_timeout, dev.state()):
            # Caught here, not raised: losing adb is a FAIL, and the restore
            # below decides whether the run must halt (A10). A timeout stays
            # a FAIL even if adb answers the very next probe (review round 2).
            mtp_error = "adb did not come back within 60 s after switching to MTP"
        elif host_run(["pgrep", "-x", "gvfsd"], timeout=10).returncode == 0:
            listed, how = gio_list(run, dev.serial, token)
        else:
            serials = mtp_interface_count()
            if serials != [dev.serial]:
                how = f"no gvfs, and MTP devices are {serials}: cannot scope mtp-files to this phone"
            else:
                r = host_run(["mtp-files"], timeout=60)
                listed = f"river-qa-{token}.txt" in r.stdout
                how = "mtp-files"
    finally:
        # The recovery clock starts at the restore command, so the wait
        # inside restore_now counts toward the 30 s (A10).
        back = time.monotonic()
        ok_fn, fmsg = restore_now(run, PHONE, "usb_functions", "usb")
        adb_back = False
        while time.monotonic() - back < 240:
            if dev.state() == "device":
                adb_back = True
                break
            time.sleep(1)
        if not adb_back:
            run.halted = "phone unreachable over adb after the USB switch; pipeline stops, tell Zak"
        adb_s = time.monotonic() - back
        ok_wd, _ = restore_now(run, RES, "watchdog", pid)
        ok_mk, _ = restore_now(run, RES, "marker_file", marker)
        for e in [e for e in run.journal.entries if e["kind"] == "gvfs_mount"]:
            restore_now(run, HOST, "gvfs_mount", e["key"])
        if adb_back:
            dev.adb("reverse", f"tcp:{server_cfg(run)['local_port']}", f"tcp:{server_cfg(run)['local_port']}",
                    timeout=20)
    d = {"how": how, "functions_restored": fmsg, "adb_back_s": round(adb_s, 1), "watchdog_killed": ok_wd,
         "marker_removed": ok_mk, "mtp_error": mtp_error}
    if run.halted:
        return Result(cid, FAIL, run.halted, d)
    if mtp_error:
        return Result(cid, FAIL, f"{mtp_error}; adb back {adb_s:.0f} s after the restore", d)
    if how.startswith("no gvfs, and"):
        return Result(cid, UNVERIFIED, how, d)
    ok = listed and adb_s <= 30 and ok_fn and ok_wd and ok_mk
    return Result(cid, PASS if ok else FAIL, f"marker {'listed' if listed else 'NOT listed'} over MTP ({how}); "
                  f"adb back in {adb_s:.0f} s", d)


def gio_list(run, serial: str, token: str) -> tuple[bool, str]:
    """List the marker through gvfs at the mtp:// URI that contains this
    serial. The mount is journalled and unmounted after the USB functions are
    restored (gvfs takes about 30 s to unmount)."""
    end = time.monotonic() + 10
    uri = None
    while time.monotonic() < end and uri is None:
        out = host_run(["gio", "mount", "-li"], timeout=20).stdout
        for m in re.finditer(r"(mtp://[^\s]*" + re.escape(serial) + r"[^\s]*)", out):
            uri = m.group(1).rstrip("/") + "/"
        if uri is None:
            time.sleep(1)
    if uri is None:
        return False, "gvfs shows no mtp:// volume with this serial"
    run.journal.record(HOST, "gvfs_mount", uri, None, run_id=run.run_id)
    mounted = host_run(["gio", "mount", uri], timeout=60)
    end = time.monotonic() + 10
    while time.monotonic() < end:
        top = host_run(["gio", "list", uri], timeout=20)
        if top.returncode == 0 and top.stdout.strip():
            for store in top.stdout.split("\n"):
                if not store.strip():
                    continue
                r = host_run(["gio", "list", uri + store.strip() + "/Download/"], timeout=30)
                if f"river-qa-{token}.txt" in r.stdout:
                    return True, f"gio {uri}{store.strip()}/Download"
        time.sleep(1)
    return False, f"gio {uri}: marker not listed (mount rc {mounted.returncode})"


# =========================================================================== system

def check_gestures(run) -> Result:
    cid = "system.gestures"
    dev = run.dev
    if control_active() == "nav-mode-0":
        put_setting(run, "secure", "navigation_mode", 0)
        if dev.sh("settings get secure navigation_mode").strip() == "0":
            injected(run, "nav-mode-0")
    sysui = dev.sh("dumpsys activity service com.android.systemui/.SystemUIService", timeout=60)
    nav = dev.sh("settings get secure navigation_mode").strip()
    home = dev.sh("cmd package resolve-activity --brief -a android.intent.action.MAIN "
                  "-c android.intent.category.HOME").strip().splitlines()[-1:]
    proxy = re.search(r"OverviewProxyService state:.*?(?=\n\S|\Z)", sysui, re.S)
    proxy_txt = proxy.group(0) if proxy else ""
    bound = overview_proxy_bound(proxy_txt)
    d = {"navigation_mode": nav, "home": home, "overview_proxy": proxy_txt[:600]}
    problems = []
    if nav != "2":
        problems.append(f"navigation_mode {nav}, not gesture (2)")
    if not bound:
        problems.append("overview proxy not connected to app.zak.perch (isConnected/mBound)")
    if not home or not home[0].startswith("app.zak.perch/"):
        problems.append(f"HOME resolves to {home}")
    w, h = screen_size(dev)
    helper.wake(dev)
    dev.sh("input keyevent KEYCODE_HOME")
    time.sleep(1.5)
    t1 = top_activity(dev)
    d["after_home"] = t1
    if not t1.startswith("app.zak.perch/") or "MainActivity" not in t1:
        problems.append(f"after HOME the top is {t1}")
    dev.sh("am start -W -n com.android.deskclock/.DeskClock")
    time.sleep(1.5)
    t2 = top_activity(dev)
    d["deskclock"] = t2
    sx, sy0, sy1 = param(run, "swipe_up", [w // 2, h - 3, int(h * 0.45)])
    dev.sh(f"input swipe {sx} {sy0} {sx} {sy1} 250")
    time.sleep(2)
    t3 = top_activity(dev)
    d["after_swipe_up"] = t3
    if not t2.startswith("com.android.deskclock"):
        problems.append(f"DeskClock did not come to the front ({t2})")
    if not t3.startswith("app.zak.perch/"):
        problems.append(f"after the swipe up the top is {t3}")
    dev.sh("am force-stop com.android.deskclock")
    seen = {}

    def on_stage(stage):
        # The helper's activities live only while its instrumentation runs.
        seen["before"] = top_activity(dev)
        bx0, by, bx1 = param(run, "back_swipe", [2, h // 2, int(w * 0.45)])
        dev.sh(f"input swipe {bx0} {by} {bx1} {by} 200")
        time.sleep(1.5)
        seen["after"] = top_activity(dev)
        return None
    res = helper.instrument(run, "back", {}, timeout=40, on_stage=on_stage, need_foreground=False)
    if control_active() == "nav-mode-0":
        restore_now(run, PHONE, "setting", "secure/navigation_mode")
    early = helper_result(cid, res)
    if early:
        dev.sh("input keyevent KEYCODE_HOME")
        return early
    tb, ta = seen.get("before", ""), seen.get("after", "")
    d["back"] = {"before": tb, "after": ta, "helper": res.values}
    if not tb.endswith(".ChildActivity"):
        problems.append(f"child B not on top before Back ({tb})")
    if ta != f"{helper.PKG}/.MainActivity":
        problems.append(f"after Back the top is {ta}, not A")
    dev.sh("input keyevent KEYCODE_HOME")
    return Result(cid, FAIL if problems else PASS, "; ".join(problems) or
                  "Perch is home and bound; HOME and swipe up go to Perch; Back returns to A", d,
                  helper_terminal=True)


PERCH = "app.zak.perch"


def overview_proxy_bound(proxy_txt: str) -> bool:
    """SystemUI's overview proxy is really connected to Perch (A8, review
    round 2): the dump names Perch as the recents component (or in the
    quickstep intent) and says isConnected=true and mBound=true. mIsEnabled
    alone doesn't count: SystemUI derives it from resolving the service,
    not from a connection (OverviewProxyService.updateEnabledState)."""
    fields = dict(re.findall(r"^\s*(\w+)=(.*?)\s*$", proxy_txt, re.M))
    named = PERCH in fields.get("mRecentsComponentName", "") or PERCH in fields.get("mQuickStepIntent", "")
    return named and fields.get("isConnected") == "true" and fields.get("mBound") == "true"


def screen_size(dev) -> tuple[int, int]:
    m = re.search(r"(\d+)x(\d+)", dev.sh("wm size").splitlines()[-1] if dev.sh("wm size") else "")
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 2270)


def alarm_firing(dump: str, label: str) -> bool:
    """DeskClock's firing notification with fullScreenIntent and this label."""
    for block in re.split(r"\n\s*NotificationRecord\(", dump):
        if "pkg=com.android.deskclock" not in block:
            continue
        chan = re.search(r"channel=([^\s,]+)|mChannel=.*?mId='([^']+)'", block)
        chan_id = (chan.group(1) or chan.group(2)) if chan else ""
        fsi = re.search(r"fullscreenintent=PendingIntent", block, re.I)
        if fsi and label in block and ("firing" in chan_id.lower() or "Firing" in block):
            return True
    return False


NOTIF_HEADER = "Current Notification Manager state"
ALARM_ABSENT_FROM_S = -16.0   # an absence sample may start no earlier than this...
ALARM_ABSENT_BY_S = -10.0     # ...and must have finished by this (review round 2)
ALARM_FIRED_BY_S = 10.0


def parse_alarm_sample(rc: int, out: str, label: str) -> tuple[float, float, bool | None] | None:
    """One sample: (phone time before dumpsys, phone time after it, firing?).
    firing is None when the dump itself failed or doesn't look like a
    notification dump: that sample is no evidence either way. None if the
    phone times are unreadable. Pure."""
    lines = out.replace("\r", "").rstrip("\n").split("\n")
    if rc != 0 or len(lines) < 3:
        return None
    try:
        t0, t1 = float(lines[0]), float(lines[-1])
    except ValueError:
        return None
    status, dump = lines[-2], "\n".join(lines[1:-2])
    if status != "__DUMP_RC__0" or NOTIF_HEADER not in dump:
        return t0, t1, None
    return t0, t1, alarm_firing(dump, label)


def alarm_observe(dev, label: str) -> tuple[float, float, bool | None] | None:
    """One observation, timed on the phone, with dumpsys's own exit status
    (a trailing date can't hide a failed dump)."""
    r = dev.read("date +%s.%N; dumpsys notification --noredact; echo __DUMP_RC__$?; date +%s.%N", timeout=30)
    return parse_alarm_sample(r.rc, r.out, label)


def alarm_verdict(polls: list[tuple[float, float, bool | None]], target_s: float) -> tuple[str, str]:
    """Judge firing evidence by phone time (A9, review round 2). Samples
    whose dump failed (firing None) are no evidence either way. Absence
    needs a successful sample that started at target - 16 s or later and
    finished by target - 10 s, and the last such sample must show no firing.
    Firing counts only from a successful sample that finished by
    target + 10 s. Firing seen in anything that finished before target - 10 s
    means it fired early."""
    rel = [(a - target_s, b - target_s, f) for a, b, f in polls if f is not None]
    early = [r for r in rel if r[2] and r[1] < ALARM_ABSENT_BY_S]
    if early:
        return FAIL, f"firing evidence already at {early[0][1]:+.1f} s"
    window = [r for r in rel if r[0] >= ALARM_ABSENT_FROM_S and r[1] <= ALARM_ABSENT_BY_S]
    if not window:
        return FAIL, "no successful observation inside target - 16 s .. target - 10 s"
    if window[-1][2]:
        return FAIL, "firing evidence present at target - 10 s"
    fired = [r for r in rel if r[2] and r[1] <= ALARM_FIRED_BY_S]
    if not fired:
        return FAIL, "no firing notification observed by target + 10 s"
    return PASS, f"absent at {window[-1][1]:+.1f} s, firing by {fired[0][1]:+.1f} s (phone time)"


def check_alarms(run) -> Result:
    cid = "system.alarms"
    dev = run.dev
    label = f"river-qa-{secrets.token_hex(4)}"
    run.journal.record(RES, "alarm", label, None, run_id=run.run_id)
    res = helper.instrument(run, "alarm_set", {"label": label, "lead_s": 45}, timeout=40)
    early = helper_result(cid, res)
    if early:
        return early
    # Everything is anchored on phone time: target_ms is the phone's clock,
    # and each observation is stamped by the phone before and after dumpsys.
    target = int(res.get("target_ms")) / 1000.0
    shift = 0
    if control_active() == "alarm-early":
        shift = -60
        run.control_injected = "alarm-early"
    eff = target + shift
    now0 = alarm_observe(dev, label)
    if now0 is None:
        return Result(cid, UNVERIFIED, "cannot read the phone's clock and notifications", helper_terminal=True)
    mono0, phone0 = time.monotonic(), now0[1]

    def phone_now():
        return phone0 + (time.monotonic() - mono0)
    time.sleep(2)
    screen_off(dev)
    polls = []
    while phone_now() < eff + 12:
        if phone_now() >= eff + ALARM_ABSENT_FROM_S:
            obs = alarm_observe(dev, label)
            if obs is not None:
                polls.append(obs)
            time.sleep(1.0)
        else:
            time.sleep(max(0.2, min(3.0, eff + ALARM_ABSENT_FROM_S - phone_now())))
    state, msg = alarm_verdict(polls, eff)
    activity = top_activity(dev)
    d = {"label": label, "target_ms": res.get("target_ms"), "shift_s": shift, "tz": res.get("tz"),
         "polls": [(round(a - eff, 2), round(b - eff, 2), f) for a, b, f in polls], "top_after": activity}
    helper.wake(dev)
    dis = helper.instrument(run, "alarm_dismiss", {"label": label}, timeout=30, need_foreground=False)
    time.sleep(2)
    after = alarm_observe(dev, label)
    still = after is None or after[2] is not False    # unreadable is not proof it was removed
    dev.sh("am force-stop com.android.deskclock")
    ok_clear, _ = restore_now(run, RES, "alarm", label)
    d["dismissed"] = dis.terminal and not still
    if state != PASS:
        return Result(cid, state, msg, d, helper_terminal=True)
    if still or not ok_clear:
        return Result(cid, FAIL, "alarm fired but was not removed", d, helper_terminal=True)
    return Result(cid, PASS, msg + ", then removed", d, helper_terminal=True)


def check_keyboard(run) -> Result:
    cid = "system.keyboard"
    dev = run.dev
    word = param(run, "keyboard_word", "river")
    keys = param(run, "keyboard_keys", None)
    if not keys:
        return Result(cid, UNVERIFIED, "no calibrated key coordinates (thresholds.json params.keyboard_keys)")
    seen = {}

    def on_stage(stage):
        ime = dev.sh("dumpsys input_method", timeout=30)
        seen["shown"] = "mInputShown=true" in ime
        cur = re.search(r"mCurId=(\S+)", ime)
        seen["latin"] = bool(cur and cur.group(1).startswith("com.android.inputmethod.latin/"))

        def taps():
            time.sleep(0.5)
            if control_active() == "input-text":
                dev.sh(f"input text {word}")
                injected(run, "input-text")
                return
            for ch in word:
                x, y = keys[ch]
                dev.sh(f"input tap {x} {y}")
                time.sleep(0.35)
        return taps
    res = helper.instrument(run, "keyboard", {"word": word, "timeout_s": 20}, timeout=60, on_stage=on_stage)
    early = helper_result(cid, res)
    if early:
        return early
    text = (res.get("text") or "").strip()
    log_ = res.get("ime_log") or ""
    commits = "".join(x.split(":", 1)[1] for x in log_.split(",") if x.startswith(("commit:", "compose:")))
    ime_ok = seen.get("shown") and seen.get("latin")
    ok = ime_ok and text == word and (word in commits or any(word in x for x in log_.split(",")))
    d = {"text": text, "ime_log": log_[:400], **seen}
    return Result(cid, PASS if ok else FAIL, f"LatinIME shown {seen.get('shown')}, field {text!r}, IME commits "
                  f"{'logged' if ok else 'missing'}", d, helper_terminal=True)


def check_webview(run) -> Result:
    cid = "system.webview"
    token = secrets.token_hex(10)
    q = "?nobeacon" if control_active() == "beacon-blocked" else ""
    if q:
        injected(run, "beacon-blocked")
    url = f"http://127.0.0.1:{server_cfg(run)['local_port']}/page/{token}{q}"
    res = helper.instrument(run, "webview", {"url": url, "token": token, "timeout_s": 15}, timeout=60)
    early = helper_result(cid, res)
    if early:
        return early
    beacon = run.server.find("beacon", token)
    title_ok = res.get("title") == token and int(res.get("elapsed_ms") or 99999) <= 15000
    d = {"title": res.get("title"), "elapsed_ms": res.get("elapsed_ms"), "beacons": len(beacon),
         "gets": len(run.server.find("page", token))}
    return Result(cid, PASS if title_ok and beacon else FAIL,
                  f"title {'= token' if title_ok else repr(res.get('title'))} in {res.get('elapsed_ms')} ms, "
                  f"beacon {'seen' if beacon else 'missing'}", d, helper_terminal=True)


def check_video(run) -> Result:
    cid = "system.video"
    cfg = server_cfg(run)
    clip = run.fixtures["files"]["clip"]
    samples = [0, 30, 60, 90, 120, 149]
    args = {"clip_url": f"http://127.0.0.1:{cfg['local_port']}/clip.mp4", "sample": ",".join(map(str, samples)),
            "output": param(run, "video_output", "buffer")}
    if control_active() == "software-decoder":
        args["force_decoder"] = "c2.android.avc.decoder"
    res = helper.instrument(run, "video", args, timeout=120)
    if args.get("force_decoder") and res.get("decoder") == args["force_decoder"]:
        injected(run, "software-decoder")   # the helper really used the forced decoder
    early = helper_result(cid, res)
    if early:
        return early
    psnrs = {}
    for i in samples:
        got = run.upload_dir / f"frame-{i}.gray"
        ref = run.fixtures_dir / f"testsrc2-frame-{i}.gray"
        psnrs[i] = round(analysis.psnr(got.read_bytes(), ref.read_bytes()), 1) if got.exists() else 0.0
    need = th(run, "video_psnr_db", 30.0)
    hw = res.get("hardware") == "true"
    count_ok = int(res.get("rendered") or 0) == clip["frames"] and int(res.get("received") or 0) == clip["frames"]
    ok = hw and count_ok and min(psnrs.values()) >= need
    d = {"decoder": res.get("decoder"), "hardware": hw, "rendered": res.get("rendered"),
         "received": res.get("received"), "psnr": psnrs}
    return Result(cid, PASS if ok else FAIL, f"{res.get('decoder')} ({'hardware' if hw else 'software'}), "
                  f"{res.get('received')}/{clip['frames']} frames, min PSNR {min(psnrs.values())} dB (need {need})",
                  d, helper_terminal=True)


ELAPSED_RE = re.compile(r"(?:mElapsedRealtimeMillis|elapsedRealtimeMillis)=(\d+)")


def network_suggestion_section(dump: str) -> str:
    """Only the network suggestion history of `dumpsys time_detector`: from
    its header to the next line indented no deeper (A12). Other sections
    (telephony, GNSS, external) carry elapsed timestamps too."""
    lines = dump.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)Network suggestion history:", line) or \
            re.match(r"^(\s*)\S.*Network suggestion", line)
        if m:
            indent = len(m.group(1))
            out = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                out.append(nxt)
            return "\n".join(out)
    return ""


def network_suggestion_newer(dump: str, e0_ms: float) -> bool:
    return any(int(v) >= e0_ms for v in ELAPSED_RE.findall(network_suggestion_section(dump)))


def check_time(run) -> Result:
    cid = "system.time"
    dev = run.dev
    put_setting(run, "global", "auto_time", 1)
    conn = dev.sh("dumpsys connectivity", timeout=60)
    m = re.search(r"Active default network: (\d+)", conn)
    wifi_default = False
    if m:
        na = re.search(r"NetworkAgentInfo\{[^\n]*?network\{" + m.group(1) + r"\}[^\n]*", conn)
        wifi_default = bool(na and "WIFI" in na.group(0))
    e0 = float(dev.sh("cat /proc/uptime").split()[0]) * 1000
    out = dev.sh("cmd network_time_update_service force_refresh", timeout=60).strip()
    if control_active() == "bad-ntp" and out == "true":
        run.journal.record(PHONE, "ntp", "ntp", None, attempt_id=run.attempt_id(), run_id=run.run_id)
        dev.sh("cmd network_time_update_service set_server_config_for_tests --server ntp://192.0.2.1 "
               "--timeout_millis 2000")
        injected(run, "bad-ntp")   # set after a successful sync
        e0 = float(dev.sh("cat /proc/uptime").split()[0]) * 1000
        out = dev.sh("cmd network_time_update_service force_refresh", timeout=60).strip()
        restore_now(run, PHONE, "ntp", "ntp")
    newer = False
    end = time.monotonic() + 5
    td = ""
    while time.monotonic() < end and not newer:
        td = dev.sh("dumpsys time_detector", timeout=30)
        newer = network_suggestion_newer(td, e0)
        time.sleep(0.5)
    t_host0 = time.time()
    ph = dev.sh("date +%s.%N").strip()
    t_host1 = time.time()
    try:
        skew = float(ph) - (t_host0 + t_host1) / 2
    except ValueError:
        skew = 999.0
    restore_now(run, PHONE, "setting", "global/auto_time")
    d = {"force_refresh": out, "wifi_default": wifi_default, "suggestion_newer": newer, "skew_s": round(skew, 2)}
    ok = out == "true" and wifi_default and newer and abs(skew) <= 2
    return Result(cid, PASS if ok else FAIL, f"force_refresh {out}, Wi-Fi default {wifi_default}, network "
                  f"suggestion after E0 {newer}, clock skew {skew:+.2f} s", d)


# =========================================================================== apps

def install_app(run, key) -> tuple[bool, str]:
    f = run.fixtures["files"][key]
    run.journal.record(RES, "package", f["package"], None, run_id=run.run_id)
    r = run.dev.adb("install", "-r", "-g", str(run.fixtures_dir / f["file"]), timeout=300)
    ok = r.returncode == 0 and "Success" in r.stdout and f["package"] in run.dev.sh(f"pm path {f['package']}")
    run.sideload[key] = {"install": ok}
    return ok, (r.stdout + r.stderr).strip()[-200:]


def app_crashed(run, pkg, since_tags) -> list[str]:
    from . import bootsig
    tags = bootsig.dropbox_tags(run.dev.sh("dumpsys dropbox", timeout=60))
    pool = list(tags)
    for t in since_tags:
        if t in pool:
            pool.remove(t)
    crash = run.dev.sh("logcat -b crash -d", timeout=60)
    hits = [t for t in pool if t in ("data_app_crash", "data_app_anr", "data_app_native_crash")]
    if re.search(r"Process: " + re.escape(pkg) + r"\b", crash) or re.search(r">>> " + re.escape(pkg), crash):
        hits.append("logcat crash")
    return hits


def vpn_launch_ok(top_after: str, package: str) -> bool:
    """Launched only if the app's own activity is on top (after any VPN
    dialog was dismissed). A VPN dialog on top doesn't count."""
    return top_after.startswith(package + "/")


def launch_and_watch(run, cid, key, start_cmd, accept_vpn_dialog=False) -> Result:
    from . import bootsig
    dev = run.dev
    f = run.fixtures["files"][key]
    ok, msg = install_app(run, key)
    if not ok:
        return Result(cid, FAIL, f"install failed: {msg}")
    tags0 = bootsig.dropbox_tags(dev.sh("dumpsys dropbox", timeout=60))
    helper.wake(dev)
    dev.sh(start_cmd)
    time.sleep(4)
    top = top_activity(dev)
    d = {"top": top}
    if accept_vpn_dialog and top.startswith("com.android.vpndialogs"):
        # The VPN consent dialog is the system's, not the app's: the app
        # counts as launched only if its own activity is resumed once the
        # dialog is dismissed (never accepted). Review round 2.
        d["vpn_dialog"] = "shown; dismissed, never accepted"
        dev.sh("input keyevent KEYCODE_BACK")
        time.sleep(2)
        top = top_activity(dev)
        d["top_after_dialog"] = top
    opened = vpn_launch_ok(top, f["package"])
    resumed = opened
    run.sideload[key]["launch"] = opened
    time.sleep(10)
    crashed = app_crashed(run, f["package"], tags0)
    d["crashes"] = crashed
    dev.sh(f"am force-stop {f['package']}; input keyevent KEYCODE_HOME")
    if not resumed:
        return Result(cid, FAIL, f"not in front after launch (top {top})", d)
    if crashed:
        return Result(cid, FAIL, f"crash or ANR within 10 s: {crashed}", d)
    return Result(cid, PASS, f"{f['package']} {f['version']} opened ({top}), no crash or ANR for 10 s", d)


def check_tailscale(run):
    f = run.fixtures["files"]["tailscale"]
    return launch_and_watch(run, "apps.tailscale", "tailscale", f"am start -n {f['activity']}", True)


def check_t3code(run):
    f = run.fixtures["files"]["t3code"]
    return launch_and_watch(run, "apps.t3code", "t3code", f"am start -n {f['activity']}")


def ui_dump(dev) -> str:
    for _ in range(3):
        dev.sh("uiautomator dump /sdcard/river-qa-ui.xml", timeout=30)
        x = dev.sh("cat /sdcard/river-qa-ui.xml")
        if "<hierarchy" in x:
            dev.sh("rm -f /sdcard/river-qa-ui.xml")
            return x
        time.sleep(1)
    return ""


def rendered_token(token: str) -> str:
    """What the test page's script writes into #r: never in the URL or the
    server-rendered HTML, so it shows that Brave ran the page script."""
    return "js-" + token[::-1]


def brave_rendered(nodes, token: str) -> bool:
    """The page script's own output is on screen (A13). The URL bar shows the
    token, so the token itself proves nothing."""
    want = rendered_token(token)
    return any(want in (n.get("text", "") + " " + n.get("content-desc", "")) for n in nodes)


def check_brave(run) -> Result:
    cid = "apps.brave"
    dev = run.dev
    f = run.fixtures["files"]["brave"]
    ok, msg = install_app(run, "brave")
    if not ok:
        return Result(cid, FAIL, f"install failed: {msg}")
    token = secrets.token_hex(10)
    url = f"http://127.0.0.1:{server_cfg(run)['local_port']}/page/{token}"
    helper.wake(dev)
    dev.sh(f"am start -a android.intent.action.VIEW -d {url} -p {f['package']}")
    # The first-run steps recorded for the pinned version (thresholds.json),
    # then generic fallbacks. Brave 1.100.58: Maybe later, Continue, Maybe
    # later, Continue, Start browsing; the last opens the intent's URL.
    buttons = list(param(run, "brave_firstrun_taps", [])) + [
        "Start browsing", "Not now", "Skip", "No thanks", "Continue", "Got it", "Maybe later", "Close", "Done"]
    steps, seen_token, seen_app = [], False, False
    end = time.monotonic() + 120
    reopened = False
    while time.monotonic() < end:
        time.sleep(2)
        x = ui_dump(dev)
        if not x:
            continue
        root = ET.fromstring(x)
        nodes = list(root.iter("node"))
        if any(n.get("package") == f["package"] for n in nodes):
            seen_app = True
        if brave_rendered(nodes, token) and top_activity(dev).startswith(f["package"]):
            seen_token = True
            break
        tapped = False
        for b in buttons:
            for n in nodes:
                if n.get("text", "").strip().lower() == b.lower() and n.get("clickable", "true") != "false":
                    xs = list(map(int, re.findall(r"\d+", n.get("bounds"))))
                    dev.sh(f"input tap {(xs[0] + xs[2]) // 2} {(xs[1] + xs[3]) // 2}")
                    steps.append(b)
                    tapped = True
                    break
            if tapped:
                break
        if not tapped and not reopened and len(steps) > 0:
            # First-run done but the page was not opened: send the intent again.
            dev.sh(f"am start -a android.intent.action.VIEW -d {url} -p {f['package']}")
            reopened = True
    run.sideload["brave"]["launch"] = seen_app   # only if Brave's window was actually seen (A11)
    gets = run.server.find("page", token)
    beacons = run.server.find("beacon", token)
    top = top_activity(dev)
    dev.sh(f"am force-stop {f['package']}; input keyevent KEYCODE_HOME")
    d = {"first_run_taps": steps, "gets": len(gets), "beacons": len(beacons), "script_text_on_screen": seen_token,
         "top": top, "ua": gets[0]["ua"] if gets else None}
    ok = seen_token and gets and beacons
    return Result(cid, PASS if ok else FAIL, f"page GET {'seen' if gets else 'missing'}, beacon "
                  f"{'seen' if beacons else 'missing'}, page-script text on screen {seen_token} (first-run taps: "
                  f"{', '.join(steps) or 'none'})", d)


# =========================================================================== orchestration

FEATURES = [
    ("radios.wifi", check_wifi), ("radios.bluetooth", check_bluetooth), ("radios.nfc", check_nfc),
    ("radios.gps", check_gps),
    ("hardware.cameras", check_cameras), ("hardware.audio", check_audio), ("hardware.fingerprint", check_fingerprint),
    ("hardware.sensors", check_sensors), ("hardware.vibration", check_vibration), ("hardware.screen", check_screen),
    ("hardware.battery", check_battery), ("hardware.usb", check_usb),
    ("system.gestures", check_gestures), ("system.alarms", check_alarms), ("system.keyboard", check_keyboard),
    ("system.webview", check_webview), ("system.video", check_video), ("system.time", check_time),
    ("apps.brave", check_brave), ("apps.tailscale", check_tailscale), ("apps.t3code", check_t3code),
]


def start_server(run) -> None:
    from .server import TestServer
    cfg = server_cfg(run)
    run.upload_token = secrets.token_hex(8)
    run.upload_dir = run.dir / "uploads"
    lan = cfg.get("lan_addr")
    have = host_run(["ip", "-4", "-o", "addr"], timeout=10).stdout
    run.server = TestServer(run.fixtures_dir / "page.html", run.fixtures_dir / run.fixtures["files"]["clip"]["file"],
                            run.upload_dir, run.dir / "requests.jsonl", int(cfg["local_port"]),
                            lan if lan and f" {lan}/" in have else None, int(cfg["lan_port"]), run.upload_token)
    if lan and f" {lan}/" not in have:
        run.server.lan_error = f"forge does not have {lan}"
    run.journal.record(HOST, "adb_reverse", str(cfg["local_port"]), None, run_id=run.run_id)
    run.dev.adb("reverse", f"tcp:{cfg['local_port']}", f"tcp:{cfg['local_port']}", timeout=20)
    run.upload_base = f"http://127.0.0.1:{cfg['local_port']}/upload/{run.upload_token}"


def run_features(run) -> None:
    from .qa import run_check
    run.sideload = {}
    start_server(run)
    ok, msg = helper.install(run)
    run.sideload["helper"] = {"install": ok}
    if not ok:
        run.add(Result("system.sideload", FAIL, f"helper install failed: {msg}"))
        return
    from .qa import logcat_check, logcat_restart, logcat_since
    for cid, fn in FEATURES:
        if run.halted:
            break
        if not run.wanted(cid):
            continue
        run.guard()
        logcat_check(run, cid)
        since = logcat_since(run.dev) if cid == "hardware.usb" else None
        run_check(run, cid, fn)
        if cid == "hardware.usb":
            logcat_restart(run, since, cid)


SIDELOAD_KEYS = ("helper", "brave", "tailscale", "t3code")


def sideload_result(sl: dict) -> Result:
    """system.sideload: all four expected APKs installed, launched (not the
    helper, which is instrumented instead) and uninstalled, each verified."""
    missing = [f"{k}: {s}" for k in SIDELOAD_KEYS for s in ("install", "launch", "uninstall")
               if (k != "helper" or s != "launch") and (sl.get(k) or {}).get(s) is not True]
    return Result("system.sideload", FAIL if missing else PASS, "; ".join(missing) if missing else
                  f"{len(SIDELOAD_KEYS)} APKs installed, opened and uninstalled, each verified", sl)


def cleanup_and_final(run) -> None:
    try:
        cleanup(run)
    finally:
        if getattr(run, "server", None):
            run.server.stop()
    final_checks(run)


def cleanup(run) -> None:
    """Undo this run's journal entries in plan_recovery order (resources, then
    phone, then host; hidden_api_policy after the Bluetooth entries), apps
    first and the helper last. Every step is verified."""
    from .device import GuardError
    from .restore import restore_entry
    dev = run.dev
    notes, bad = [], []
    if dev.state() != "device":
        run.add(Result("cleanup.verified", FAIL, "phone not reachable for cleanup"))
        return
    try:
        run.guard()
    except GuardError as e:
        run.halted = run.halted or f"guard failed before cleanup: {e}"
        run.add(Result("cleanup.verified", FAIL, f"guard failed before cleanup: {e}"))
        return
    dev.root()
    mine = [e for e in run.journal.entries if e.get("run_id") == run.run_id]
    order, _ = jmod.plan_recovery(mine, jmod.current_wipe_seq(run.journal.qa_home))
    apps = [e for e in order if e["kind"] == "package" and e["key"] != helper.PKG]
    rest = [e for e in order if e["kind"] != "package"]
    for e in apps + rest:
        ok, msg = restore_entry(run, e)
        (notes if ok else bad).append(msg)
        if e["kind"] == "package":
            for k, v in (getattr(run, "sideload", {}) or {}).items():
                if run.fixtures.get("files", {}).get(k, {}).get("package") == e["key"]:
                    v["uninstall"] = ok
    if getattr(run, "sideload", None) is not None and run.journal.find(RES, "package", helper.PKG):
        ok = helper.uninstall(run)
        run.sideload.setdefault("helper", {})["uninstall"] = ok
        (notes if ok else bad).append("helper " + ("uninstalled" if ok else "still installed"))
    dev.sh("input keyevent KEYCODE_HOME; input keyevent KEYCODE_SLEEP")
    left = [e for e in run.journal.entries if e.get("run_id") == run.run_id]
    if left:
        bad.append(f"{len(left)} journal entries not restored")
    run.add(Result("cleanup.verified", FAIL if bad else PASS, "; ".join(bad) or f"{len(notes)} steps verified",
                   {"done": notes, "failed": bad}))
    if getattr(run, "sideload", None) is not None and run.wanted("system.sideload"):
        run.add(sideload_result(run.sideload))


def final_health_parts(snap: dict, reads: dict, logcat_path: Path, logcat_dead: str | None) -> list[tuple[str, str]]:
    """Tombstones, pstore, the streamed logcat and the crash buffer, each
    against the preflight snapshot. A failed read or a dead stream is
    UNVERIFIED, never PASS. Pure apart from reading the logcat file."""
    import hashlib
    parts = []
    ok, text, why = reads["tombstones"]
    if not ok:
        parts.append((UNVERIFIED, f"/data/tombstones unreadable: {why}"))
    else:
        tomb = sorted(text.split())
        parts.append((PASS if tomb == snap["tombstones"] else FAIL,
                      "no new tombstones" if tomb == snap["tombstones"] else "new tombstones"))
    ok, data, why = reads["pstore"]
    if not ok:
        parts.append((UNVERIFIED, f"pstore unreadable: {why}"))
    else:
        same = hashlib.sha256(data).hexdigest() == snap["pstore"]
        parts.append((PASS if same else FAIL, "pstore unchanged" if same else "pstore changed"))
    lc = logcat_path.read_text(errors="replace") if logcat_path.exists() else None
    if lc is None:
        parts.append((UNVERIFIED, "no streamed logcat"))
    else:
        crashes = re.findall(r"FATAL EXCEPTION|Fatal signal \d+|>>> \S+ <<<|ANR in \S+", lc)
        if crashes:
            parts.append((FAIL, f"streamed logcat: {len(crashes)} crashes/ANRs"))
        elif logcat_dead:
            parts.append((UNVERIFIED, logcat_dead))
        else:
            parts.append((PASS, "streamed logcat: no crashes or ANRs"))
    ok, text, why = reads["crash"]
    if not ok:
        parts.append((UNVERIFIED, f"logcat -b crash unreadable at the end: {why}"))
    else:
        before = {}
        for line in (snap.get("crash") or "").splitlines():
            before[line] = before.get(line, 0) + 1
        new = []
        for line in text.splitlines():
            if before.get(line):
                before[line] -= 1
            else:
                new.append(line)
        from .evidence import crash_processes
        procs = crash_processes("\n".join(new))
        parts.append((FAIL if procs else PASS, ("crash buffer: new crashes " + ", ".join(procs)) if procs
                      else "crash buffer: nothing new since preflight"))
    return parts


def final_checks(run) -> None:
    from .qa import check_dropbox, check_services, diagnostic_reads, hal_deaths, logcat_check, proc_info, stop_logcat
    dev = run.dev
    snap = run.snapshot
    from .device import GuardError
    logcat_check(run, "the final checks")
    stop_logcat(run)
    if not snap or dev.state() != "device":
        for cid in ("final.health", "final.processes", "final.hal_deaths", "final.boot_id", "final.settings"):
            run.add(Result(cid, UNVERIFIED, "no preflight snapshot or phone unreachable"))
        return
    try:
        run.guard()   # C6
    except GuardError as e:
        run.halted = run.halted or f"guard failed before the final checks: {e}"
        for cid in ("final.health", "final.processes", "final.hal_deaths", "final.boot_id", "final.settings"):
            run.add(Result(cid, FAIL, f"guard failed before the final checks: {e}"))
        return
    parts = []
    db = check_dropbox(run, since=snap["dropbox"])
    parts.append((db.state, db.summary))
    reads = diagnostic_reads(dev)
    parts += final_health_parts(snap, reads, run.dir / "logcat.txt", run.logcat_dead)
    sv = check_services(run)
    parts.append((sv.state, sv.summary))
    if getattr(run, "bootcount_expected", None) is not None:
        from .qa import check_boot_count_now
        parts.append(check_boot_count_now(run))
    run.add(Result("final.health", worst([p[0] for p in parts]), "; ".join(p[1] for p in parts),
                   {"logcat_restarts": run.logcat_restarts}))
    now = proc_info(dev)
    changed = [k for k, v in snap["procs"].items() if not v or not now.get(k) or now[k]["pid"] != v["pid"]
               or now[k]["start"] != v["start"]]
    run.add(Result("final.processes", FAIL if changed else PASS,
                   ("restarted or gone: " + ", ".join(changed)) if changed else "all watched processes unchanged",
                   {"now": now}))
    deaths = hal_deaths(dev)
    run.add(Result("final.hal_deaths", PASS if deaths == "0" else (UNVERIFIED if deaths is None else FAIL),
                   f"HAL deaths since last reboot: {deaths}"))
    bid = dev.boot_id()
    want = snap["boot_id"]
    if control_active() == "boot-id-changed":
        want = "00000000-control"
        injected(run, "boot-id-changed")
    run.add(Result("final.boot_id", PASS if bid == want else FAIL,
                   "boot_id unchanged" if bid == want else f"boot_id changed: {want[:8]} -> {bid[:8]}"))
    s = {"navigation_mode": dev.sh("settings get secure navigation_mode").strip(),
         "location": dev.sh("cmd location is-location-enabled").strip(),
         "extra_dim": dev.sh("settings get secure reduce_bright_colors_activated").strip(),
         "sound_effects": dev.sh("settings get system sound_effects_enabled").strip(),
         "lock_disabled": dev.sh("cmd lock_settings get-disabled").strip()}
    want_s = {"navigation_mode": "2", "location": "false", "extra_dim": "1", "sound_effects": "0",
              "lock_disabled": "true"}
    diff = {k: s[k] for k in want_s if s[k] != want_s[k]}
    run.add(Result("final.settings", FAIL if diff else PASS,
                   f"differs from phone-setup: {diff}" if diff else "matches phone-setup's choices", s))
