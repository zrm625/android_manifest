"""The runner's context: ports, locks, the control-flow exceptions and the
small helpers every phase uses (journal commits, tool calls, file reads)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from riverqa import children, journal as jmod
from riverqa.core import now_iso, read_json, read_jsonl
from riverqa.lock import BuildLock, DeviceLock, LockError

from . import github, paths, ports, state as S, tc
from .compose import GitError

EXIT_OK, EXIT_ERROR, EXIT_HALT, EXIT_RESUMABLE = 0, 1, 3, 75
# Deadlines (seconds) per tool, generous: a cold build is ~6 h on this host,
# a 3-boot bench and the full QA run well under 2 h each.
TOOL_TIMEOUTS = {"build": 10 * 3600, "flash": 2 * 3600, "device-bench": 3 * 3600, "phone-qa": 4 * 3600,
                 "phone-setup": 30 * 60}


# --- control flow ------------------------------------------------------------------

class Stop(Exception):
    """Leave the current step; ``kind`` says how the tick reports it."""
    kind = "stop"

    def __init__(self, message: str, advice: str | None = None, step: str | None = None):
        super().__init__(message)
        self.message, self.advice, self.step = message, advice, step


class Halt(Stop):
    kind = "halt"


class Pause(Stop):
    kind = "pause"


class Blocked(Stop):
    kind = "blocked"


class NotQualified(Stop):
    kind = "not-qualified"


class Retry(Stop):
    """A step that cannot finish this tick (a failed rollback): stop; the
    next tick derives it again."""
    kind = "retry"


class ReExec(Stop):
    """The manifest checkout was fast-forwarded: release the locks and
    re-exec once (R8.2 step 2)."""
    kind = "reexec"


class Busy(Stop):
    """A lock or the build container is busy, or a child is alive: 75."""
    kind = "busy"


class Abort(Exception):
    """End a run before merge_started with an outcome (R11.3)."""

    def __init__(self, outcome: dict, pause: str | None = None):
        super().__init__(outcome.get("reason") or outcome.get("class"))
        self.outcome, self.pause = outcome, pause


Unknown = github.Unknown
AuthRefused = github.AuthRefused
Refused = github.Refused


# --- context ------------------------------------------------------------------------

@dataclass
class Ctx:
    gh: github.GitHub = field(default_factory=github.GitHub)
    git: object = field(default_factory=ports.Git)
    tools: ports.Tools = field(default_factory=ports.Tools)
    podman: object = field(default_factory=ports.Podman)
    phone_factory: object = ports.phone
    sleep: object = time.sleep
    now: object = time.time
    free_gb: object = ports.free_gb
    tc_check: object = tc.check
    cfg: dict = field(default_factory=paths.config)
    unknown: str | None = None           # R8.1: an unknown outcome left the loop
    tick_id: str | None = None           # the tick this context runs (Sol SM8: refusals count once per tick)
    notes: list = field(default_factory=list)
    locks: tuple = ()

    # --- reads --------------------------------------------------------------------
    def phone(self):
        return self.phone_factory()

    def slot_state(self) -> dict | None:
        return read_json(paths.slot_state_file())

    def known_good(self) -> dict | None:
        return read_json(paths.known_good_file())

    def batch_json(self, bid: str) -> dict | None:
        p = paths.batch_dir(bid) / "batch.json"
        if not p.exists():
            return None
        try:
            return read_json(p)
        except ValueError as e:
            raise Halt(f"{p} is unreadable: {e}") from e

    def flash_rec(self, bid: str) -> dict:
        return read_json(paths.batch_dir(bid) / "flash.json", {}) or {}

    def wipes(self) -> list[dict]:
        return read_jsonl(paths.home() / "qa" / "wipes.jsonl")

    def wipe_seq(self) -> int:
        return jmod.current_wipe_seq(paths.home() / "qa")

    def note(self, msg: str) -> None:
        self.notes.append(msg)
        from riverqa.core import log
        log(f"batch: {msg}")

    # --- tools ----------------------------------------------------------------------
    def tool(self, name: str, args: list[str], bid: str | None, log_name: str | None = None,
             env: dict | None = None) -> ports.ToolResult:
        """Run a tool through the holder with its deadline (Sol M9, Opus
        minor): ``tool_timeouts`` in runner/config.json, else TOOL_TIMEOUTS."""
        script = {"flash": "flash", "device-bench": "bench/device-bench", "phone-qa": "phone-qa",
                  "phone-setup": "phone-setup", "build": "build"}[name]
        log = None
        if bid:
            d = paths.batch_dir(bid) / "runner-logs"
            d.mkdir(parents=True, exist_ok=True)
            log = d / f"{log_name or name}.log"
        timeout = float((self.cfg.get("tool_timeouts") or {}).get(name, TOOL_TIMEOUTS[name]))
        return self.tools.run([str(paths.SCRIPTS / script), *args], bid, log, env, timeout=timeout)


# --- journal commits (R11.1) ----------------------------------------------------------

def commit_phase(phase: str, **updates) -> dict:
    with S.transaction("phase", phase=phase) as st:
        run = st["running"]
        run["phase"] = phase
        run["intent"] = None
        run.update(updates)
        return dict(run)


def commit_intent(step: str, before: str, after: str) -> None:
    with S.transaction("intent", step=step) as st:
        st["running"]["intent"] = {"step": step, "before": before, "after": after, "at": now_iso()}


def update_running(**updates) -> dict:
    with S.transaction() as st:
        st["running"].update(updates)
        return dict(st["running"])


# --- locks (R6.1-R6.3) ------------------------------------------------------------------

class Locks:
    """Device lock, then build lock, both non-blocking (busy: 75). The
    orphan check runs after each flock (in DeviceLock and BuildLock) and
    again here for the container (R6.2)."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.dl: DeviceLock | None = None
        self.bl: BuildLock | None = None

    def __enter__(self):
        for k in ("BENCH_LOCK_TOKEN", "BENCH_BUILD_LOCK_TOKEN"):
            os.environ.pop(k, None)
        try:
            self.dl = DeviceLock(check_build=False).acquire()
            os.environ.pop("BENCH_LOCK_TOKEN", None)
            self.bl = BuildLock(export=False).acquire()
        except LockError as e:
            self.release()
            raise Busy(f"busy: {e}") from e
        self.ctx.tools.lock_fds = (self.dl.fd, self.bl.fd)
        self.ctx.tools.tokens = {"BENCH_LOCK_TOKEN": self.dl.token, "BENCH_BUILD_LOCK_TOKEN": self.bl.token}
        try:
            children_and_container(self.ctx)
        except Busy:
            self.release()
            raise
        return self

    def release(self):
        if self.bl:
            self.bl.release()
        if self.dl:
            self.dl.release()
        os.environ.pop("BENCH_LOCK_TOKEN", None)
        self.ctx.tools.lock_fds, self.ctx.tools.tokens = (), {}
        self.dl = self.bl = None

    def __exit__(self, *exc):
        self.release()


