"""Restore and clean up journal entries. Each handler returns (ok, message)
and verifies what it did."""

from __future__ import annotations

import re
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
    # A checked read, like every other restore (review round 3): a failed
    # `settings get` is never taken as the value.
    r = dev.read(f"settings get {ns} {shlex.quote(name)}")
    if not r.ok:
        return False, f"{e['key']}: unreadable (exit {r.rc})"
    now = r.out.strip()
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
    r = dev.read("dumpsys bluetooth_manager")
    if not r.ok or "enabled:" not in r.out:
        return False, f"bluetooth state unreadable (exit {r.rc})"
    on = "enabled: true" in r.out
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
    if rc != 0:
        return False, f"usb functions unreadable (exit {rc}: {now!r})"
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
    """Reset the test server config, then verify the dump says so directly:
    NtpTrustedTime prints mNtpConfigForTests=null once no test config is
    set (review round 2), and the control's server is gone."""
    rc, out, err = run.dev.sh_rc("cmd network_time_update_service reset_server_config_for_tests")
    r = run.dev.read("dumpsys network_time_update_service")
    cleared = r.ok and re.search(r"^\s*mNtpConfigForTests=null\s*$", r.out, re.M) is not None
    ok = rc == 0 and cleared and BAD_NTP_HOST not in r.out
    return ok, (f"NTP test config {'cleared' if ok else 'NOT verified cleared'} (exit {rc}, dump exit {r.rc}, "
                f"mNtpConfigForTests=null {'seen' if cleared else 'not seen'})")


def _wifi(run, e):
    run.dev.sh(f"cmd wifi set-wifi-enabled {e['original']}")
    time.sleep(5)
    r = run.dev.read("cmd wifi status")
    if not r.ok or not re.search(r"Wifi is (enabled|disabled)", r.out):
        return False, f"wifi state unreadable (exit {r.rc})"
    on = "Wifi is enabled" in r.out
    return on == (e["original"] == "enabled"), f"wifi {'enabled' if on else 'disabled'}"


def _sysfs(run, e):
    run.dev.sh(f"echo {shlex.quote(str(e['original']))} > {shlex.quote(e['key'])}")
    r = run.dev.read(f"cat {shlex.quote(e['key'])}")
    now = r.out.strip()
    return r.ok and now == str(e["original"]), f"{e['key']}={now if r.ok else 'unreadable'}"


def uninstall_checked(dev, pkg: str) -> tuple[bool, str]:
    """``pm uninstall`` then a checked package listing: gone only if the
    listing succeeded and has no exact ``package:<pkg>`` line. A failed
    listing is never proof the package is gone (review rounds 2 and 3)."""
    dev.sh(f"pm uninstall {pkg}", timeout=60)
    r = dev.read(f"pm list packages {pkg}", timeout=60)
    if not r.ok:
        return False, f"{pkg}: package list unreadable (exit {r.rc}), not proof it is gone"
    gone = f"package:{pkg}" not in r.out.split()
    return gone, f"{pkg} {'uninstalled' if gone else 'still installed'}"


def _package(run, e):
    if e["key"] == "river.qa":
        return True, "helper is uninstalled last, by cleanup"
    return uninstall_checked(run.dev, e["key"])


def live_alarms(dump: str) -> str | None:
    """The parts of `dumpsys alarm` that list live alarms: the pending
    alarms and the user-blocked background alarms, up to "Pending alarms per
    uid", and the past-due non-wakeup alarms, up to "Number of delayed
    alarms" (AlarmManagerService.dumpImpl; review round 3). The removal
    history further down names cleared alarms and is not included (review
    round 2). None if the dump doesn't have that shape."""
    end = dump.find("Pending alarms per uid")
    if end < 0:
        return None
    m = re.search(r"^\s*\d+ pending alarms:|^\s*Pending user blocked background alarms:", dump, re.M)
    if m is None or m.start() > end:
        return None
    pd = re.search(r"^\s*Past-due non-wakeup alarms:", dump[end:], re.M)
    if pd is None:
        return None
    pd_start = end + pd.start()
    pd_end = dump.find("Number of delayed alarms", pd_start)
    if pd_end < 0:
        return None
    return dump[m.start():end] + dump[pd_start:pd_end]


def _alarm(run, e):
    # A dismissed alarm stays in DeskClock's database; the test phone has no
    # alarms of its own, so clear DeskClock. Verified: pm clear printed
    # Success and no live alarm belongs to DeskClock afterwards.
    run.dev.sh("am force-stop com.android.deskclock")
    rc, out, _ = run.dev.sh_rc("pm clear com.android.deskclock")
    r = run.dev.read("dumpsys alarm")
    live = live_alarms(r.out) if r.ok else None
    left = live.count("com.android.deskclock") if live is not None else None
    ok = rc == 0 and "Success" in out and left == 0
    return ok, (f"DeskClock {'cleared' if ok else 'NOT cleared'} (pm clear: {out.strip() or rc}, "
                f"live alarm entries {'unreadable' if left is None else left})")


def _marker(run, e):
    run.dev.sh(f"rm -f {shlex.quote(e['key'])}")
    r = run.dev.read(f"[ -e {shlex.quote(e['key'])} ] && echo yes || echo no")
    gone = r.ok and r.out.strip() == "no"
    return gone, f"{e['key']} {'removed' if gone else 'still there or unreadable'}"


