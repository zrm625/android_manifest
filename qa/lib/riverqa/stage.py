"""Stage a build as a batch: $BENCH_HOME/batches/<id>/ with batch.json.

The batch runner (stage 2) calls this; stage 1 uses it for the baseline.
batch.json is the interface between the runner, flash, device-bench and
phone-qa:

    {
      "version": 1,
      "batch": "<id>",
      "kind": "batch" | "baseline" | "calibration" | "experiment",
      "experiment": false,
      "build_id": "<ro.river.build_id of the staged images>",
                     # kind "batch": exactly "<batch>-<commit_set_hash>", never
                     # "unstamped"; stage refuses anything else (A4)
      "vendor_build_id": "<ro.vendor.river.build_id>",
      "commit_set": {"<repo path>": "<sha>", ...},
      "commit_set_hash": "...",
      "dates": {"build": "...", "vendor": "...", "bootimage": "..."},
      "images": {"boot": {"file": "images/boot.img", "sha256": "...", "size": N}, ...},
      "installed_files": {"system": {"file", "sha256"}, "vendor": {...}},
      "cameras": {"set": [0, 1, 2], "source": "bootstrap" | "<device repo file>"},
      "camera_set_change": null | {"url", "set"},
      "prs": [], "tag": null, "tag_repos": [],
      "pair": null | {...},                 # written by flash
      "first_boot_seconds": null,           # written by flash (information only)
      "bench_runs": [],                     # run IDs of the current flash attempt (device-bench)
      "flash_attempt": null,                # the attempt bench_runs belong to (flash)
      "prior_bench_runs": [],               # earlier attempts' runs, archived by flash
      "staged_at": "..."
    }
"""

from __future__ import annotations

import hashlib
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from .core import MANIFEST_DIR, TOP, batch_dir, now_iso, read_json, sha256_file, write_json
from .slots import KINDS

IMAGES = ("boot", "dtbo", "system", "vendor")
BOOTSTRAP_CAMERAS = [0, 1, 2]
CAMERA_SWITCH_FILE = "device/motorola/river/qa/cameras"  # does not exist until per-camera switches land


def read_prop(path: Path, name: str) -> str | None:
    try:
        for line in path.read_text(errors="replace").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() == name:
                return v.strip()
    except FileNotFoundError:
        return None
    return None


def bootimage_prop_file(product: Path) -> Path:
    return product / "obj/ETC/ramdisk-build.prop_intermediates/ramdisk-build.prop"


def camera_set(tree: Path = TOP) -> dict:
    f = tree / CAMERA_SWITCH_FILE
    if f.exists():
        ids = sorted(int(x) for x in f.read_text().split() if x.strip().isdigit())
        return {"set": ids, "source": CAMERA_SWITCH_FILE}
    return {"set": list(BOOTSTRAP_CAMERAS), "source": "bootstrap"}


UNSTAMPED = "unstamped"
MANIFEST_PATH = ".repo/manifests"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def expected_repos(manifest: Path | None = None) -> set[str]:
    """The repos a commit set must cover: every zrm625 fork in default.xml
    and the manifest itself (the same set scripts/build records)."""
    root = ET.parse(manifest or MANIFEST_DIR / "default.xml").getroot()
    paths = {p.get("path") or p.get("name") for p in root.iter("project")
             if "zrm625-fork" in (p.get("groups") or "").split(",")}
    return paths | {MANIFEST_PATH}


def commit_set_hash(commit_set: dict) -> str:
    """The hash scripts/build computes: sha256 of the fork lines in byte
    order (LC_ALL=C sort), then the manifest line, newline-terminated."""
    forks = sorted(f"{p} {v}" for p, v in commit_set.items() if p != MANIFEST_PATH)
    text = "\n".join(forks + [f"{MANIFEST_PATH} {commit_set.get(MANIFEST_PATH, '')}"]) + "\n"
    return hashlib.sha256(text.encode()).hexdigest()[:10]


