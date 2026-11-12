"""Operational K2 registry acceptance (TV-REG), contract-first.

The shipped file is the DISPLAY deployment's operational registry: every
row carries a pinned UNCALIBRATED value, so an operational display Registry
builds from it. Removing any display age row refuses the build. The
trading/entry deployment's ages are not covered here and stay UNAVAILABLE
until sourced.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from sextant.ranking.registry import ROWS, Registry, RegistryRow

ROOT = Path(__file__).parents[2]
REGISTRY = ROOT / "lanes/tradingview/k2-ranking.params.json"
LANES = "seathatflowsinourveins/northstar-lanes@00203275944a22655b3e14b221cfde9a265d549a"
TRADING = "seathatflowsinourveins/northstar-trading@8256c16c31dcb8e53fc726e0c1c77dc2c8d0d4fa"
DISPLAY_ONLY = (
    "display deployment only; the trading/entry deployment's ages stay UNAVAILABLE until sourced"
)
DISPLAY_AGES = {
    "generation_max_age_s": (
        1200,
        "read_research_inputs/_envelope",
        "Research freshness 1,200s; age equal to limit refuses",
    ),
    "heartbeat_max_age_s": (
        1200,
        "read_research_inputs/_envelope",
        "Research freshness 1,200s; age equal to limit refuses",
    ),
    "input_max_age_s": (600, "research.py:_envelope", "Snapshot lag ≤600s"),
}
MOVERS = {
    "mover_min_move": ("0.03", 26),
    "mover_min_trades_accel": ("1.5", 27),
    "mover_min_dvol_ratio": ("3", 28),
    "move_1m_window_s": (60, 29),
    "move_5m_window_s": (300, 30),
    "trades_accel_window_s": (60, 31),
    "trades_accel_baseline_min": (5, 32),
    "publish_min_interval_s": (1, 33),
}


def rows() -> dict[str, dict]:
    raw = json.loads(REGISTRY.read_text())
    assert raw["schema"] == "k2-ranking.registry/1"
    return {row["name"]: row for row in raw["rows"]}


def test_operational_registry_has_exactly_the_twelve_rows():
    raw = json.loads(REGISTRY.read_text())
    names = [row["name"] for row in raw["rows"]]
    assert len(names) == len(set(names)) == 12
    assert set(names) == set(ROWS)
    for name, row in rows().items():
        assert row["unit"] == ROWS[name][1]
        assert row["calibration_state"] == "UNCALIBRATED"
        assert row["source"].strip()
        date.fromisoformat(row["last_change"])


def test_mover_rows_carry_the_pinned_moverconfig_values():
    shipped = rows()
    for name, (value, line) in MOVERS.items():
        row = shipped[name]
        kind = ROWS[name][0]
        assert row["value"] == value and "availability" not in row
        assert type(row["value"]) is (str if kind is Decimal else int)
        assert f"{LANES}:src/northstar_lanes/registry.py:{line}" in row["source"]


def test_top_n_is_the_publisher_shortlist_size():
    row = rows()["top_n"]
    assert row["value"] == 100 and "availability" not in row
    assert f"{TRADING}:src/northstar_trading/scanner/s2_tap.py:85" in row["source"]
    assert row["value"] <= 1000  # producer/ADR-0021 named-list bound


def test_display_ages_carry_the_sourced_freshness_bounds():
    shipped = rows()
    for name, (value, ref, bound) in DISPLAY_AGES.items():
        row = shipped[name]
        assert row["value"] == value and "availability" not in row
        assert type(row["value"]) is int
        assert TRADING in row["source"]
        assert ref in row["source"]
        assert bound in row["source"]
        assert DISPLAY_ONLY in row["source"]


def _registry_row(row: dict) -> RegistryRow:
    kind = ROWS[row["name"]][0]
    value = Decimal(row["value"]) if kind is Decimal else row["value"]
    return RegistryRow(
        row["name"],
        row["unit"],
        value,
        row["source"],
        row["calibration_state"],
        date.fromisoformat(row["last_change"]),
    )


def test_available_rows_construct_uncalibrated_registry_rows():
    for row in rows().values():
        assert row["value"] is not None
        built = _registry_row(row)
        assert type(built.value) is ROWS[built.name][0]
        assert built.value > 0


def test_operational_display_registry_builds_from_the_shipped_file():
    registry = Registry(tuple(_registry_row(row) for row in rows().values()))
    assert registry.input_max_age_s == 600
    assert registry.generation_max_age_s == 1200
    assert registry.heartbeat_max_age_s == 1200
    assert registry.params_hash


def test_operational_display_registry_refuses_without_each_display_age():
    shipped = rows()
    for name in DISPLAY_AGES:
        partial = tuple(_registry_row(row) for key, row in shipped.items() if key != name)
        with pytest.raises((TypeError, ValueError)):
            Registry(partial)
