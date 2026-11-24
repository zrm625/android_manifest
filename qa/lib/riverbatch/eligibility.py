"""Eligibility (R5.3, R5.4): one function, run at every transition into
``queued`` and for every member at claim. It reads GitHub only and never
fetches into the tree, so enqueue and requeue never touch a fork while a
batch runs. A transport failure raises github.Unknown and changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from riverqa import stage

from . import records
from .github import BRANCH, GitHub

OK, REAPPROVE, WAITING, REFUSED = "ok", "needs-reapproval", "waiting-zak", "refused"
MANIFEST_REPO = "zrm625/android_manifest"
RIVER = "device/motorola/river"
CAMERA_FILE = stage.CAMERA_SWITCH_FILE.split(RIVER + "/", 1)[1]


class BaseCameraMismatch(Exception):
    """R5.4 step 1: river-22.2's camera set differs from known-good."""

    def __init__(self, base_set, kg_set):
        super().__init__(f"river-22.2 camera set {base_set} differs from known-good {kg_set}: re-baseline (Z8) first")


@dataclass
class Outcome:
    kind: str
    reason: str = ""


def tooling_path(p: str) -> bool:
    """R5.8: qa/, scripts/, workspace/, or a *.md other than CHARTER.md (at
    any depth); never a top-level manifest XML."""
    if p.startswith(("qa/", "scripts/", "workspace/")):
        return True
    return p.endswith(".md") and p.rsplit("/", 1)[-1] != "CHARTER.md"


def camera_text(gh: GitHub, repo: str, ref: str) -> tuple[str | None, str | None]:
    return gh.file_at(repo, f"{CAMERA_FILE}", ref)


def base_camera_set(gh: GitHub, river_repo: str, base_river: str) -> list[int]:
    return stage.parse_camera_set(camera_text(gh, river_repo, base_river)[1])


def change_camera_set(gh: GitHub, river_repo: str, base_river: str, head: str) -> list[int]:
    """The change's camera set: at the approved river head if the switch file
    differs between the merge base and the head, else at the base."""
    mb = gh.merge_base(river_repo, base_river, head)
    blob_mb, _ = camera_text(gh, river_repo, mb)
    blob_head, text_head = camera_text(gh, river_repo, head)
    if blob_mb != blob_head:
        return stage.parse_camera_set(text_head)
    return stage.parse_camera_set(camera_text(gh, river_repo, base_river)[1])


def pr_set_problems(gh: GitHub, rec: dict, repos: list[str]) -> list[str]:
    """R5.3: the open PRs with head branch ``branch`` in every fork and the
    manifest are exactly the record's PRs, each at its approved head, on
    river-22.2, not cross-repository."""
    heads = records.approved_heads(rec)
    found = {}
    for repo in repos:
        for p in gh.pr_list(repo, rec["branch"], "open"):
            found[f"{repo}#{p['number']}"] = p
    probs = []
    for k in sorted(set(heads) - set(found)):
        probs.append(f"{k}: not open with head branch {rec['branch']} (closed, merged or moved)")
    for k in sorted(set(found) - set(heads)):
        probs.append(f"{k}: an open PR from {rec['branch']} that the record does not name (PR set changed)")
    for k in sorted(set(found) & set(heads)):
        p = found[k]
        if p.get("baseRefName") != BRANCH:
            probs.append(f"{k}: retargeted to {p.get('baseRefName')}")
        if p.get("headRefOid") != heads[k]:
            probs.append(f"{k}: head moved to {str(p.get('headRefOid'))[:12]} (approved {heads[k][:12]})")
        if p.get("isCrossRepository") is not False:
            probs.append(f"{k}: a cross-repository PR")
    return probs


def manifest_outcome(gh: GitHub, rec: dict) -> Outcome | None:
    """R5.4 step 3: an entry with a manifest PR is never ok."""
    mans = [p for p in rec["prs"] if records.split_key(p["key"])[0] == MANIFEST_REPO]
    if not mans:
        return None
    paths = []
    for p in mans:
        for f in gh.changed_files(MANIFEST_REPO, records.split_key(p["key"])[1]):
            paths.append(f.get("filename") or "")
            if f.get("previous_filename"):
                paths.append(f["previous_filename"])          # I21: both sides of a rename
    if paths and all(tooling_path(x) for x in paths):
        return Outcome(REFUSED, "a tooling-only manifest PR goes through council code review and "
                                "`change merge --record`, not the queue (R5.8)")
    return Outcome(WAITING, "manifest source changes are not supported by runner v1"
                   + (f" ({', '.join(sorted(x for x in paths if not tooling_path(x))[:5])})" if paths else ""))


def eligible(gh: GitHub, rec: dict | None, rec_errors: list[str], base: dict[str, str], slugs: dict[str, str],
             kg_cameras: list[int]) -> Outcome:
    """R5.4 for one entry. ``rec`` is the entry's bundle record (None with
    ``rec_errors`` if it no longer hashes or is invalid). ``base``: path ->
    base commit (forks and manifest). ``slugs``: path -> owner/name for the
    forks and the manifest. Raises BaseCameraMismatch (step 1) and
    github.Unknown / AuthRefused (no answer)."""
    river_repo = slugs[RIVER]
    bset = base_camera_set(gh, river_repo, base[RIVER])
    if bset != sorted(kg_cameras):                                           # step 1
        raise BaseCameraMismatch(bset, sorted(kg_cameras))
    if rec is None:                                                          # step 2
        return Outcome(REAPPROVE, "approval record invalid: " + "; ".join(rec_errors[:3]))
    probs = pr_set_problems(gh, rec, sorted(set(slugs.values())))
    if probs:
        return Outcome(REAPPROVE, "; ".join(probs[:4]))
    m = manifest_outcome(gh, rec)                                            # step 3
    if m:
        return m
    heads = records.approved_heads(rec)
    river_pr = next((p for p in rec["prs"] if p["path"] == RIVER), None)     # step 4
    if river_pr:
        cset = change_camera_set(gh, river_repo, base[RIVER], heads[river_pr["key"]])
        if cset != sorted(kg_cameras):
            return Outcome(WAITING, f"camera-set change to {cset}: Zak merges it by hand (Z8)")
    if rec.get("needs_zak"):
        return Outcome(WAITING, f"needs Zak: {rec['needs_zak']}")
    return Outcome(OK)                                                       # step 5
