"""Install and drive the river-qa helper from the host.

``instrument()`` runs one helper test with ``am instrument -w -r`` and parses
its streamed status. When the helper reports a stage (its activity is resumed),
the host confirms the foreground itself with ``dumpsys activity top`` and
``dumpsys power``, runs its own step for that stage, and sends the go
broadcast. Only one UiAutomation exists at a time: host uiautomator commands
never run while an instrumentation is running (``Run.ui_busy``).
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

from . import journal as jmod
from .core import log

PKG = "river.qa"
RUNNER = f"{PKG}/.QaInstrumentation"
RUNTIME_PERMS = ["android.permission.CAMERA", "android.permission.RECORD_AUDIO",
                 "android.permission.ACCESS_FINE_LOCATION", "android.permission.ACCESS_COARSE_LOCATION",
                 "android.permission.BLUETOOTH_SCAN", "android.permission.BLUETOOTH_CONNECT"]


@dataclass
class HelperResult:
    test: str
    terminal: bool
    values: dict = field(default_factory=dict)
    stages: list = field(default_factory=list)
    foreground: list = field(default_factory=list)   # (stage, ok, detail)
    raw: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.terminal and self.values.get("result") == "DONE"

    def get(self, k, d=None):
        return self.values.get(k, d)


def install(run) -> tuple[bool, str]:
    dev = run.dev
    run.journal.record(jmod.RESOURCE, "package", PKG, None, attempt_id=run.attempt_id(), run_id=run.run_id)
    r = dev.adb("install", "-r", "-g", str(run.helper_apk), timeout=180)
    if r.returncode != 0 or "Success" not in r.stdout:
        return False, (r.stdout + r.stderr).strip()[-300:]
    for p in RUNTIME_PERMS:
        dev.sh(f"pm grant {PKG} {p}")
    dev.sh(f"appops set {PKG} RUN_ANY_IN_BACKGROUND allow; cmd deviceidle whitelist +{PKG}")
    return True, "installed"


def top_activities(dump: str) -> list[tuple[str, bool]]:
    """(component, resumed) for each ACTIVITY block of `dumpsys activity top`.
    The tasks are listed bottom to top."""
    out = []
    blocks = re.split(r"\n(?=\s*ACTIVITY )", dump)
    for b in blocks:
        m = re.match(r"\s*ACTIVITY (\S+) [0-9a-f]+ pid=", b)
        if m:
            out.append((m.group(1), "mResumed=true" in b.split("\nTASK ")[0]))
    return out


def resumed_top(dump: str) -> tuple[str, bool]:
    acts = top_activities(dump)
    resumed = [a for a, r in acts if r]
    if resumed:
        return resumed[-1], True
    return (acts[-1][0], False) if acts else ("", False)


def foreground(dev, activity_prefix: str = PKG) -> tuple[bool, str]:
    act, resumed = resumed_top(dev.sh("dumpsys activity top", timeout=60))
    awake = "mWakefulness=Awake" in dev.sh("dumpsys power", timeout=30)
    ok = act.startswith(activity_prefix) and resumed and awake
    return ok, f"top {act or '?'}{'' if resumed else ' (not resumed)'}, {'awake' if awake else 'screen not awake'}"


def wake(dev) -> None:
    dev.sh("input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard")


def instrument(run, test: str, args: dict | None = None, timeout: float = 120,
               on_stage=None, no_go: tuple = ("pair-ready",), need_foreground: bool = True) -> HelperResult:
    dev = run.dev
    args = dict(args or {})
    if getattr(run, "upload_base", None):
        args.setdefault("upload", run.upload_base)
    cmd = ["adb", "-s", dev.serial, "shell", "am", "instrument", "-w", "-r", "-e", "test", test]
    for k, v in args.items():
        cmd += ["-e", k, str(v)]
    cmd.append(RUNNER)
    wake(dev)
    res = HelperResult(test, False)
    run.ui_busy = True
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    killer = threading.Timer(timeout, p.kill)
    killer.start()
    raw = []
    status: dict = {}
    result: dict = {}
    last = None
    post_go = []
    try:
        for line in p.stdout:
            line = line.rstrip("\r\n")
            raw.append(line)
            if line.startswith("INSTRUMENTATION_STATUS: "):
                k, _, v = line[len("INSTRUMENTATION_STATUS: "):].partition("=")
                status[k] = v
                last = (status, k)
            elif line.startswith("INSTRUMENTATION_STATUS_CODE:"):
                stage = status.get("stage")
                if stage:
                    res.stages.append(stage)
                    if stage not in no_go:
                        if need_foreground:
                            ok, detail = foreground(dev)
                            res.foreground.append((stage, ok, detail))
                            if not ok:
                                time.sleep(1.5)
                                ok, detail = foreground(dev)
                                res.foreground[-1] = (stage, ok, detail)
                        hook = on_stage(stage) if on_stage else None
                        dev.sh(f"am broadcast -a river.qa.GO -p {PKG} --es stage {stage}")
                        if callable(hook):
                            post_go.append(threading.Thread(target=hook, daemon=True))
                            post_go[-1].start()
                    elif on_stage:
                        hook = on_stage(stage)
                        if callable(hook):
                            post_go.append(threading.Thread(target=hook, daemon=True))
                            post_go[-1].start()
                status = {}
                last = None
            elif line.startswith("INSTRUMENTATION_RESULT: "):
                k, _, v = line[len("INSTRUMENTATION_RESULT: "):].partition("=")
                result[k] = v
                last = (result, k)
            elif line.startswith("INSTRUMENTATION_CODE:"):
                last = None
            elif last is not None:
                d, k = last
                d[k] += "\n" + line
        p.wait(10)
    finally:
        killer.cancel()
        if p.poll() is None:
            p.kill()
        for t in post_go:
            t.join(60)
        run.ui_busy = False
    res.raw = "\n".join(raw)
    res.values = result
    res.terminal = "result" in result
    if not res.terminal:
        res.error = "no terminal result (" + ("timed out" if p.returncode in (-9, None) else
                                              (raw[-1] if raw else "no output")) + ")"
    elif result.get("result") == "ERROR":
        res.error = result.get("error")
    (run.dir / "helper").mkdir(exist_ok=True)
    (run.dir / "helper" / f"{test}-{int(time.time())}.txt").write_text(res.raw)
    return res


def fg_problem(res: HelperResult) -> str | None:
    bad = [f"{s}: {d}" for s, ok, d in res.foreground if not ok]
    return "; ".join(bad) if bad else None


def uninstall(run) -> bool:
    run.dev.sh(f"pm uninstall {PKG}")
    gone = PKG not in run.dev.sh(f"pm list packages {PKG}")
    if gone:
        e = run.journal.find(jmod.RESOURCE, "package", PKG)
        if e:
            run.journal.resolve(e)
    return gone


__all__ = ["install", "instrument", "uninstall", "foreground", "wake", "fg_problem", "HelperResult", "PKG", "log"]
