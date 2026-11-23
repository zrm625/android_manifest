"""The charter's regression rules and known-good.json. Pure logic.

known-good.json has two separate records:

- ``latest``: replaced at every clean tag. The tag, the commit set, the run
  IDs, host_boot_seconds, system_used_kb, vendor_used_kb and the camera set.
- ``ram_reference``: a running minimum. The tag, the run IDs, the 3-boot
  median (MB) and ``seed``. It changes only when a clean tag has a 3-boot
  median strictly lower than the stored one. The calibration seed is retired
  at the first real tag: the reference becomes the lower of the two medians,
  with that value's provenance, and ``seed`` is cleared either way.

MB is used_ram_kb / 1024 throughout.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .results import FAIL, PASS, UNVERIFIED, worst

BOOT_LIMIT_S = 55.0
RAM_SINGLE_MARGIN_MB = 10.0
RAM_MEDIAN_MARGIN_MB = 5.0
RAM_ALWAYS_3_AT_MB = 1050.0


def mb(used_ram_kb) -> float:
    return float(used_ram_kb) / 1024.0


def median3(values_mb: list[float]) -> float | None:
    return statistics.median(values_mb) if len(values_mb) == 3 else None


def needs_three_boots(boot1_mb: float, known_good: dict | None, sizes_grew: bool) -> tuple[bool, list[str]]:
    """The four triggers for bench boots 2-3 (the runner decides before setup)."""
    why = []
    ref = (known_good or {}).get("ram_reference")
    if not ref or ref.get("median_mb") is None:
        why.append("no RAM reference yet")
    else:
        if ref.get("seed"):
            why.append("RAM reference is still the calibration seed")
        if boot1_mb > ref["median_mb"] + RAM_SINGLE_MARGIN_MB:
            why.append(f"boot 1 {boot1_mb:.1f} MB > reference {ref['median_mb']:.1f} + 10 MB")
    if boot1_mb <= RAM_ALWAYS_3_AT_MB:
        why.append(f"boot 1 {boot1_mb:.1f} MB <= 1,050 MB")
    if sizes_grew:
        why.append("/system or /vendor grew (RAM exception may be claimed)")
    return bool(why), why


def sizes_grew(system_kb: int, vendor_kb: int, known_good: dict | None) -> bool:
    latest = (known_good or {}).get("latest") or {}
    if "system_used_kb" not in latest or "vendor_used_kb" not in latest:
        return False
    return int(system_kb) > int(latest["system_used_kb"]) or int(vendor_kb) > int(latest["vendor_used_kb"])


@dataclass
class Verdict:
    state: str
    summary: str
    details: dict


def evaluate(boot_rows: list[dict], known_good: dict | None) -> Verdict:
    """Apply the charter to the batch's bench rows (boot 1 first, in order)."""
    details: dict = {}
    parts: list[tuple[str, str]] = []
    if not boot_rows:
        return Verdict(UNVERIFIED, "no benchmark rows for this batch", details)
    b1 = boot_rows[0]
    boot1_mb = mb(b1["used_ram_kb"])
    details["boot1_host_boot_seconds"] = float(b1["host_boot_seconds"])
    details["boot1_mb"] = round(boot1_mb, 1)
    rams = [mb(r["used_ram_kb"]) for r in boot_rows[:3]]
    med = median3(rams)
    details["median_mb"] = None if med is None else round(med, 1)

    # Boot time: bench boot 1 only.
    secs = float(b1["host_boot_seconds"])
    parts.append((PASS if secs <= BOOT_LIMIT_S else FAIL, f"boot 1 {secs:.1f} s (limit {BOOT_LIMIT_S:.0f} s)"))

    ref = (known_good or {}).get("ram_reference")
    latest = (known_good or {}).get("latest")
    sys_kb, ven_kb = int(b1["system_used_kb"]), int(b1["vendor_used_kb"])
    grew = sizes_grew(sys_kb, ven_kb, known_good)
    required, why = needs_three_boots(boot1_mb, known_good, grew)
    details["three_boots_required"] = required
    details["three_boots_why"] = why

    if not ref or ref.get("median_mb") is None:
        parts.append((UNVERIFIED, "no RAM reference"))
    else:
        r = float(ref["median_mb"])
        details["reference_mb"] = r
        if med is not None:
            ok = med <= r + RAM_MEDIAN_MARGIN_MB
            parts.append((PASS if ok else FAIL, f"RAM median {med:.1f} MB vs reference {r:.1f} + 5"))
        elif required:
            parts.append((UNVERIFIED, "3-boot median required but missing (" + "; ".join(why) + ")"))
        else:
            ok = boot1_mb <= r + RAM_SINGLE_MARGIN_MB
            parts.append((PASS if ok else FAIL, f"RAM boot 1 {boot1_mb:.1f} MB vs reference {r:.1f} + 10"))

    if not latest or "system_used_kb" not in latest:
        parts.append((UNVERIFIED, "no latest known-good sizes"))
    elif grew and med is None:
        parts.append((UNVERIFIED, "/system or /vendor grew; the 3-boot median needed to claim the RAM "
                      "exception is missing"))
    elif grew:
        below = ref is not None and med < float(ref["median_mb"])
        parts.append((PASS if below else FAIL,
                      f"/system {sys_kb} KB vs {latest['system_used_kb']}, /vendor {ven_kb} KB vs "
                      f"{latest['vendor_used_kb']}: grew" + (", allowed: RAM median below reference" if below else "")))
    else:
        parts.append((PASS, "/system and /vendor did not grow"))

    state = worst([p[0] for p in parts])
    return Verdict(state, "; ".join(p[1] for p in parts), details)


