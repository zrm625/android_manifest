"""scripts/bench/device-bench: measure a build on the test phone."""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import time
from pathlib import Path

from . import evidence, flashing, reboots, stage
from .core import BENCH_HOME, SERIAL, batch_dir, bench_path, log, now_iso, write_json
from .device import Device, GuardError
from .lock import DeviceLock, LockError

COLUMNS = ["run_id", "env", "boot", "host_boot_seconds", "uptime_at_boot_completed", "mem_total_kb",
           "mem_available_kb", "used_ram_kb", "processes", "packages", "system_used_kb", "vendor_used_kb",
           "cpu_idle_pct", "lineage_version", "fingerprint",
           # Added for phone-qa (DESIGN.md): identity and per-boot health binding.
           "build_date_utc", "vendor_date_utc", "bootimage_date_utc", "slot", "batch", "build_id",
           "boot_id", "signals", "evidence"]


def csv_path() -> Path:
    return bench_path("results", "device.csv")


def migrate_header(path: Path) -> None:
    """Bring an old device.csv up to COLUMNS. Old rows get empty new fields;
    the old file is kept as device.csv.bak-<time>."""
    if not path.exists() or path.stat().st_size == 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(COLUMNS)
        return
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    if header == COLUMNS:
        return
    if header != COLUMNS[:len(header)]:
        raise SystemExit(f"error: {path} has an unknown header; not migrating")
    shutil.copyfile(path, path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in rows[1:]:
            w.writerow(r + [""] * (len(COLUMNS) - len(r)))
    os.replace(tmp, path)
    log(f"Migrated {path} to the new columns")


def read_rows(path: Path | None = None) -> list[dict]:
    path = path or csv_path()
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def append_row(row: dict) -> None:
    path = csv_path()
    migrate_header(path)
    with open(path, "a", newline="") as f:
        csv.writer(f).writerow([row.get(c, "") for c in COLUMNS])
        f.flush()
        os.fsync(f.fileno())


def df_used(dev: Device, mount: str) -> str:
    lines = dev.sh(f"df -k {mount}").strip().splitlines()
    return lines[1].split()[2] if len(lines) > 1 else ""


def cpu_idle(dev: Device) -> str:
    a = dev.sh("head -1 /proc/stat").split()
    time.sleep(30)
    b = dev.sh("head -1 /proc/stat").split()
    x, y = [int(v) for v in a[1:]], [int(v) for v in b[1:]]
    tot = sum(y) - sum(x)
    idle = (y[3] - x[3]) + (y[4] - x[4])
    return f"{100 * idle / tot:.1f}" if tot else ""


def used_ram_kb(dev: Device) -> str:
    for line in dev.sh("dumpsys meminfo", timeout=120).splitlines():
        if "Used RAM" in line:
            num = line.split(":", 1)[1].strip().split("K")[0].replace(",", "")
            return num.strip()
    return ""


def bench_boots_in_batch(batch_id: str) -> int:
    return sum(1 for r in read_rows() if r.get("batch") == batch_id)


USAGE = """\
Measure a build on the Moto G7 test phone, and optionally flash it first.

Usage: device-bench <baseline|stripped|current> [--flash] [--wipe] [--boots N] [--settle S]
                    [--batch ID]

  baseline|stripped  Label for the results. With --flash, also installs the images
                     from $BENCH_HOME/out-<env>/target/product/river.
  current            Measure whatever the phone runs now. Cannot be combined with --flash.
  --flash   Install the build with scripts/flash (fastboot, inactive slot).
  --wipe    Factory reset while flashing.
  --boots N   Reboot and measure N times. Default: 3.
  --settle S  Seconds to wait after boot before measuring memory and CPU. Default: 120.
  --batch ID  Measure for a staged batch (with current): the rows carry the batch
              ID, slot, build dates and build ID, and each boot gets health
              evidence in $BENCH_HOME/batches/ID/boots/<run_id>-<boot>/ plus the
              three boot signals (kernel-log chain, uptime, dropbox boot count).

Every boot records its boot_id when it sees boot completed; that boot's
evidence must carry the same boot_id. Results are appended to
$BENCH_HOME/results/device.csv (an old header is migrated to the new columns).
Only the phone with serial ZY225DPT7F is touched (fixed in the code).
"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="device-bench", usage=USAGE, add_help=False)
    ap.add_argument("env", nargs="?")
    ap.add_argument("--flash", action="store_true")
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--boots", type=int, default=3)
    ap.add_argument("--settle", type=int, default=120)
    ap.add_argument("--batch")
    ap.add_argument("-h", "--help", action="store_true")
    a = ap.parse_args(argv)
    if a.help or a.env not in ("baseline", "stripped", "current"):
        print(USAGE)
        return 0 if a.help else 1
    if a.flash and a.env == "current":
        log("error: --flash needs baseline or stripped")
        return 1
    if a.wipe and not a.flash:
        log("error: --wipe only applies with --flash")
        return 1
    if a.batch and (a.flash or a.env != "current"):
        log("error: --batch measures the flashed batch: use 'current' without --flash")
        return 1
    try:
        with DeviceLock():
            return run(a)
    except (LockError, GuardError, TimeoutError) as e:
        log(f"error: {e}")
        return 1


def run(a) -> int:
    dev = Device(SERIAL)
    dev.guard()
    if a.flash:
        product = BENCH_HOME / f"out-{a.env}" / "target/product/river"
        rc = flashing.flash_dev(product, a.wipe, True)
        if rc != 0:
            return rc
    batch = stage.load(a.batch) if a.batch else None
    migrate_header(csv_path())
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{a.env}"
    done_before = bench_boots_in_batch(a.batch) if batch else 0
    if batch:
        batch.setdefault("bench_runs", []).append(run_id)
        stage.save(batch)
    timing = reboots.load_timing()
    for i in range(1, a.boots + 1):
        log(f"Boot {i}/{a.boots}")
        dev.root()
        try:
            rec = reboots.planned_reboot(dev, "adb", boot_timeout=flashing.BOOT_TIMEOUT)
        except (TimeoutError, RuntimeError) as e:
            log(f"error: boot {i} failed: {e}")
            if batch:
                st = flashing.load_state()
                try:
                    flashing.rescue(dev, st, f"bench boot of {a.batch}: {e}")
                    return flashing.EXIT_RESCUED
                except (flashing.Halt, Exception) as h:  # noqa: BLE001 - halt on anything here
                    log(f"HALT: {h}")
                    return flashing.EXIT_HALT
            return 1
        key = f"{run_id}-{i}"
        ev_dir = batch_dir(a.batch) / "boots" / key if batch else None
        if ev_dir:
            # boot_id recorded the moment boot completed, before anything else.
            write_json(ev_dir / "completion.json", {"boot_id": rec.boot_id, "run_id": run_id, "boot": i,
                                                    "batch": a.batch, "recorded_at": now_iso(),
                                                    "uptime": rec.uptime})
        reboots.judge_signals(dev, rec, timing)
        # Same screen state for every measurement.
        dev.sh("input keyevent KEYCODE_SLEEP")
        time.sleep(a.settle)
        props = dev.sh("getprop ro.build.date.utc; getprop ro.vendor.build.date.utc; "
                       "getprop ro.bootimage.build.date.utc; getprop ro.boot.slot_suffix; "
                       "getprop ro.lineage.display.version; getprop ro.build.fingerprint").split("\n") + [""] * 6
        sys_id, _ven_id = flashing.partition_build_ids(dev)
        row = {
            "run_id": run_id, "env": a.env, "boot": i,
            "host_boot_seconds": f"{rec.host_boot_seconds:.2f}",
            "uptime_at_boot_completed": f"{rec.uptime:.2f}",
            "mem_total_kb": dev.sh("grep MemTotal /proc/meminfo").split()[1],
            "mem_available_kb": dev.sh("grep MemAvailable /proc/meminfo").split()[1],
            "used_ram_kb": used_ram_kb(dev),
            "processes": str(len(dev.sh("ps -A").strip().splitlines()) - 1),
            "packages": str(len(dev.sh("pm list packages").strip().splitlines())),
            "system_used_kb": df_used(dev, "/"), "vendor_used_kb": df_used(dev, "/vendor"),
            "cpu_idle_pct": cpu_idle(dev),
            "lineage_version": props[4].strip(), "fingerprint": props[5].strip(),
            "build_date_utc": props[0].strip(), "vendor_date_utc": props[1].strip(),
            "bootimage_date_utc": props[2].strip(), "slot": props[3].strip().lstrip("_"),
            "batch": a.batch or "", "build_id": sys_id or "", "boot_id": rec.boot_id,
        }
        if batch:
            expected = 1 + done_before + i  # the first boot after the wipe, then each bench boot
            count = reboots.read_boot_count(dev, expected)
            signals = {"path": "adb", "chain": list(rec.chain), "uptime": list(rec.uptime_check),
                       "boot_count": list(count), "reboot": rec.to_json(), "expected_boots": expected}
            evidence.collect(dev, ev_dir, expected_boot_id=rec.boot_id,
                             context={"run_id": run_id, "boot": i, "batch": a.batch, "kind": "bench"})
            write_json(ev_dir / "signals.json", signals)
            verdict = reboots.combined_verdict(signals, "adb", timing)
            row["signals"] = verdict[0]
            row["evidence"] = str(ev_dir.relative_to(BENCH_HOME))
        else:
            row["signals"] = f"chain {rec.chain[0]}; uptime {rec.uptime_check[0]}"
        append_row(row)
        log(f"  boot {rec.host_boot_seconds:.1f}s (uptime {rec.uptime:.1f}s), used RAM {row['used_ram_kb']} KB, "
            f"{row['processes']} processes, {row['packages']} packages, CPU idle {row['cpu_idle_pct']}%, "
            f"signals {row['signals']}")
    log(f"Results: {csv_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