def _proc_stat(run, pid) -> str | None:
    """/proc/<pid>/stat, "" if the process is gone, None if unreadable."""
    r = run.dev.read(f"if [ -e /proc/{int(pid)} ]; then cat /proc/{int(pid)}/stat; else echo __GONE__; fi")
    if not r.ok:
        return None
    return "" if r.out.strip() == "__GONE__" else r.out


def _watchdog(run, e):
    pid, ex = e["key"], e.get("extra") or {}
    st = run.dev.boot_status()
    if not st:
        return False, "cannot read boot_id: not proof the watchdog is from an earlier boot"
    if st.boot_id != ex.get("boot_id"):
        return True, "watchdog belonged to an earlier boot"
    stat = _proc_stat(run, pid)
    if stat is None:
        return False, f"/proc/{pid} unreadable: not proof the watchdog is gone"
    if not stat:
        return True, "watchdog already gone"
    start = stat.rsplit(")", 1)[1].split()[19]
    if start != ex.get("start"):
        return True, "PID reused by another process; not killing"
    run.dev.sh(f"kill {pid}")
    time.sleep(0.5)
    after = _proc_stat(run, pid)
    gone = after == ""
    return gone, f"watchdog {pid} {'killed' if gone else 'still alive or unreadable'}"


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
        # The list of known devices, with its status: a failed listing is not
        # proof the bond is gone.
        r = host_run(["bluetoothctl", "devices"], timeout=20)
        ok_host = r.returncode == 0 and phone_addr.upper() not in (r.stdout or "").upper()
    return ok_phone and ok_host, f"bond removed: phone {ok_phone}, host {ok_host}"


def _bt_discoverable(run, e):
    host_run(["bluetoothctl", "discoverable", "on" if e["original"] == "yes" else "off"], timeout=20)
    r = host_run(["bluetoothctl", "show"], timeout=20)
    if r.returncode != 0:
        # A failed show is not proof of the state (review round 4).
        return False, f"bluetoothctl show failed (exit {r.returncode}): not proof Discoverable is {e['original']}"
    ok = re.search(rf"^\s*Discoverable: {re.escape(str(e['original']))}\s*$", r.stdout or "", re.M) is not None
    return ok, f"host Discoverable: {e['original'] if ok else 'not ' + str(e['original'])}"


def _adb_reverse(run, e):
    run.dev.adb("reverse", "--remove", f"tcp:{e['key']}", timeout=20)
    r = run.dev.adb("reverse", "--list", timeout=20)
    if r.returncode != 0:
        return False, f"adb reverse --list failed (exit {r.returncode}): not proof tcp:{e['key']} is gone"
    gone = f"tcp:{e['key']} " not in (r.stdout or "")
    return gone, f"adb reverse tcp:{e['key']} {'removed' if gone else 'still there'}"


def _gvfs(run, e):
    try:
        host_run(["gio", "mount", "-u", e["key"]], timeout=90)
    except TimeoutError:
        pass
    r = host_run(["gio", "mount", "-l"], timeout=20)
    if r.returncode != 0:
        return False, f"gio mount -l failed (exit {r.returncode}): not proof {e['key']} is unmounted"
    left = e["key"].rstrip("/") in (r.stdout or "")
    return not left, f"{e['key']} {'still mounted' if left else 'unmounted'}"


def _fb_mode(run, e):
    """A leftover fb_mode obligation (review round 4): clear it and boot
    Android (fbmode.recover), then confirm the entry is gone. The entry is
    resolved by fbmode itself, so this handler re-reads the journal."""
    from . import fbmode
    what = fbmode.recover(run.dev, 600, boot=True, j=run.journal)
    left = fbmode.pending(jmod.Journal(run.journal.path, run.journal.qa_home))
    return left is None, f"fb_mode: {what or 'nothing to clear'}"


HANDLERS = {
    "setting": _setting, "location": _location, "bt_enabled": _bt_enabled, "bt_scan_mode": _bt_scan_mode,
    "usb_functions": _usb_functions, "user_rotation": _user_rotation, "volume_music": _volume, "ntp": _ntp,
    "sysfs": _sysfs, "wifi": _wifi, "package": _package, "alarm": _alarm, "marker_file": _marker, "watchdog": _watchdog,
    "bt_bond": _bond, "bt_discoverable": _bt_discoverable, "adb_reverse": _adb_reverse, "gvfs_mount": _gvfs,
    "fb_mode": _fb_mode,
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
    # Entries created during recovery (a hidden_api_policy whose restore
    # failed, for one) get one more pass.
    planned = {x["id"] for x in restore}
    for e in [x for x in run.journal.entries if x["id"] not in planned
              and not (x["kind"] == "package" and x["key"] == "river.qa")]:
        ok, msg = restore_entry(run, e)
        (done if ok else failed).append(msg)
    helper_left = run.journal.find(jmod.RESOURCE, "package", "river.qa")
    if helper_left and not failed:
        from . import helper
        if helper.uninstall(run):
            done.append("helper uninstalled")
        else:
            failed.append("helper still installed")
    # Reread the whole journal from disk: preflight goes on only if it is
    # empty, whatever created the entries (review round 2).
    left = jmod.Journal(run.journal.path, run.journal.qa_home).entries
    if left:
        failed.append(f"journal not empty after recovery ({len(left)} entries): " +
                      ", ".join(f"{e['kind']} {e['key']}" for e in left))
    run.journal.entries = left
    return done, drop, failed