def children_and_container(ctx: Ctx) -> None:
    """R6.2: 75 while a live child record remains or a lineageos-build
    container runs; a stopped one is removed with `podman rm` (never
    --force)."""
    live = children.live_children(paths.home())
    if live:
        raise Busy("a runner child is still alive: " + "; ".join(children.describe(r) for r in live))
    r = ctx.podman.run("ps", "-a", "--filter", "name=^lineageos-build$", "--format", "{{.State}}")
    if r.returncode != 0:
        raise Busy(f"podman ps failed: {(r.stderr or '').strip()[-200:]}")
    states = [s.strip().lower() for s in r.stdout.splitlines() if s.strip()]
    if any(s in ("running", "paused", "stopping") for s in states):
        raise Busy("a lineageos-build container is running")
    if states:
        rm = ctx.podman.run("rm", "lineageos-build")
        if rm.returncode != 0:
            raise Busy(f"podman rm lineageos-build failed: {(rm.stderr or '').strip()[-200:]}")


# --- git helpers ---------------------------------------------------------------------------

def remote_of(path: str) -> str:
    return "origin" if path == ".repo/manifests" else "zrm625"


def clean(ctx: Ctx, path: str) -> bool:
    rc, out = ctx.git.try_(path, "status", "--porcelain", "--untracked-files=normal")
    return rc == 0 and not out.strip()


def head(ctx: Ctx, path: str) -> str:
    return ctx.git.out(path, "rev-parse", "HEAD").strip()


def branch(ctx: Ctx, path: str) -> str | None:
    rc, out = ctx.git.try_(path, "symbolic-ref", "--short", "-q", "HEAD")
    return out.strip() if rc == 0 and out.strip() else None


def remote_git(ctx: Ctx, path: str, *args) -> str:
    """A git command that talks to a remote, classified like GitHub calls
    (R11.6)."""
    from riverqa import recovery
    try:
        return ctx.git.out(path, *args)
    except GitError as e:
        cls, status, line = recovery.classify(e.rc, "", e.err)
        exc = {recovery.AUTH: AuthRefused, recovery.REFUSED: Refused}.get(cls, Unknown)
        raise exc(f"git {' '.join(args[:2])}", path, status, line) from e


def image_tag() -> str:
    import hashlib
    c = (paths.SCRIPTS / "bench" / "Containerfile").read_bytes()
    return f"lineageos-bench:{hashlib.sha256(c).hexdigest()[:12]}"


def image_id(ctx: Ctx) -> str | None:
    r = ctx.podman.run("image", "inspect", "--format", "{{.Id}}", image_tag())
    out = (r.stdout or "").strip()
    return out if r.returncode == 0 and out else None


def bench_result() -> str | None:
    p = paths.out_dir() / ".bench-result"
    try:
        return p.read_text().strip() or None
    except OSError:
        return None


def river_build() -> dict | None:
    return read_json(paths.out_dir() / "river-build.json")


def wait(ctx: Ctx, seconds: float) -> None:
    ctx.sleep(seconds)


def path_exists(p: Path) -> bool:
    return os.path.lexists(p)
