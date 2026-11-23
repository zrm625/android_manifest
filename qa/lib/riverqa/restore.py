"""Restore and clean up journal entries. Each handler returns (ok, message)
and verifies what it did."""

from __future__ import annotations

import shlex
import time

from . import journal as jmod
from .core import run as host_run


def _setting(run, e):
    ns, name = e["key"].split("/", 1)
    dev = run.dev
    if e["original"] is None:
        dev.sh(f"settings delete {ns} {shlex.quote(name)}")
    else:
        dev.sh(f"settings put {ns} {shlex.quote(name)} {shlex.quote(str(e['original']))}")
    now = dev.sh(f"settings get {ns} {shlex.quote(name)}").strip()
    want = "null" if e["original"] is None else str(e["original"])
    return now == want, f"{e['key']}={now}"


def _location(run, e):
    run.dev.sh(f"cmd location set-location-enabled {e['original']}")
    now = run.dev.sh("cmd location is-location-enabled").strip()
    return now == e["original"], f"location {now}"


def _bt_enabled(run, e):
    dev = run.dev
    if e["original"] == "true":
        dev.sh("cmd bluetooth_manager enable; cmd bluetooth_manager wait-for-state:STATE_ON", timeout=30)
    else:
        dev.sh("cmd bluetooth_manager disable; cmd bluetooth_manager wait-for-state:STATE_OFF", timeout=30)
    on = "enabled: true" in dev.sh("dumpsys bluetooth_manager")
    return on == (e["original"] == "true"), f"bluetooth {'on' if on else 'off'}"


def _needs_helper(run):
    from . import helper
    if helper.PKG in run.dev.sh(f"pm list packages {helper.PKG}"):
        return True
    if not getattr(run, "helper_apk", None):
        from .helperapk import ensure_helper
        run.helper_apk, _ = ensure_helper()
    ok, _ = helper.install(run)
    return ok


HIDDEN_API_KEY = "global/hidden_api_policy"


class relaxed_hidden_api:
    """Make sure hidden_api_policy is relaxed while a handler calls hidden
    APIs through the helper (C4). If this run's journal already holds the
    policy's original, that entry restores it later (plan_recovery orders it
    after the Bluetooth entries). Otherwise the original is journalled here,
    so a crash leaves it to recovery, and restored on the way out."""

    def __init__(self, run):
        self.run, self.own = run, None

    def __enter__(self):
        dev = self.run.dev
        cur = dev.sh("settings get global hidden_api_policy").strip()
        if cur != "1":
            existing = self.run.journal.find(jmod.PHONE, "setting", HIDDEN_API_KEY)
            e = self.run.journal.record(jmod.PHONE, "setting", HIDDEN_API_KEY, None if cur == "null" else cur,
                                        attempt_id=getattr(self.run, "attempt_id", lambda: None)(),
                                        run_id=getattr(self.run, "run_id", None))
            self.own = None if existing else e
            dev.sh("settings put global hidden_api_policy 1")
        return self

    def __exit__(self, *exc):
        if self.own is not None:
            ok, _ = _setting(self.run, self.own)
            if ok:
                self.run.journal.resolve(self.own)


def _bt_scan_mode(run, e):
    from . import helper
    if not _needs_helper(run):
        return False, "helper not installable"
    run.dev.sh("cmd bluetooth_manager enable; cmd bluetooth_manager wait-for-state:STATE_ON", timeout=30)
    with relaxed_hidden_api(run):
        r = helper.instrument(run, "bt_set_scan_mode", {"mode": e["original"]}, timeout=40, need_foreground=False)
    return r.get("scan_mode") == str(e["original"]), f"scan mode {r.get('scan_mode')}"


def _usb_functions(run, e):
    orig = e["original"] or ""
    run.dev.sh(f"svc usb setFunctions {orig}".strip(), timeout=30)
    time.sleep(3)
    run.dev.wait_for_device(60)
    rc, out, err = run.dev.sh_rc("svc usb getFunctions")
    now = (out + err).strip()
    return now == orig.strip(), f"usb functions {now!r}"


