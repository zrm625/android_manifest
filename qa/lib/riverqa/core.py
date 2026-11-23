"""Paths, time, JSON files and subprocesses with deadlines."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

QA_DIR = Path(__file__).resolve().parents[2]          # .repo/manifests/qa
MANIFEST_DIR = QA_DIR.parent                          # .repo/manifests
SCRIPTS_DIR = MANIFEST_DIR / "scripts"
TOP = MANIFEST_DIR.parents[1]                         # the repo tree

BENCH_HOME = Path(os.environ.get("BENCH_HOME", "/mnt/storage/lineageos-bench"))
SERIAL = os.environ.get("BENCH_DEVICE", "ZY225DPT7F")
CODENAME = "river"


def bench_path(*parts: str) -> Path:
    return BENCH_HOME.joinpath(*parts)


def batch_dir(batch_id: str) -> Path:
    check_id(batch_id, "batch ID")
    return bench_path("batches", batch_id)


def check_id(value: str, what: str) -> str:
    ok = value and len(value) <= 80 and all(c.isalnum() or c in "._-" for c in value)
    if not ok:
        raise SystemExit(f"error: bad {what} {value!r}")
    return value


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def sha256_file(path: Path, limit: int | None = None) -> str:
    h = hashlib.sha256()
    left = limit
    with open(path, "rb") as f:
        while True:
            n = 1 << 20 if left is None else min(1 << 20, left)
            if n == 0:
                break
            chunk = f.read(n)
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
    return h.hexdigest()


def read_json(path: Path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path: Path, data) -> None:
    """Write atomically: a crash leaves the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def append_jsonl(path: Path, record) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path) -> list:
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


class Deadline:
    """A hard deadline for a wait loop."""

    def __init__(self, seconds: float, what: str):
        self.end = time.monotonic() + seconds
        self.what = what
        self.seconds = seconds

    def left(self) -> float:
        return self.end - time.monotonic()

    def expired(self) -> bool:
        return self.left() <= 0

    def check(self) -> None:
        if self.expired():
            raise TimeoutError(f"{self.what}: no result within {self.seconds:.0f} s")


def run(cmd: list[str], timeout: float, input: str | bytes | None = None,
        text: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command to completion under a deadline, reading all its output.

    Reading everything matters: closing a dumpsys pipe early sends SIGPIPE to
    the service being dumped (it killed the camera HAL once).
    """
    try:
        return subprocess.run(cmd, input=input, capture_output=True, text=text,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"{' '.join(cmd[:6])}: timed out after {timeout:.0f} s") from e


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
