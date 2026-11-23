"""The state journal, $BENCH_HOME/qa/journal.json, and the wipe log.

Before any change, the journal records the original value. Entries:

- ``phone``: a setting on the phone. Tagged with the flash attempt and the
  /data generation (the wipe sequence number at the time). Dropped only when
  the wipe log shows a successful wipe after it; otherwise restored, even
  after a reboot or a failed flash.
- ``host``: Bluetooth adapter state, gvfs mounts, adb reverse ports. Always
  restored.
- ``resource``: something the run created (alarm label, marker file,
  watchdog PID, Bluetooth bond). Cleaned up by identity.

Recovery runs only after the lock is held and the guards confirmed the phone;
the caller enforces that. This module is pure apart from file I/O.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from .core import append_jsonl, now_iso, read_json, read_jsonl, write_json

PHONE, HOST, RESOURCE = "phone", "host", "resource"


def wipe_log_path(qa_home: Path) -> Path:
    return qa_home / "wipes.jsonl"


def current_wipe_seq(qa_home: Path) -> int:
    seqs = [w["seq"] for w in read_jsonl(wipe_log_path(qa_home)) if w.get("ok")]
    return max(seqs, default=0)


def record_wipe(qa_home: Path, attempt_id: str, how: str) -> int:
    """Record a successful `fastboot erase userdata`. Returns its sequence."""
    seq = current_wipe_seq(qa_home) + 1
    append_jsonl(wipe_log_path(qa_home), {"seq": seq, "attempt_id": attempt_id, "how": how,
                                          "ok": True, "at": now_iso()})
    return seq


def record_wipe_boot(qa_home: Path, seq: int, boot_id: str) -> None:
    """Record the boot_id of the first boot after wipe ``seq`` (review round
    3): calibration ties "the first adb reboot after a wipe" to it."""
    append_jsonl(wipe_log_path(qa_home), {"seq": seq, "first_boot_id": boot_id, "at": now_iso()})


def last_wipe(qa_home: Path) -> dict | None:
    """The latest successful wipe, with ``first_boot_id`` if its first boot
    was recorded. None if there was none."""
    recs = read_jsonl(wipe_log_path(qa_home))
    seq = current_wipe_seq(qa_home)
    if not seq:
        return None
    out = dict(next(w for w in recs if w.get("ok") and w.get("seq") == seq))
    boots = [w["first_boot_id"] for w in recs if w.get("seq") == seq and w.get("first_boot_id")]
    out["first_boot_id"] = boots[0] if len(boots) == 1 else None
    return out


class Journal:
    def __init__(self, path: Path, qa_home: Path | None = None):
        self.path = path
        self.qa_home = qa_home or path.parent
        data = read_json(path, None) or {"version": 1, "entries": []}
        self.entries: list[dict] = data["entries"]

    def save(self) -> None:
        write_json(self.path, {"version": 1, "entries": self.entries})

    def find(self, where: str, kind: str, key: str) -> dict | None:
        for e in self.entries:
            if e["where"] == where and e["kind"] == kind and e["key"] == key:
                return e
        return None

    def record(self, where: str, kind: str, key: str, original, *, attempt_id: str | None = None,
               run_id: str | None = None, extra: dict | None = None) -> dict:
        """Record the original value before changing it. The first original
        wins: a second change of the same thing keeps the first value."""
        if where not in (PHONE, HOST, RESOURCE):
            raise ValueError(where)
        old = self.find(where, kind, key)
        if old is not None:
            return old
        e = {"id": secrets.token_hex(4), "where": where, "kind": kind, "key": key,
             "original": original, "attempt_id": attempt_id, "run_id": run_id,
             "wipe_seq": current_wipe_seq(self.qa_home) if where == PHONE else None,
             "created_at": now_iso(), "extra": extra or {}}
        self.entries.append(e)
        self.save()  # durable before the change is made
        return e

    def resolve(self, entry: dict) -> None:
        self.entries = [e for e in self.entries if e["id"] != entry["id"]]
        self.save()


def plan_recovery(entries: list[dict], wipe_seq_now: int) -> tuple[list[dict], list[dict]]:
    """Split leftovers into (restore, drop). Phone entries from before a
    successful wipe are dropped; everything else is restored or cleaned up.
    Restore order: resources first (they may need the phone state), then
    phone, then host, newest first within each."""
    restore, drop = [], []
    for e in entries:
        if e["where"] == PHONE and e.get("wipe_seq") is not None and wipe_seq_now > e["wipe_seq"]:
            drop.append(e)
        else:
            restore.append(e)
    # Newest first within each class. created_at has one-second resolution,
    # so ties fall back to journal order (later entries are newer). Then
    # hidden_api_policy goes after every Bluetooth entry: restoring the scan
    # mode and removing the bond call hidden APIs through the helper.
    pos = {e["id"]: i for i, e in enumerate(entries)}
    out = []
    for cls in (RESOURCE, PHONE, HOST):
        out += sorted([e for e in restore if e["where"] == cls],
                      key=lambda e: (e["created_at"], pos[e["id"]]), reverse=True)
    policy = [e for e in out if e["kind"] == "setting" and e["key"] == "global/hidden_api_policy"]
    out = [e for e in out if e not in policy] + policy
    return out, drop
