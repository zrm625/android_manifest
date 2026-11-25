"""Summarize the RAM evidence and evaluate the settled trial gate."""

import csv
import json
import sys
from pathlib import Path

MARGIN_KIB = 5 * 1024
SEED_BUILD = "try-default-nfc-restored"
SEED_KIB = 1072982


def state_record(directory: Path) -> dict:
    return dict(line.split("=", 1) for line in
                (directory / "post-setup-state.txt").read_text().splitlines())


def build_record(path: Path) -> dict:
    record = json.loads(path.read_text())
    if not isinstance(record, dict) or not isinstance(record.get("build_id"), str) or not record["build_id"]:
        raise ValueError(f"missing build identity in {path}")
    return record


def reference_kib(directory: Path) -> int:
    build = build_record(directory / "river-build.json")
    if build.get("nfc_enabled") is not True:
        raise ValueError("a RAM reference requires an NFC-enabled build record")
    state = state_record(directory)
    bid = build["build_id"]
    if state.get("build") != bid or not state.get("boot_id"):
        raise ValueError("the RAM reference does not match its build and boot evidence")
    path = directory / "trial-ram.json"
    if not path.exists() and bid == SEED_BUILD:
        if int((directory / "post-setup-used-ram-kb.txt").read_text()) == SEED_KIB:
            return SEED_KIB
    result = json.loads(path.read_text())
    values = result.get("settled_samples_kib", [])
    if (result.get("build_id") != bid or result.get("boot_id") != state["boot_id"]
            or result.get("nfc_enabled") is not True or state.get("low_ram") != "false"
            or result.get("gate") != "pass" or len(values) != 3
            or not all(type(value) is int and value > 0 for value in values)):
        raise ValueError("the RAM reference needs a passing default trial with three settled samples")
    return sorted(values)[1]


def before_setup(out: Path, bid: str) -> list[int]:
    with (out / "pre-setup-boots.csv").open() as data:
        rows = list(csv.DictReader(data))
    if (len(rows) != 3 or {row["build_id"] for row in rows} != {bid}
            or len({row["run_id"] for row in rows}) != 1
            or len({row["boot_id"] for row in rows}) != 3
            or any(not row["boot_id"] for row in rows)):
        raise ValueError("expected three distinct pre-setup boots of the same build and run")
    before = [int(row["used_ram_kb"]) for row in rows]
    if not all(value > 0 for value in before):
        raise ValueError("expected three valid pre-setup RAM samples")
    return before


def summarize(out: Path, record: Path, reference: Path) -> str:
    ref_kib = reference_kib(reference)
    build = build_record(record)
    state = state_record(out)
    bid = build["build_id"]
    if state.get("build") != bid or not state.get("boot_id"):
        raise ValueError("settled RAM does not match the flashed build and boot")
    before = before_setup(out, bid)
    samples = [int(line) for line in (out / "post-setup-ram-samples-kb.txt").read_text().splitlines()]
    if len(samples) != 3 or not all(value > 0 for value in samples):
        raise ValueError("expected three valid settled RAM samples")
    if state.get("low_ram") not in ("true", "false"):
        raise ValueError("the low-RAM mode is unknown")
    nfc = build.get("nfc_enabled")
    if nfc is not True and nfc is not False:
        raise ValueError("the trial build record must report its NFC mode")
    settled = sorted(samples)[1]
    experiment = state["low_ram"] == "true" or nfc is False
    gate = "experiment" if experiment else "pass" if settled <= ref_kib + MARGIN_KIB else "fail"
    result = {
        "build_id": bid,
        "boot_id": state["boot_id"],
        "nfc_enabled": nfc,
        "low_ram": state["low_ram"] == "true",
        "pre_setup_samples_kib": before,
        "pre_setup_median_kib": sorted(before)[1],
        "settled_samples_kib": samples,
        "settled_median_kib": settled,
        "reference_kib": ref_kib,
        "gate": gate,
    }
    (out / "trial-ram.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"Used RAM median before setup: {result['pre_setup_median_kib'] / 1024:.1f} MB")
    print(f"Used RAM median after setup: {settled / 1024:.1f} MB "
          f"(reference {ref_kib / 1024:.1f} MB, allowed +5 MB)")
    return gate


def main():
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "reference":
            print(reference_kib(Path(sys.argv[2])))
            return 0
        if len(sys.argv) == 4 and sys.argv[1] == "before":
            out, record = map(Path, sys.argv[2:])
            median_kib = sorted(before_setup(out, build_record(record)["build_id"]))[1]
            (out / "pre-setup-used-ram-kb.txt").write_text(f"{median_kib}\n")
            return 0
        out, record, reference = map(Path, sys.argv[1:])
        gate = summarize(out, record, reference)
    except (OSError, KeyError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("RESULT ram-gate=unverified")
        return 2
    print(f"RESULT ram-gate={gate}")
    return int(gate == "fail")


if __name__ == "__main__":
    sys.exit(main())
