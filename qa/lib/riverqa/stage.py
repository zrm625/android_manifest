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
      "prs": [], "no_pr": [], "tag": null, "tag_repos": [],
                     # runner: the PRs it opened, the repo paths of commit_set
                     # with no PR (explicitly), the tag and every repo the
                     # tag goes in (all of commit_set)
      "merge_started": null | {"at": "...", "expected_prs": {"<path>": {"repo", "number" | null}}},
                     # runner: flash --merge-started, durably, before any
                     # merge or tag action. It freezes the complete PR set;
                     # recovery tells "never started" from "interrupted
                     # mid-merge" by it, and proves promotion against it
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
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from .core import MANIFEST_DIR, TOP, batch_dir, now_iso, read_json, sha256_file, write_json
from .core import run as host_run
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


def parse_camera_set(text: str | None) -> list[int]:
    """The enabled camera IDs in the switch file's text (H13). None (no
    file) is the bootstrap set. Pure, so the batch runner applies the same
    rule to a file read from GitHub."""
    if text is None:
        return list(BOOTSTRAP_CAMERAS)
    return sorted(int(x) for x in text.split() if x.strip().isdigit())


def camera_set(tree: Path = TOP) -> dict:
    f = tree / CAMERA_SWITCH_FILE
    if f.exists():
        return {"set": parse_camera_set(f.read_text()), "source": CAMERA_SWITCH_FILE}
    return {"set": parse_camera_set(None), "source": "bootstrap"}


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


def repo_slugs(manifest: Path | None = None) -> dict[str, str]:
    """GitHub owner/name for each repo path in expected_repos()."""
    root = ET.parse(manifest or MANIFEST_DIR / "default.xml").getroot()
    out = {(p.get("path") or p.get("name")): f"zrm625/{p.get('name').rsplit('/', 1)[-1]}"
           for p in root.iter("project") if "zrm625-fork" in (p.get("groups") or "").split(",")}
    out[MANIFEST_PATH] = "zrm625/android_manifest"
    return out


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
    if info.get("nfc_enabled") is False and kind != "experiment":
        raise SystemExit("error: stage an NFC-off build as an experiment")
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
        "experiment": bool(experiment) or info.get("nfc_enabled") is False,
        "nfc_enabled": info.get("nfc_enabled"),
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
        "prs": [], "no_pr": [], "tag": None, "tag_repos": [],
        "merge_started": None,
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


# --- kernel batches (review round 4) -------------------------------------------

KERNEL_PATH = "kernel/motorola/sdm632"
MANIFEST_PATH = ".repo/manifests"
# Files outside the kernel repo that change what goes into the boot image
# (R5-6): the board configs (TARGET_KERNEL_CONFIG, the kernel command line,
# BOARD_BOOT_HEADER_VERSION, mkbootimg arguments) and LineageOS's kernel
# build rules and toolchain defaults. The defconfig and every fragment named
# by TARGET_KERNEL_CONFIG resolve inside the kernel source
# (arch/<arch>/configs, vendor/lineage/build/tasks/kernel.mk), so the kernel
# commit and the embedded IKCONFIG cover them.
BOOT_INPUT_FILES = {
    "device/motorola/river": ("BoardConfig*.mk",),
    "device/motorola/sdm632-common": ("BoardConfigCommon.mk",),
    "vendor/lineage": ("config/BoardConfigKernel.mk", "build/tasks/kernel.mk"),
}
# The kernel toolchain: pinned projects in default.xml (R5-6, R6-3). Besides
# the compilers and build tools: prebuilts/tools-lineage, prebuilts/rust and
# prebuilts/clang-tools are on the kernel build's PATH (TOOLS_PATH_OVERRIDE,
# vendor/lineage config/BoardConfigKernel.mk), and external/dtc builds the
# host dtc that compiles every DTB (DTC_EXT, build/tasks/kernel.mk, set
# because BoardConfigCommon.mk has BOARD_KERNEL_SEPARATED_DTBO).
TOOLCHAIN_PREFIXES = ("prebuilts/clang/", "prebuilts/clang-tools", "prebuilts/gcc/", "prebuilts/rust",
                      "prebuilts/kernel-build-tools", "prebuilts/build-tools", "prebuilts/tools-lineage",
                      "prebuilts/misc", "external/dtc")


def git_out(repo: str, *args: str) -> str | None:
    """``git -C <tree>/<repo> ...``'s stdout, or None if it failed (an
    unknown commit, a missing repo)."""
    try:
        r = host_run(["git", "-C", str(TOP / repo), *args], timeout=60)
    except (TimeoutError, OSError):
        return None
    return r.stdout if r.returncode == 0 else None


def toolchain_pins(default_xml: str) -> dict[str, str] | None:
    """{path: revision} of the kernel toolchain projects in a default.xml,
    or None if it doesn't parse."""
    try:
        root = ET.fromstring(default_xml)
    except ET.ParseError:
        return None
    return {pr.get("path") or pr.get("name"): pr.get("revision") or ""
            for pr in root.iter("project")
            if (pr.get("path") or pr.get("name") or "").startswith(TOOLCHAIN_PREFIXES)}


