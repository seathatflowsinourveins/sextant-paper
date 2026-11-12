"""Registry authority and unavailable protection reserves have runtime consequences."""

import json
from pathlib import Path

import pytest

from sextant.execution.pacer import AccountPacer, ActionKind
from sextant.risk.controls import Controls

from .conftest import control_rows

ROOT = Path(__file__).parents[3]


def test_fixture_rows_cannot_be_loaded_as_deployment_authority():
    with pytest.raises(ValueError, match="fixture"):
        Controls.from_rows(control_rows())
    rows = control_rows()
    rows[0]["source"] = ""
    with pytest.raises(ValueError, match="metadata"):
        Controls.from_rows(rows, fixture=True)


def test_sourced_registry_carries_the_structural_protection_reserve():
    rows = json.loads((ROOT / "lanes/custody-parameters.json").read_text())
    controls = Controls.from_rows(rows)
    assert controls["reserve_per_open_position"] == 2
    assert controls["reserve_per_exiting_position"] == 1
    by_name = {row["name"]: row for row in rows}
    for name in ("reserve_per_open_position", "reserve_per_exiting_position"):
        row = by_name[name]
        assert row["scope"] == "REVIEWED"
        assert row["calibration"] == "UNCALIBRATED"
        assert "protection.py" in row["source"]
        assert "ADR-0007" in row["source"]


def test_a_missing_reserve_still_never_blocks_protection():
    controls = Controls.from_rows(
        control_rows(reserve_per_open_position=None, reserve_per_exiting_position=None),
        fixture=True,
    )
    assert controls["reserve_per_open_position"] is None
    pacer = AccountPacer(controls, lambda: 0)
    sends = []
    pacer.request(ActionKind.SUBMIT, lambda mark: (mark(), sends.append("entry")), open_positions=1)
    pacer.request(
        ActionKind.REDUCE, lambda mark: (mark(), sends.append("protection")), open_positions=1
    )
    assert sends == ["protection"]


def test_a_twenty_slot_lane_is_refused_at_the_current_burst():
    with pytest.raises(ValueError, match="entry-reserve-exceeds-burst"):
        Controls.from_rows(control_rows(reserve_per_open_position=2), fixture=True, slots=20)


def test_a_five_slot_lane_loads_at_the_current_burst():
    controls = Controls.from_rows(control_rows(reserve_per_open_position=2), fixture=True, slots=5)
    assert controls.burst == 18