def _user_rotation(run, e):
    run.dev.sh(f"cmd window user-rotation {e['original']}")
    now = run.dev.sh("cmd window user-rotation").strip()
    return now == e["original"], f"user-rotation {now}"


def _volume(run, e):
    run.dev.sh(f"cmd media_session volume --stream 3 --set {int(e['original'])}")
    out = run.dev.sh("cmd media_session volume --stream 3 --get")
    return f"volume is {int(e['original'])} " in out, out.strip().splitlines()[-1] if out.strip() else "?"


BAD_NTP_HOST = "192.0.2.1"   # the bad-ntp control's server (TEST-NET-1)


def _ntp(run, e):
    """Reset the test server config, then verify the dump no longer names
    the control's server."""
    rc, out, err = run.dev.sh_rc("cmd network_time_update_service reset_server_config_for_tests")
    r = run.dev.read("dumpsys network_time_update_service")
    ok = rc == 0 and r.ok and bool(r.out.strip()) and BAD_NTP_HOST not in r.out
    return ok, f"NTP server config {'reset' if ok else 'NOT verified reset'} (exit {rc}, dump exit {r.rc})"


def _wifi(run, e):
    run.dev.sh(f"cmd wifi set-wifi-enabled {e['original']}")
    time.sleep(5)
    on = "Wifi is enabled" in run.dev.sh("cmd wifi status")
    return on == (e["original"] == "enabled"), f"wifi {'enabled' if on else 'disabled'}"


def _sysfs(run, e):
    run.dev.sh(f"echo {shlex.quote(str(e['original']))} > {shlex.quote(e['key'])}")
    now = run.dev.sh(f"cat {shlex.quote(e['key'])}").strip()
    return now == str(e["original"]), f"{e['key']}={now}"


def _package(run, e):
    if e["key"] == "river.qa":
        return True, "helper is uninstalled last, by cleanup"
    run.dev.sh(f"pm uninstall {e['key']}", timeout=60)
    gone = e["key"] not in run.dev.sh(f"pm list packages {e['key']}")
    return gone, f"{e['key']} {'uninstalled' if gone else 'still installed'}"


def _alarm(run, e):
    # A dismissed alarm stays in DeskClock's database; the test phone has no
    # alarms of its own, so clear DeskClock. Verified: pm clear printed
    # Success and dumpsys alarm lists nothing for DeskClock afterwards.
    run.dev.sh("am force-stop com.android.deskclock")
    rc, out, _ = run.dev.sh_rc("pm clear com.android.deskclock")
    r = run.dev.read("dumpsys alarm")
    left = r.out.count("com.android.deskclock") if r.ok else None
    ok = rc == 0 and "Success" in out and left == 0
    return ok, f"DeskClock {'cleared' if ok else 'NOT cleared'} (pm clear: {out.strip() or rc}, alarm entries {left})"


def _marker(run, e):
    run.dev.sh(f"rm -f {shlex.quote(e['key'])}")
    gone = run.dev.sh(f"[ -e {shlex.quote(e['key'])} ] && echo yes || echo no").strip() == "no"
    return gone, f"{e['key']} {'removed' if gone else 'still there'}"


def _watchdog(run, e):
    pid, ex = e["key"], e.get("extra") or {}
    st = run.dev.boot_status()
    if not st or st.boot_id != ex.get("boot_id"):
        return True, "watchdog belonged to an earlier boot"
    stat = run.dev.sh(f"cat /proc/{pid}/stat 2>/dev/null")
    if not stat:
        return True, "watchdog already gone"
    start = stat.rsplit(")", 1)[1].split()[19]
    if start != ex.get("start"):
        return True, "PID reused by another process; not killing"
    run.dev.sh(f"kill {pid}")
    time.sleep(0.5)
    alive = bool(run.dev.sh(f"cat /proc/{pid}/stat 2>/dev/null"))
    return not alive, f"watchdog {pid} {'killed' if not alive else 'still alive'}"


