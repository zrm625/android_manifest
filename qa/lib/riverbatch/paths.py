"""Where the runner keeps its files, under $BENCH_HOME (R3.2, R4.1).

Every path is computed at call time from riverqa.core.BENCH_HOME, so tests
that redirect $BENCH_HOME (qa/tests/benchhome.py) redirect these too."""

from __future__ import annotations

from pathlib import Path

from riverqa import core

PKG = Path(__file__).resolve().parent
MANIFEST_DIR = core.MANIFEST_DIR
SCRIPTS = core.SCRIPTS_DIR
TOP = core.TOP


def home() -> Path:
    return core.BENCH_HOME


def runner(*parts: str) -> Path:
    return core.BENCH_HOME.joinpath("runner", *parts)


def state_file() -> Path:
    return runner("state.json")


def state_lock() -> Path:
    return runner("state.lock")


def events_file() -> Path:
    return runner("events.jsonl")


def children_dir() -> Path:
    return runner("children")


def council_dir() -> Path:
    return runner("council")


def reports_dir() -> Path:
    return runner("reports")


def config_file() -> Path:
    return runner("config.json")


def qualified_file() -> Path:
    return runner("qualified.json")


def abtest_file() -> Path:
    return runner("abtest.json")


def batch_dir(bid: str) -> Path:
    return core.BENCH_HOME / "batches" / bid


def known_good_file() -> Path:
    return core.BENCH_HOME / "qa" / "known-good.json"


def slot_state_file() -> Path:
    return core.BENCH_HOME / "slot-state.json"


def out_dir() -> Path:
    return core.BENCH_HOME / "out-stripped"


def product_dir() -> Path:
    return out_dir() / "target" / "product" / "river"


def queue_file() -> Path:
    return core.BENCH_HOME / "queue" / "approved.jsonl"


DEFAULT_CONFIG = {"wifi_ssid_file": None, "wifi_psk_file": None, "batch_size": 4, "batches_per_tick": 3,
                  "free_space_floor_gb": 60}


def config() -> dict:
    """runner/config.json over the defaults (R3.2). No secrets: the Wi-Fi
    values are file paths."""
    return {**DEFAULT_CONFIG, **(core.read_json(config_file(), {}) or {})}