def boot_input_change(mine: dict, theirs: dict, git=None) -> tuple[bool, str]:
    """Do the boot-affecting files outside the kernel repo, or the kernel
    toolchain pins, differ between two commit sets? Anything that can't be
    compared counts as a change (R5-6)."""
    git = git or git_out
    for repo, specs in BOOT_INPUT_FILES.items():
        a, b = theirs.get(repo), mine.get(repo)
        if not a or not b:
            return True, f"no {repo} commit to compare boot inputs with"
        if a == b:
            continue
        out = git(repo, "diff", "--name-only", a, b, "--", *specs)
        if out is None:
            return True, f"can't compare {repo} {a[:12]}..{b[:12]}"
        if out.strip():
            return True, f"{repo} changed {', '.join(out.split())}"
    a, b = theirs.get(MANIFEST_PATH), mine.get(MANIFEST_PATH)
    if not a or not b:
        return True, "no manifest commit to compare the kernel toolchain with"
    if a != b:
        xa, xb = git(MANIFEST_PATH, "show", f"{a}:default.xml"), git(MANIFEST_PATH, "show", f"{b}:default.xml")
        pa, pb = (toolchain_pins(xa) if xa is not None else None), (toolchain_pins(xb) if xb is not None else None)
        if pa is None or pb is None:
            return True, f"can't compare the kernel toolchain pins in default.xml {a[:12]}..{b[:12]}"
        if pa != pb:
            diff = sorted(k for k in set(pa) | set(pb) if pa.get(k) != pb.get(k))
            return True, "kernel toolchain pins changed: " + ", ".join(diff)
    return False, "no boot input or toolchain change"


# A kernel part that was read completely and is not there (CONFIG_IKCONFIG
# off, no appended DTB). It is distinct from None, "could not be parsed",
# which always counts as a kernel batch (R6-3).
ABSENT = "absent"
FDT_MAGIC = b"\xd0\x0d\xfe\xed"
FDT_HEADER = 40


def boot_info(img: Path) -> dict | None:
    """From an Android boot image with header version 0-2: the header
    version, the kernel command line (512 bytes at 64, plus the 1024-byte
    extra at 608), and from the kernel the SHA-256 of its embedded config
    (IKCONFIG) and of the DTBs appended to it (Image.gz-dtb); each of those
    is a hash, ABSENT, or None if it could not be parsed. None if the file
    is missing, not a boot image, has a header version this parser doesn't
    know, or a kernel that runs past the end of the file (R5-6, R5-17,
    R6-6: unknown fails safe to a kernel batch)."""
    try:
        data = img.read_bytes()
    except OSError:
        return None
    if len(data) < 1632 or data[:8] != b"ANDROID!":
        return None
    kernel_size, page_size, version = (struct.unpack_from("<I", data, off)[0] for off in (8, 36, 40))
    if version > 2 or page_size not in (2048, 4096, 16384) or page_size + kernel_size > len(data):
        return None
    cmdline = (data[64:576].split(b"\0", 1)[0] + data[608:1632].split(b"\0", 1)[0]).decode("latin-1")
    image, tail = split_kernel(data[page_size:page_size + kernel_size])
    return {"header_version": version, "cmdline": cmdline,
            "ikconfig_sha256": None if image is None else ikconfig_sha(image),
            "dtb_sha256": None if tail is None else appended_dtb_sha(tail)}


def split_kernel(kernel: bytes) -> tuple[bytes | None, bytes | None]:
    """(kernel image, bytes appended after it). A gzip kernel
    (Image.gz-dtb) is decompressed whole; what follows the gzip stream is
    the appended DTBs. (None, None) if the gzip stream is corrupt or cut
    short. A raw kernel is returned as is, with None for the tail: where a
    raw Image ends can't be told reliably, so its DTBs can't be compared."""
    if kernel[:2] != b"\x1f\x8b":
        return kernel, None
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        img = d.decompress(kernel)
    except zlib.error:
        return None, None
    if not d.eof:
        return None, None
    return img, d.unused_data


def ikconfig_sha(img: bytes) -> str | None:
    """SHA-256 of the kernel's embedded .config (CONFIG_IKCONFIG: gzip data
    between IKCFG_ST and the next IKCFG_ED, R6-6) in a decompressed kernel.
    ABSENT if the kernel has no IKCFG_ST; None if the block is there but
    can't be read (R6-3)."""
    s = img.find(b"IKCFG_ST")
    if s < 0:
        return ABSENT
    e = img.find(b"IKCFG_ED", s + 8)
    if e < 0:
        return None
    try:
        cfg = zlib.decompress(img[s + 8:e], 16 + zlib.MAX_WBITS)
    except zlib.error:
        return None
    return hashlib.sha256(cfg).hexdigest()