def _bond(run, e):
    from . import helper
    addr = e["key"]
    ok_phone = False
    if _needs_helper(run):
        run.dev.sh("cmd bluetooth_manager enable; cmd bluetooth_manager wait-for-state:STATE_ON", timeout=30)
        with relaxed_hidden_api(run):
            r = helper.instrument(run, "bt_unbond", {"addr": addr}, timeout=40, need_foreground=False)
        ok_phone = r.get("bond_state") == "10"
    phone_addr = (e.get("extra") or {}).get("phone_addr")
    ok_host = True
    if phone_addr:
        host_run(["bluetoothctl", "remove", phone_addr], timeout=20)
        info = host_run(["bluetoothctl", "info", phone_addr], timeout=20).stdout
        ok_host = "Paired: yes" not in info
    return ok_phone and ok_host, f"bond removed: phone {ok_phone}, host {ok_host}"


def _bt_discoverable(run, e):
    host_run(["bluetoothctl", "discoverable", "on" if e["original"] == "yes" else "off"], timeout=20)
    show = host_run(["bluetoothctl", "show"], timeout=20).stdout
    return f"Discoverable: {e['original']}" in show, f"host Discoverable: {e['original']}"


def _adb_reverse(run, e):
    run.dev.adb("reverse", "--remove", f"tcp:{e['key']}", timeout=20)
    left = run.dev.adb("reverse", "--list", timeout=20).stdout
    return f"tcp:{e['key']} " not in left, f"adb reverse tcp:{e['key']} removed"


def _gvfs(run, e):
    try:
        host_run(["gio", "mount", "-u", e["key"]], timeout=90)
    except TimeoutError:
        pass
    left = e["key"].rstrip("/") in host_run(["gio", "mount", "-l"], timeout=20).stdout
    return not left, f"{e['key']} {'still mounted' if left else 'unmounted'}"


HANDLERS = {
    "setting": _setting, "location": _location, "bt_enabled": _bt_enabled, "bt_scan_mode": _bt_scan_mode,
    "usb_functions": _usb_functions, "user_rotation": _user_rotation, "volume_music": _volume, "ntp": _ntp,
    "sysfs": _sysfs, "wifi": _wifi, "package": _package, "alarm": _alarm, "marker_file": _marker, "watchdog": _watchdog,
    "bt_bond": _bond, "bt_discoverable": _bt_discoverable, "adb_reverse": _adb_reverse, "gvfs_mount": _gvfs,
}


def restore_entry(run, e) -> tuple[bool, str]:
    h = HANDLERS.get(e["kind"])
    if h is None:
        return False, f"no handler for {e['kind']}"
    try:
        ok, msg = h(run, e)
    except Exception as ex:  # noqa: BLE001
        return False, f"{e['kind']} {e['key']}: {ex}"
    if ok:
        run.journal.resolve(e)
    return ok, msg


def recover_journal(run) -> tuple[list, list, list]:
    """Restore leftovers (only called after the lock is held and the guards
    confirmed the phone). Each handler verifies its restoration before the
    entry is resolved; anything left over is reported as failed, and
    preflight stops on it."""
    restore, drop = jmod.plan_recovery(run.journal.entries, jmod.current_wipe_seq(run.journal.qa_home))
    for e in drop:
        run.journal.resolve(e)
    done, failed = [], []
    for e in restore:
        if e["kind"] == "package" and e["key"] == "river.qa":
            continue  # uninstalled last
        ok, msg = restore_entry(run, e)
        (done if ok else failed).append(msg)
    helper_left = run.journal.find(jmod.RESOURCE, "package", "river.qa")
    if helper_left and not failed:
        from . import helper
        if helper.uninstall(run):
            done.append("helper uninstalled")
        else:
            failed.append("helper still installed")
    left = [e for e in run.journal.entries if e["id"] in {x["id"] for x in restore}]
    if left and not failed:
        failed.append(f"{len(left)} entries still unresolved: " + ", ".join(f"{e['kind']} {e['key']}" for e in left))
    return done, drop, failed
