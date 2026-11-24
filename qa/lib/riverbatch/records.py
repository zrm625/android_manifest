"""Council approval records and their immutable bundles (R5.1, R5.2, I14).

A record (``record.json``) and every brief and reviewer reply it names are
one bundle, ``$BENCH_HOME/runner/council/<name>/<sha>/``, where ``<sha>`` is
the SHA-256 of ``record.json``. File names in a record are **basenames**
(I21): the files sit next to the record, in the draft directory and in the
bundle.

Record (version 2)::

    {"version": 2, "name": "perch-launcher", "kind": "change", "alone": false,
     "needs_zak": null, "branch": "perch-launcher",
     "prs": [{"key": "zrm625/android_device_motorola_river#6", "path": "device/motorola/river"}, ...],
     "rounds": [
       {"scope": "full", "at": "...", "brief": "r2-brief.md", "brief_sha256": "...",
        "heads": {"<key>": "<40-hex>"},
        "verdicts": {"opus": "APPROVE 9", "gemini": "APPROVE 8", "sol": "APPROVE 9"},
        "replies": {"opus": {"file": "r2-opus.txt", "sha256": "..."}, ...}},
       {"scope": "delta", ..., "heads": {"<key>": {"from": "<40-hex>", "to": "<40-hex>"}}, ...}],
     "prior": []}

**Evidence binding** (R5.2 item 5 as tightened by I14, Sol B3). For every
round, the brief and each reply carry the round's **scope block**:

- a line ``Scope: full`` or ``Scope: delta`` equal to the round's scope;
- for a full round, one line per PR ``Head: <owner/name#N> <40-hex head>``;
- for a delta round, one line per PR ``Delta: <owner/name#N> <40-hex from>
  -> <40-hex to>`` (in that order).

The PR-to-head lines must be exactly the round's ``heads``: every PR once,
with its own head (or its own from and to, not reversed), and no other PR.
A SHA merely mentioned somewhere does not bind anything. Each reply also has
a verdict line ``Verdict: APPROVE`` (optionally with the score) equal to the
provider's recorded verdict. A reply about other heads, another scope, a
reversed delta, swapped PRs or another verdict does not count.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from riverqa.core import fsync_dir

from . import paths

PROVIDERS = ("opus", "gemini", "sol")
KINDS = ("change", "experiment", "tooling")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
VERDICT_RE = re.compile(r"^(APPROVE|OBJECT)(?: (\d{1,2}))?$")
SCOPE_LINE_RE = re.compile(r"(?im)^[\s>*#_-]*scope[\s*_]*[:=][\s*_]*(full|delta)\b")
VERDICT_LINE_RE = re.compile(r"(?im)^[\s>*#_-]*verdict[\s*_]*[:=][\s*_]*(APPROVE|OBJECT)\b(?:\s*[:/-]?\s*(\d{1,2}))?")


class RecordError(ValueError):
    pass


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def split_key(key: str) -> tuple[str, int]:
    m = KEY_RE.match(key or "")
    if not m:
        raise RecordError(f"bad PR key {key!r}: want owner/name#number")
    return m.group(1), int(m.group(2))


def identity(record_or_prs) -> str:
    """The canonical identity of a change (I13): its sorted PR keys."""
    prs = record_or_prs.get("prs") if isinstance(record_or_prs, dict) else record_or_prs
    return "+".join(sorted(p["key"] for p in prs))


def _basename(name, what: str) -> str:
    if not isinstance(name, str) or not name or "/" in name or name in (".", "..") or name == "record.json":
        raise RecordError(f"{what}: {name!r} must be a plain file name next to the record (I21)")
    return name


SCOPE_KEY = r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*)"
_SEP = r"[\s*_`]"
BIND_ANY_RE = re.compile(r"(?im)^[\s>*#_-]*(head|delta)[\s*_]*[:=](.*)$")
HEAD_RE = re.compile(rf"^{_SEP}*{SCOPE_KEY}{_SEP}+([0-9a-f]{{40}}){_SEP}*$")
DELTA_RE = re.compile(rf"^{_SEP}*{SCOPE_KEY}{_SEP}+([0-9a-f]{{40}}){_SEP}*(?:->|\u2192){_SEP}*([0-9a-f]{{40}}){_SEP}*$")


def scope_block(text: str) -> tuple[dict, dict, list[str]]:
    """The ``Head:`` and ``Delta:`` lines of a brief or reply: ({key:
    head}, {key: {"from", "to"}}, errors). A line that starts like one but
    is not exactly ``<key> <40-hex>`` (or ``<key> <from> -> <to>``), or a
    key given twice, is an error."""
    heads, deltas, errs = {}, {}, []
    for kind, rest in BIND_ANY_RE.findall(text):
        kind = kind.lower()
        m = (HEAD_RE if kind == "head" else DELTA_RE).match(rest)
        if not m:
            errs.append(f"malformed {kind.capitalize()} line {rest.strip()[:100]!r}")
            continue
        key = m.group(1)
        val = m.group(2) if kind == "head" else {"from": m.group(2), "to": m.group(3)}
        into = heads if kind == "head" else deltas
        if key in into and into[key] != val:
            errs.append(f"{key} is bound twice, to different heads")
        into[key] = val
    return heads, deltas, errs


def check_binding(text: str, rnd: dict, who: str) -> list[str]:
    """Why ``text`` (a brief or a reply) does not bind exactly this round's
    PRs to exactly its heads (Sol B3), or []."""
    heads, deltas, errs = scope_block(text)
    errs = [f"{who}: {e}" for e in errs]
    want = rnd["heads"]
    got, other = (heads, deltas) if rnd["scope"] == "full" else (deltas, heads)
    word = "Head" if rnd["scope"] == "full" else "Delta"
    if other:
        errs.append(f"{who}: has {'Delta' if word == 'Head' else 'Head'} lines in a {rnd['scope']} round")
    if got != want:
        missing = sorted(k for k in want if k not in got)
        extra = sorted(k for k in got if k not in want)
        wrong = sorted(k for k in want if k in got and got[k] != want[k])
        why = "; ".join(x for x in (
            f"no {word} line for {', '.join(missing)}" if missing else "",
            f"binds PRs outside the round: {', '.join(extra)}" if extra else "",
            f"binds {', '.join(wrong)} to other heads than the round's" if wrong else "") if x)
        errs.append(f"{who}: scope block does not bind the round's PRs to its heads ({why})")
    return errs


def _verdict(v) -> tuple[str, str | None]:
    m = VERDICT_RE.match(v or "") if isinstance(v, str) else None
    if not m:
        raise RecordError(f"bad verdict {v!r}: want 'APPROVE N' or 'OBJECT N'")
    return m.group(1), m.group(2)


def check_reply(text: str, rnd: dict, provider: str, verdict: str) -> list[str]:
    """Why this reply does not approve this round (I14), or []."""
    errs = []
    scopes = {s.lower() for s in SCOPE_LINE_RE.findall(text)}
    if scopes != {rnd["scope"]}:
        errs.append(f"{provider}: reply states scope {sorted(scopes) or 'none'}, the round is {rnd['scope']}")
    want_word, want_score = _verdict(verdict)
    found = VERDICT_LINE_RE.findall(text)
    if not found:
        errs.append(f"{provider}: reply has no 'Verdict:' line")
    for word, score in found:
        if word.upper() != want_word or (want_score is not None and score and score != want_score) \
                or (want_score is not None and not score):
            got = f"{word} {score}".strip()
            errs.append(f"{provider}: reply's verdict {got!r} differs from the recorded {verdict!r}")
    errs.extend(check_binding(text, rnd, f"{provider}: reply"))
    return errs


def validate(record: dict, directory: Path, kinds=KINDS) -> list[str]:
    """Every reason the record is invalid (R5.2 items 0-5, I14, I21);
    [] if valid. Files are read from ``directory``."""
    errs: list[str] = []
    try:
        if record.get("version") != 2:
            errs.append("version must be 2")
        name = record.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            errs.append(f"bad name {name!r}")
        if record.get("kind") not in kinds:
            errs.append(f"kind {record.get('kind')!r} is not one of {list(kinds)}")
        if not isinstance(record.get("alone", False), bool):
            errs.append("alone must be true or false")
        if record.get("needs_zak") is not None and not isinstance(record.get("needs_zak"), str):
            errs.append("needs_zak must be null or a reason")
        if not isinstance(record.get("branch"), str) or not record["branch"]:
            errs.append("no branch")
        prs = record.get("prs")
        if not isinstance(prs, list) or not prs:                           # item 0
            return errs + ["prs is empty"]
        keys = []
        for p in prs:
            split_key(p.get("key"))
            if not isinstance(p.get("path"), str) or not p["path"]:
                errs.append(f"{p.get('key')}: no path")
            keys.append(p["key"])
        if len(set(keys)) != len(keys):
            errs.append("a PR key appears twice")
        rounds = record.get("rounds")
        if not isinstance(rounds, list) or not rounds:
            return errs + ["no rounds"]
        approved: dict[str, str] = {}
        for i, rnd in enumerate(rounds):
            where = f"round {i + 1}"
            scope, heads = rnd.get("scope"), rnd.get("heads")
            if not isinstance(heads, dict) or not heads:
                errs.append(f"{where}: no heads")
                continue
            if i == 0:
                if scope != "full":                                          # item 1
                    errs.append(f"{where}: the first round must be full")
                    continue
                if set(heads) != set(keys):
                    errs.append(f"{where}: names {sorted(heads)}, the record's PRs are {sorted(keys)}")
                for k, h in heads.items():
                    if not isinstance(h, str) or not SHA_RE.match(h):
                        errs.append(f"{where}: {k} head {h!r} is not 40-hex")
                    else:
                        approved[k] = h
            else:
                if scope != "delta":                                         # item 2
                    errs.append(f"{where}: later rounds must be delta (an objected round goes in prior)")
                    continue
                for k, v in heads.items():
                    if k not in approved:
                        errs.append(f"{where}: delta names {k}, which is not one of the record's PRs")
                        continue
                    if not isinstance(v, dict) or not all(SHA_RE.match(str(v.get(x, ""))) for x in ("from", "to")):
                        errs.append(f"{where}: {k} needs 40-hex from and to")
                        continue
                    if v["from"] != approved[k]:
                        errs.append(f"{where}: {k} from {v['from'][:12]} is not the head approved so far "
                                    f"{approved[k][:12]}")
                    approved[k] = v["to"]
            verdicts, replies = rnd.get("verdicts") or {}, rnd.get("replies") or {}
            for prov in PROVIDERS:                                           # item 3
                try:
                    word, _ = _verdict(verdicts.get(prov))
                except RecordError as e:
                    errs.append(f"{where}: {prov}: {e}")
                    continue
                if word != "APPROVE":
                    errs.append(f"{where}: {prov} did not approve ({verdicts.get(prov)}); an objected round goes "
                                "in prior and the chain restarts with a full round")
            try:
                brief = _basename(rnd.get("brief"), f"{where} brief")
                btext = _read_checked(directory / brief, rnd.get("brief_sha256"), f"{where} brief")   # item 4
                bscopes = {s.lower() for s in SCOPE_LINE_RE.findall(btext)}
                if bscopes != {scope}:
                    errs.append(f"{where}: brief states scope {sorted(bscopes) or 'none'}, the round is {scope}")
                errs.extend(f"{where}: {e}" for e in check_binding(btext, rnd, "brief"))
            except RecordError as e:
                errs.append(str(e))
            for prov in PROVIDERS:
                rep = replies.get(prov)
                if not isinstance(rep, dict):
                    errs.append(f"{where}: no reply from {prov}")
                    continue
                try:
                    fname = _basename(rep.get("file"), f"{where} {prov} reply")
                    text = _read_checked(directory / fname, rep.get("sha256"), f"{where} {prov} reply")
                    errs.extend(f"{where}: {e}" for e in check_reply(text, rnd, prov, verdicts.get(prov)))
                except RecordError as e:
                    errs.append(str(e))
        if not isinstance(record.get("prior", []), list):
            errs.append("prior must be a list")
    except (RecordError, KeyError, TypeError, AttributeError) as e:
        errs.append(f"malformed record: {e}")
    return errs


def approved_heads(record: dict) -> dict[str, str]:
    """PR key -> head approved by the whole chain."""
    heads = dict(record["rounds"][0]["heads"])
    for rnd in record["rounds"][1:]:
        for k, v in rnd["heads"].items():
            heads[k] = v["to"]
    return heads


def files_of(record: dict) -> list[str]:
    out = []
    for rnd in record.get("rounds") or []:
        out.append(rnd["brief"])
        out.extend(r["file"] for r in (rnd.get("replies") or {}).values())
    for rnd in record.get("prior") or []:
        for f in [rnd.get("brief")] + [r.get("file") for r in (rnd.get("replies") or {}).values()]:
            try:
                out.append(_basename(f, "prior"))       # never '.', '..', a path or record.json
            except RecordError:
                continue
    return list(dict.fromkeys(out))


def _read_checked(path: Path, want_sha: str | None, what: str) -> str:
    try:
        data = path.read_bytes()
    except OSError as e:
        raise RecordError(f"{what}: {path.name} cannot be read: {e}") from e
    if not want_sha or sha256_bytes(data) != want_sha:
        raise RecordError(f"{what}: {path.name} does not have the recorded SHA-256")
    return data.decode(errors="replace")


# --- bundles (R5.1) ---------------------------------------------------------------

def load_draft(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    try:
        rec = json.loads(data)
    except ValueError as e:
        raise RecordError(f"{path} is not JSON: {e}") from e
    if not isinstance(rec, dict):
        raise RecordError(f"{path} is not a JSON object")
    return rec, data


def bundle_dir(name: str, sha: str) -> Path:
    return paths.council_dir() / name / sha


def store_bundle(record: dict, raw: bytes, src_dir: Path) -> str:
    """Copy the record and its files into their immutable bundle, durably,
    before any state transaction (R5.1). Returns the bundle SHA. An existing
    bundle must match byte for byte."""
    sha = sha256_bytes(raw)
    final = bundle_dir(record["name"], sha)
    names = files_of(record)
    if final.exists():
        if (final / "record.json").read_bytes() != raw:
            raise RecordError(f"bundle {final} exists with other content; refusing")
        for n in names:
            if not (final / n).exists() or (final / n).read_bytes() != (src_dir / n).read_bytes():
                raise RecordError(f"bundle {final} exists and {n} differs; refusing")
        return sha
    try:
        contents = [("record.json", raw)] + [(n, (src_dir / n).read_bytes()) for n in names]
    except OSError as e:
        raise RecordError(f"{e.filename}: cannot be read ({e.strerror}); every brief and reply the record "
                          "names, prior rounds included, must sit next to it") from e
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{sha[:12]}.", dir=final.parent))
    try:
        for n, data in contents:
            with open(tmp / n, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        fsync_dir(tmp)
        os.rename(tmp, final)
        fsync_dir(final.parent)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return sha


def load_bundle(name: str, sha: str) -> tuple[dict | None, list[str]]:
    """(record, errors): the bundle's record if it still hashes to ``sha``
    and is valid."""
    d = bundle_dir(name, sha)
    try:
        raw = (d / "record.json").read_bytes()
    except OSError as e:
        return None, [f"bundle {name}/{sha[:12]} cannot be read: {e}"]
    if sha256_bytes(raw) != sha:
        return None, [f"bundle {name}/{sha[:12]}: record.json no longer hashes to its name"]
    try:
        rec = json.loads(raw)
    except ValueError as e:
        return None, [f"bundle {name}/{sha[:12]}: record.json does not parse: {e}"]
    if not isinstance(rec, dict):
        return None, [f"bundle {name}/{sha[:12]}: record.json is not an object"]
    errs = validate(rec, d)
    return (rec if not errs else None), errs