def batch_identity_errors(batch: dict, expected: set[str] | None = None) -> list[str]:
    """Why this batch.json does not tie its build to the batch (A4): the build
    ID must be "<batch>-<commit set hash>" in both partitions, never
    "unstamped", and the commit set must cover every expected repo at a
    committed SHA (no +dirty)."""
    errs = []
    bid, vid = batch.get("build_id"), batch.get("vendor_build_id")
    if not bid or bid == UNSTAMPED:
        errs.append(f"build is unstamped (build ID {bid!r})")
    if vid != bid:
        errs.append(f"vendor build ID {vid!r} differs from system {bid!r}")
    cs = batch.get("commit_set") or {}
    exp = expected if expected is not None else expected_repos()
    missing, extra = sorted(exp - set(cs)), sorted(set(cs) - exp)
    if missing:
        errs.append("commit set lacks " + ", ".join(missing))
    if extra:
        errs.append("commit set has unexpected repos " + ", ".join(extra))
    bad = sorted(p for p, v in cs.items() if not SHA_RE.match(str(v)))
    if bad:
        errs.append("not a clean commit (+dirty or malformed): " + ", ".join(bad))
    h = batch.get("commit_set_hash")
    if cs and commit_set_hash(cs) != h:
        errs.append(f"commit_set_hash {h!r} does not match the commit set ({commit_set_hash(cs)})")
    want = f"{batch.get('batch')}-{h}"
    if bid and bid != want:
        errs.append(f"build ID {bid!r} is not {want!r}")
    return errs


def stage(batch_id: str, product: Path, kind: str, experiment: bool, build_info: Path | None = None,
          expected: set[str] | None = None) -> dict:
    if kind not in KINDS:
        raise SystemExit(f"error: kind must be one of {KINDS}")
    bdir = batch_dir(batch_id)
    if (bdir / "batch.json").exists():
        raise SystemExit(f"error: {bdir} already staged")
    sys_prop, ven_prop = product / "system/build.prop", product / "vendor/build.prop"
    info = read_json(build_info or (product.parents[2] / "river-build.json"), {}) or {}
    ident = {"batch": batch_id, "build_id": read_prop(sys_prop, "ro.river.build_id"),
             "vendor_build_id": read_prop(ven_prop, "ro.vendor.river.build_id"),
             "commit_set": info.get("commit_set") or {}, "commit_set_hash": info.get("commit_set_hash")}
    if info.get("build_id") and ident["build_id"] and info["build_id"] != ident["build_id"]:
        raise SystemExit(f"error: river-build.json says {info['build_id']}, build.prop says {ident['build_id']}")
    if kind == "batch":
        errs = batch_identity_errors(ident, expected)
        if errs:
            raise SystemExit("error: refusing to stage a batch whose build is not tied to it:\n  " +
                             "\n  ".join(errs) + f"\nBuild it with: scripts/build --batch {batch_id}")
    (bdir / "images").mkdir(parents=True)
    images = {}
    for name in IMAGES:
        src = product / f"{name}.img"
        if not src.exists():
            raise SystemExit(f"error: missing {src}")
        dst = bdir / "images" / f"{name}.img"
        shutil.copyfile(src, dst)
        images[name] = {"file": f"images/{name}.img", "sha256": sha256_file(dst), "size": dst.stat().st_size}
    inst = {}
    for part, fname in (("system", "installed-files.json"), ("vendor", "installed-files-vendor.json")):
        src = product / fname
        if src.exists():
            dst = bdir / fname
            shutil.copyfile(src, dst)
            inst[part] = {"file": fname, "sha256": sha256_file(dst)}
    batch = {
        "version": 1,
        "batch": batch_id,
        "kind": kind,
        "experiment": bool(experiment),
        "build_id": ident["build_id"],
        "vendor_build_id": ident["vendor_build_id"],
        "commit_set": ident["commit_set"],
        "commit_set_hash": ident["commit_set_hash"],
        "dates": {
            "build": read_prop(sys_prop, "ro.build.date.utc"),
            "vendor": read_prop(ven_prop, "ro.vendor.build.date.utc"),
            "bootimage": read_prop(bootimage_prop_file(product), "ro.bootimage.build.date.utc"),
        },
        "images": images,
        "installed_files": inst,
        "cameras": camera_set(),
        "camera_set_change": None,
        "prs": [], "tag": None, "tag_repos": [],
        "pair": None,
        "first_boot_seconds": None,
        "bench_runs": [],
        "staged_at": now_iso(),
    }
    write_json(bdir / "batch.json", batch)
    return batch


def load(batch_id: str) -> dict:
    b = read_json(batch_dir(batch_id) / "batch.json")
    if b is None:
        raise SystemExit(f"error: no batch.json for {batch_id}")
    return b


def save(batch: dict) -> None:
    write_json(batch_dir(batch["batch"]) / "batch.json", batch)
