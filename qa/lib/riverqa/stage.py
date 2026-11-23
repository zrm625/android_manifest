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
      "bench_runs": [],                     # run IDs, written by device-bench
      "staged_at": "..."
    }
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .core import TOP, batch_dir, now_iso, read_json, sha256_file, write_json
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


def stage(batch_id: str, product: Path, kind: str, experiment: bool, build_info: Path | None = None) -> dict:
    if kind not in KINDS:
        raise SystemExit(f"error: kind must be one of {KINDS}")
    bdir = batch_dir(batch_id)
    if (bdir / "batch.json").exists():
        raise SystemExit(f"error: {bdir} already staged")
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
    sys_prop, ven_prop = product / "system/build.prop", product / "vendor/build.prop"
    info = read_json(build_info or (product.parents[2] / "river-build.json"), {}) or {}
    batch = {
        "version": 1,
        "batch": batch_id,
        "kind": kind,
        "experiment": bool(experiment),
        "build_id": read_prop(sys_prop, "ro.river.build_id"),
        "vendor_build_id": read_prop(ven_prop, "ro.vendor.river.build_id"),
        "commit_set": info.get("commit_set") or {},
        "commit_set_hash": info.get("commit_set_hash"),
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
    if info.get("build_id") and batch["build_id"] and info["build_id"] != batch["build_id"]:
        raise SystemExit(f"error: river-build.json says {info['build_id']}, build.prop says {batch['build_id']}")
    write_json(bdir / "batch.json", batch)
    return batch


def load(batch_id: str) -> dict:
    b = read_json(batch_dir(batch_id) / "batch.json")
    if b is None:
        raise SystemExit(f"error: no batch.json for {batch_id}")
    return b


def save(batch: dict) -> None:
    write_json(batch_dir(batch["batch"]) / "batch.json", batch)