def appended_dtb_sha(tail: bytes) -> str | None:
    """SHA-256 of the DTBs appended to a gzip kernel (R6-3). The tail must
    be whole flattened device trees back to back (magic, then totalsize,
    which ends exactly at the end): ABSENT if empty, None if it isn't
    that."""
    if not tail:
        return ABSENT
    off = 0
    while off < len(tail):
        if tail[off:off + 4] != FDT_MAGIC or off + 8 > len(tail):
            return None
        size = struct.unpack_from(">I", tail, off + 4)[0]
        if size < FDT_HEADER or off + size > len(tail):
            return None
        off += size
    return hashlib.sha256(tail).hexdigest()


def boot_cmdline(img: Path) -> str | None:
    """The kernel command line of a v0-v2 boot image, or None (boot_info)."""
    info = boot_info(img)
    return None if info is None else info["cmdline"]


def kernel_change(batch: dict, known_good: dict | None, load_batch=None, info=None,
                  git=None) -> tuple[bool, str]:
    """Does this batch change the kernel or the boot image's kernel side,
    compared with the latest known-good build? (True, why) if the kernel
    commit, a boot-affecting file outside the kernel repo or the kernel
    toolchain pins (boot_input_change), the dtbo image, the boot image
    header version, its command line, the kernel's embedded config or the
    DTB appended to the kernel differs, and also when any of them can't be
    compared or parsed (unknown counts as changed; R5-6, R6-3).

    The boot image's own hash can't be used: it changes on every build (the
    ramdisk carries the build date and the kernel its build timestamp), as
    baseline-1 and baseline-2 show for the same kernel commit."""
    load_batch = load_batch or (lambda b: read_json(batch_dir(b) / "batch.json"))
    info = info or boot_info
    latest = (known_good or {}).get("latest") or {}
    cs_mine, cs_theirs = batch.get("commit_set") or {}, latest.get("commit_set") or {}
    mine, theirs = cs_mine.get(KERNEL_PATH), cs_theirs.get(KERNEL_PATH)
    if not mine or not theirs:
        return True, "no known-good kernel commit to compare with"
    if mine != theirs:
        return True, f"{KERNEL_PATH} {mine[:12]} differs from known-good {theirs[:12]}"
    changed, why = boot_input_change(cs_mine, cs_theirs, git)
    if changed:
        return True, why
    try:
        kg_batch = load_batch(latest["batch"])
    except (KeyError, OSError, ValueError, TypeError, SystemExit):
        kg_batch = None
    if not kg_batch:
        return True, "the known-good batch.json is missing, so its boot images can't be compared"
    dt_mine = ((batch.get("images") or {}).get("dtbo") or {}).get("sha256")
    dt_theirs = ((kg_batch.get("images") or {}).get("dtbo") or {}).get("sha256")
    if not dt_mine or dt_mine != dt_theirs:
        return True, "dtbo.img differs from the known-good build's (or is unknown)"
    i_mine = info(batch_dir(batch["batch"]) / "images" / "boot.img")
    i_theirs = info(batch_dir(kg_batch["batch"]) / "images" / "boot.img")
    if i_mine is None or i_theirs is None:
        return True, "a boot image is unreadable or has an unknown header version"
    if i_mine["header_version"] != i_theirs["header_version"]:
        return True, f"boot image header version {i_mine['header_version']} vs {i_theirs['header_version']}"
    if i_mine["cmdline"] != i_theirs["cmdline"]:
        return True, "the boot image command line differs from the known-good build's"
    parts = (("ikconfig_sha256", "the kernel's embedded config (IKCONFIG)"),
             ("dtb_sha256", "the DTB appended to the kernel"))
    for key, what in parts:
        # R6-3: "could not parse" is never "absent in both".
        if i_mine.get(key) is None or i_theirs.get(key) is None:
            return True, f"{what} could not be parsed in this build's or the known-good boot image"
        if i_mine[key] != i_theirs[key]:
            return True, f"{what} differs from the known-good build's (or is present in only one)"
    cfg = "IKCONFIG" if i_mine["ikconfig_sha256"] != ABSENT else "IKCONFIG confirmed absent in both"
    dtb = "appended DTB" if i_mine["dtb_sha256"] != ABSENT else "no appended DTB in either"
    return False, (f"same kernel commit {mine[:12]}, boot inputs, toolchain, dtbo, header, command line, "
                   f"kernel config ({cfg}) and {dtb} as known-good")


def sysrq_boots_required(batch: dict, known_good: dict | None, **kw) -> tuple[int, str]:
    """sysrq bench boots this batch needs (amendment A2, review round 4): 1,
    or 2 when it touches the kernel or the boot image (kernel_change)."""
    changed, why = kernel_change(batch, known_good, **kw)
    return (2, f"kernel batch: {why}") if changed else (1, why)
