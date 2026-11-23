"""The shared device lock, $BENCH_HOME/.device-lock.

The batch runner (or scripts/device-lock) takes the lock with flock, writes a
fresh token into the lock file, and exports BENCH_LOCK_TOKEN. flash,
device-bench, phone-setup and phone-qa then run in token mode: they skip flock
and refuse to run unless the token in the environment equals the token in the
file and someone really holds the lock. With no token in the environment, a
tool takes the lock itself for its own run.

Either way, no device job starts while a build holds the build lock,
$BENCH_HOME/.lock (scripts/bench/lib.sh): a build loads the host enough to
eat the timing brackets' headroom (review round 5, R5-13). The runner builds
first and then runs the device jobs, in sequence. The probe takes the lock
shared, so concurrent probes don't block each other; flash --merge-started
and --promote, which time nothing, skip it (R6-5).
"""

from __future__ import annotations

import fcntl
import os
import secrets

from .core import bench_path


class LockError(RuntimeError):
    pass


def _path():
    return bench_path(".device-lock")


def build_running() -> bool | None:
    """True if a build holds $BENCH_HOME/.lock, False if not (or there is no
    lock file), None if the lock file can't be opened."""
    p = bench_path(".lock")
    if not p.exists():
        return False
    try:
        fd = os.open(p, os.O_RDONLY)
    except OSError:
        return None
    try:
        # Shared (R6-5): the build takes it exclusive (flock -n in
        # scripts/bench/lib.sh), so a shared probe still sees a build, but
        # two device jobs probing at once don't see each other as one.
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _refuse_during_build() -> None:
    b = build_running()
    if b is None:
        raise LockError("can't read the build lock $BENCH_HOME/.lock; refusing to run a device job")
    if b:
        raise LockError("a build holds $BENCH_HOME/.lock; device jobs wait until it finishes (timing needs an "
                        "idle host)")


def _held_by_someone() -> bool:
    fd = os.open(_path(), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class DeviceLock:
    """``check_build=False`` skips the build-lock probe, for host-side
    steps that measure no timing (flash --merge-started, --promote; R6-5)."""

    def __init__(self, check_build: bool = True):
        self.fd: int | None = None
        self.token: str | None = None
        self.owned = False
        self.check_build = check_build

    def acquire(self) -> "DeviceLock":
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.check_build:
            _refuse_during_build()
        env_token = os.environ.get("BENCH_LOCK_TOKEN")
        if env_token:
            try:
                file_token = path.read_text().strip()
            except FileNotFoundError:
                file_token = ""
            if file_token != env_token:
                raise LockError("BENCH_LOCK_TOKEN does not match the device lock; refusing to run")
            if not _held_by_someone():
                raise LockError("BENCH_LOCK_TOKEN is set but nobody holds the device lock; refusing to run")
            self.token = env_token
            return self
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockError("another device job holds $BENCH_HOME/.device-lock")
        self.fd, self.owned = fd, True
        self.token = secrets.token_hex(16)
        os.ftruncate(fd, 0)
        os.pwrite(fd, self.token.encode() + b"\n", 0)
        os.fsync(fd)
        os.environ["BENCH_LOCK_TOKEN"] = self.token  # children reuse it
        return self

    def release(self) -> None:
        if self.owned and self.fd is not None:
            try:
                os.ftruncate(self.fd, 0)
            except OSError:
                pass
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            os.environ.pop("BENCH_LOCK_TOKEN", None)
        self.fd, self.owned = None, False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
