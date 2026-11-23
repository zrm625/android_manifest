"""Result states, the canonical required-check list, exit codes and the merge
gate. Pure logic: no adb, no files."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

PASS, FAIL, UNVERIFIED, INFO = "PASS", "FAIL", "UNVERIFIED", "INFO"
STATES = (PASS, FAIL, UNVERIFIED, INFO)

# Groups in run order, and the checks in each. Every ID here is required in a
# full run; a run passes only if each one is PASS, so a harness bug that drops
# a check cannot pass. INFO results never stand in for a required ID.
GROUPS: dict[str, list[str]] = {
    "preflight": ["preflight.lock", "preflight.guards", "preflight.controls", "preflight.fixtures",
                  "preflight.journal", "preflight.helper", "preflight.snapshot", "preflight.continuity"],
    "identity": ["identity.build_id", "identity.boot_hash", "identity.props", "identity.low_ram",
                 "identity.bench_rows", "identity.image_hashes"],
    "health": ["health.boot", "health.selinux", "health.boot_evidence", "health.boot_signals",
               "health.dropbox", "health.tombstones", "health.pstore", "health.services",
               "health.telephony", "health.regression"],
    "radios": ["radios.wifi", "radios.bluetooth", "radios.nfc", "radios.gps"],
    "hardware": ["hardware.cameras", "hardware.audio", "hardware.fingerprint", "hardware.sensors",
                 "hardware.vibration", "hardware.screen", "hardware.battery", "hardware.usb"],
    "system": ["system.gestures", "system.alarms", "system.keyboard", "system.sideload",
               "system.webview", "system.video", "system.time"],
    "apps": ["apps.brave", "apps.tailscale", "apps.t3code"],
    "cleanup": ["cleanup.verified"],
    "final": ["final.health", "final.processes", "final.hal_deaths", "final.boot_id", "final.settings"],
}
REQUIRED: list[str] = [c for checks in GROUPS.values() for c in checks]

# Checks run by --baseline: no functional checks, so it works on Trebuchet.
BASELINE_REQUIRED = (GROUPS["preflight"][:3] + ["preflight.journal", "preflight.continuity"] + GROUPS["identity"] +
                     [c for c in GROUPS["health"]])
# Checks run by --quick: the health group on the live phone only.
QUICK_REQUIRED = ["preflight.guards", "health.boot", "health.selinux", "health.crashes",
                  "health.tombstones", "health.services", "health.telephony"]

# Checks whose helper test must reach a terminal result for eligibility.
HELPER_CHECKS = {"radios.wifi", "radios.bluetooth", "radios.gps", "hardware.cameras",
                 "hardware.audio", "hardware.fingerprint", "hardware.sensors",
                 "hardware.vibration", "hardware.screen", "system.gestures", "system.alarms",
                 "system.keyboard", "system.webview", "system.video"}


def group_of(check_id: str) -> str:
    return check_id.split(".", 1)[0]


@dataclass
class Result:
    id: str
    state: str
    summary: str
    details: dict = field(default_factory=dict)
    attempts: int = 1
    seconds: float = 0.0
    helper_terminal: bool | None = None   # None: no helper involved

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError(f"bad state {self.state!r}")

    def to_json(self):
        return asdict(self)


def worst(states) -> str:
    """FAIL beats UNVERIFIED beats PASS. INFO is ignored. Empty: UNVERIFIED."""
    states = [s for s in states if s != INFO]
    if not states:
        return UNVERIFIED
    if FAIL in states:
        return FAIL
    if UNVERIFIED in states:
        return UNVERIFIED
    return PASS


def fill_missing(results: dict[str, Result], required: list[str]) -> dict[str, Result]:
    """Every required ID that produced no result is UNVERIFIED."""
    out = dict(results)
    for cid in required:
        if cid not in out:
            out[cid] = Result(cid, UNVERIFIED, "no result (check did not run)")
    return out


def exit_code(results: dict[str, Result], required: list[str]) -> int:
    """1 if any check FAILed (wins over UNVERIFIED), else 2 if any required
    check is not PASS, else 0."""
    states = [r.state for r in results.values()]
    if FAIL in states:
        return 1
    for cid in required:
        r = results.get(cid)
        if r is None or r.state != PASS:
            return 2
    if UNVERIFIED in states:
        return 2
    return 0


CALIBRATION_RUNS_NEEDED = 5


@dataclass
class GateInputs:
    mode: str                      # batch, quick, calibrate, baseline, control
    full_set: bool                 # no --only / --skip
    experiment: bool | None        # from batch.json; None if unknown
    low_ram: str | None            # phone's ro.config.low_ram; None if the read failed
    controls_qualified: bool
    controls_reason: str = ""
    batch_id: str | None = None
    kind: str | None = None        # batch.json kind; only "batch" can merge
    thresholds_calibrated: bool = False
    calibration_runs: int = 0      # clean --calibrate runs for this harness version
    camera_set_change: bool = False  # batch.json has camera_set_change (A7: Zak merges by hand)


def eligibility(results: dict[str, Result], g: GateInputs) -> tuple[bool, list[str]]:
    """Merge eligibility. Returns (eligible, reasons it is not)."""
    reasons = []
    if g.mode != "batch":
        reasons.append(f"mode is {g.mode}, not batch")
    if not g.full_set:
        reasons.append("not the full check set (--only/--skip)")
    if g.experiment is None:
        reasons.append("batch.json experiment flag unknown")
    elif g.experiment:
        reasons.append("experiment batch")
    if g.kind != "batch":
        reasons.append(f"batch kind is {g.kind!r}, not 'batch'")
    if g.camera_set_change:
        # A7: the camera set is a must-work item, and removing one needs
        # Zak. The approval check stays in the report as evidence only.
        reasons.append("camera set change: Zak merges by hand")
    low = parse_bool(g.low_ram)
    if g.low_ram is None:
        reasons.append("ro.config.low_ram could not be read")
    elif low is None:
        reasons.append(f"ro.config.low_ram={g.low_ram.strip()!r} is not a boolean")
    elif low:
        reasons.append("ro.config.low_ram is enabled")
    if not g.thresholds_calibrated:
        reasons.append("thresholds.json is not calibrated")
    if g.calibration_runs < CALIBRATION_RUNS_NEEDED:
        reasons.append(f"{g.calibration_runs}/{CALIBRATION_RUNS_NEEDED} clean --calibrate runs for this harness version")
    if not g.controls_qualified:
        reasons.append("controls not qualified" + (f" ({g.controls_reason})" if g.controls_reason else ""))
    for cid in REQUIRED:
        r = results.get(cid)
        if r is None:
            reasons.append(f"{cid}: missing")
        elif r.state != PASS:
            reasons.append(f"{cid}: {r.state}")
    for cid in HELPER_CHECKS:
        r = results.get(cid)
        if r is not None and r.helper_terminal is not True:
            reasons.append(f"{cid}: helper returned no terminal result")
    for key, label in (("identity.build_id", "build identity"), ("identity.boot_hash", "boot hash"),
                       ("final.boot_id", "boot_id unchanged"), ("cleanup.verified", "cleanup"),
                       ("final.health", "final checks")):
        r = results.get(key)
        if r is None or r.state != PASS:
            reasons.append(f"{label} not proven")
    # De-duplicate while keeping order.
    seen, uniq = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return (not uniq, uniq)


# Android's boolean property values (system/libbase/parsebool.cpp).
_TRUE = ("1", "y", "yes", "on", "true")
_FALSE = ("0", "n", "no", "off", "false")


def parse_bool(value: str | None) -> bool | None:
    """True or False the way Android's GetBoolProperty reads it; an unset
    property ("") is False. None for a failed read or an unparseable value."""
    if value is None:
        return None
    v = value.strip()
    if v == "" or v in _FALSE:
        return False
    if v in _TRUE:
        return True
    return None


def low_ram_check(low_ram: str | None, experiment: bool | None) -> tuple[str, str]:
    """ro.config.low_ram enabled while experiment is false is a FAIL. A failed
    read (None) is not the same as an unset property, and is UNVERIFIED."""
    if low_ram is None:
        return UNVERIFIED, "cannot read ro.config.low_ram"
    on = parse_bool(low_ram)
    if on is None:
        return UNVERIFIED, f"ro.config.low_ram={low_ram.strip()!r} is not a boolean Android understands"
    if experiment is None:
        return UNVERIFIED, "batch.json has no experiment flag"
    if on and not experiment:
        return FAIL, f"ro.config.low_ram={low_ram.strip()} (enabled) but batch.json says experiment: false"
    if on:
        return PASS, "low-RAM experiment (never merge-eligible)"
    return PASS, f"ro.config.low_ram={low_ram.strip() or 'unset'}"
