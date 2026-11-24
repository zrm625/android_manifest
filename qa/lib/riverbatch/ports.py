"""The runner's ports to the outside: local git, the tools (through the
holder), podman, the phone, the clock and disk space. Tests replace them.

Every tool runs through ``hold.py`` (R6.2) with the two lock fds and the
tokens; nothing else ever sees a lock fd (the fds are opened close-on-exec)."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from riverqa.core import TOP, SERIAL, run as host_run

from . import paths
from .compose import GitError


class Git:
    """Local git in the tree (``path`` relative to TOP)."""

    def __init__(self, top: Path = TOP):
        self.top = top

    def _run(self, path: str, args, env=None, text=True):
        e = None
        if env:
            e = dict(os.environ, **env)
        try:
            return subprocess.run(["git", "-C", str(self.top / path), *args], capture_output=True, text=text,
                                  timeout=600, env=e)
        except subprocess.TimeoutExpired as ex:
            raise GitError(path, list(args), 124, "timed out") from ex

    def try_(self, path: str, *args, env=None) -> tuple[int, str]:
        r = self._run(path, args, env)
        return r.returncode, (r.stdout if r.returncode in (0, 1) else r.stderr or r.stdout)

    def out(self, path: str, *args, env=None) -> str:
        r = self._run(path, args, env)
        if r.returncode != 0:
            raise GitError(path, list(args), r.returncode, r.stderr or r.stdout)
        return r.stdout

    def raw(self, path: str, *args) -> bytes:
        r = self._run(path, args, text=False)
        if r.returncode != 0:
            raise GitError(path, list(args), r.returncode, (r.stderr or b"").decode(errors="replace"))
        return r.stdout


@dataclass
class ToolResult:
    rc: int
    stdout: str

    def last_line(self) -> str:
        lines = [ln for ln in self.stdout.splitlines() if ln.strip()]
        return lines[-1] if lines else ""


class Tools:
    """Runs a tool through the holder (R6.2)."""

    def __init__(self, lock_fds: tuple[int, ...] = (), tokens: dict | None = None):
        self.lock_fds = lock_fds
        self.tokens = tokens or {}
        self.current: subprocess.Popen | None = None

    def run(self, argv: list[str], batch_id: str | None, log: Path | None = None, env: dict | None = None,
            timeout: float | None = None) -> ToolResult:
        e = {k: v for k, v in os.environ.items() if k not in ("BENCH_LOCK_TOKEN", "BENCH_BUILD_LOCK_TOKEN",
                                                                "RIVER_BATCH_ID")}
        e.update(self.tokens)
        if batch_id:
            e["RIVER_BATCH_ID"] = batch_id
        e.update(env or {})
        e["BENCH_HOME"] = str(paths.home())
        cmd = [sys.executable, "-I", str(paths.PKG / "hold.py"), str(paths.children_dir()), "--", *argv]
        logf = open(log, "ab") if log else subprocess.DEVNULL
        try:
            self.current = subprocess.Popen(cmd, pass_fds=self.lock_fds, start_new_session=True, env=e,
                                            stdout=subprocess.PIPE, stderr=logf)
            out, _ = self.current.communicate(timeout=timeout)
            rc = self.current.returncode
        finally:
            self.current = None
            if log:
                logf.close()
        text = out.decode(errors="replace")
        if log:
            with open(log, "a") as f:
                f.write(text)
        return ToolResult(128 - rc if rc < 0 else rc, text)

    def start(self, argv: list[str], batch_id: str | None, log: Path, env: dict | None = None) -> subprocess.Popen:
        """Start a tool through the holder without waiting (part 1's B4)."""
        e = {k: v for k, v in os.environ.items() if k not in ("BENCH_LOCK_TOKEN", "BENCH_BUILD_LOCK_TOKEN",
                                                                "RIVER_BATCH_ID")}
        e.update(self.tokens)
        if batch_id:
            e["RIVER_BATCH_ID"] = batch_id
        e.update(env or {})
        e["BENCH_HOME"] = str(paths.home())
        cmd = [sys.executable, "-I", str(paths.PKG / "hold.py"), str(paths.children_dir()), "--", *argv]
        with open(log, "ab") as logf:
            self.current = subprocess.Popen(cmd, pass_fds=self.lock_fds, start_new_session=True, env=e,
                                            stdout=logf, stderr=logf)
        return self.current

    def stop(self) -> None:
        """The runner got a signal: SIGTERM its holder and wait (R6.2)."""
        p = self.current
        if p is not None and p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                pass


class Podman:
    def run(self, *args: str) -> subprocess.CompletedProcess:
        return host_run(["podman", *args], timeout=120)


def phone():
    from riverqa.device import Device
    return Device(SERIAL)


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def now() -> float:
    return time.time()