def baseline_record(tag: str | None, batch_id: str, commit_set: dict, run_ids: list[str],
                    boot_rows: list[dict], cameras: list[int]) -> dict:
    """known-good.json from the --baseline run: latest plus the seeded reference."""
    rams = [mb(r["used_ram_kb"]) for r in boot_rows[:3]]
    med = median3(rams)
    if med is None:
        raise ValueError("the baseline needs exactly 3 measured boots")
    b1 = boot_rows[0]
    seed = {"batch": batch_id, "run_ids": run_ids, "median_mb": round(med, 3)}
    return {
        # Immutable: the harness version hashes this, never ram_reference,
        # so retiring the seed at the first tag doesn't change the version.
        "calibration_seed": seed,
        "latest": {
            "tag": tag, "batch": batch_id, "commit_set": commit_set, "run_ids": run_ids,
            "host_boot_seconds": float(b1["host_boot_seconds"]),
            "system_used_kb": int(b1["system_used_kb"]), "vendor_used_kb": int(b1["vendor_used_kb"]),
            "cameras": sorted(cameras),
        },
        "ram_reference": {"tag": tag, "batch": batch_id, "run_ids": run_ids,
                          "median_mb": round(med, 3), "seed": "calibration"},
    }


def update_on_tag(known_good: dict, tag: str, batch_id: str, commit_set: dict, run_ids: list[str],
                  boot_rows: list[dict], cameras: list[int]) -> dict:
    """known-good.json after a clean tag (the tester calls this)."""
    rams = [mb(r["used_ram_kb"]) for r in boot_rows[:3]]
    med = median3(rams)
    b1 = boot_rows[0]
    out = dict(known_good)
    out["latest"] = {
        "tag": tag, "batch": batch_id, "commit_set": commit_set, "run_ids": run_ids,
        "host_boot_seconds": float(b1["host_boot_seconds"]),
        "system_used_kb": int(b1["system_used_kb"]), "vendor_used_kb": int(b1["vendor_used_kb"]),
        "cameras": sorted(cameras),
    }
    ref = dict(known_good.get("ram_reference") or {})
    new = {"tag": tag, "batch": batch_id, "run_ids": run_ids,
           "median_mb": None if med is None else round(med, 3), "seed": None}
    if med is None:
        out["ram_reference"] = ref  # one boot never changes it
    elif not ref or ref.get("median_mb") is None:
        out["ram_reference"] = new
    elif ref.get("seed"):
        # Retire the seed; keep the lower value and its provenance.
        keep = dict(ref) if float(ref["median_mb"]) <= med else new
        keep["seed"] = None
        out["ram_reference"] = keep
    elif med < float(ref["median_mb"]):
        out["ram_reference"] = new
    else:
        out["ram_reference"] = ref
    return out
