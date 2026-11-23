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
FB_MODE = "fb_mode"      # the kind of fbmode's obligation entry


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


class Journal:
    """The journal file. Every read and every change goes to the file, never
    to a copy held in memory (R8-1): two Journal objects for the same file
    (phone-qa's run journal and fbmode's) can't overwrite each other's
    entries with a stale list. The device lock keeps out other processes."""

    def __init__(self, path: Path, qa_home: Path | None = None):
        self.path = path
        self.qa_home = qa_home or path.parent

    @property
    def entries(self) -> list[dict]:
        """The entries now in the file (a fresh list on every read)."""
        data = read_json(self.path, None) or {"version": 1, "entries": []}
        return data["entries"]

    def _save(self, entries: list[dict]) -> None:
        write_json(self.path, {"version": 1, "entries": entries})

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
        entries = self.entries
        for old in entries:
            if old["where"] == where and old["kind"] == kind and old["key"] == key:
                return old
        e = {"id": secrets.token_hex(4), "where": where, "kind": kind, "key": key,
             "original": original, "attempt_id": attempt_id, "run_id": run_id,
             "wipe_seq": current_wipe_seq(self.qa_home) if where == PHONE else None,
             "created_at": now_iso(), "extra": extra or {}}
        self._save(entries + [e])  # durable before the change is made
        return e

    def annotate(self, entry: dict, **extra) -> dict:
        """Add fields to an entry's ``extra``, durably. Returns the entry as
        saved. Raises KeyError if the entry is gone."""
        entries = self.entries
        for e in entries:
            if e["id"] == entry["id"]:
                e["extra"] = {**(e.get("extra") or {}), **extra}
                self._save(entries)
                return e
        raise KeyError(f"journal entry {entry['id']} is not in {self.path}")

    def resolve(self, entry: dict) -> None:
        """Drop a restored or cleaned-up entry. An fb_mode obligation is
        refused: only fbmode.settle resolves one, with a verified boot
        (R8-1)."""
        if entry.get("kind") == FB_MODE:
            raise ValueError("an fb_mode obligation is resolved only by fbmode.settle, with a VerifiedBoot")
        self._drop(entry)

    def _settle_fb_mode(self, entry: dict) -> None:
        """For fbmode.settle only (a unit test checks that nothing else
        calls it)."""
        if entry.get("kind") != FB_MODE:
            raise ValueError(entry.get("kind"))
        self._drop(entry)

    def _drop(self, entry: dict) -> None:
        self._save([e for e in self.entries if e["id"] != entry["id"]])


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
