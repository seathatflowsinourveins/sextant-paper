"""C reads its entry-size rows through a narrow load; the full load still refuses."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from sextant.entry.parameters import FIRST_LEG, SIZING, EntryParameters

ROOT = Path(__file__).parents[3]


def registry_rows() -> list[dict]:
    return json.loads((ROOT / "lanes/sim/entry-parameters.json").read_text())


def test_sizing_only_loads_the_first_leg_and_sizing_rows() -> None:
    params = EntryParameters.from_rows(registry_rows(), sizing_only=True)

    assert params.sizing_only
    assert not params.first_leg_only
    assert set(params.values) == FIRST_LEG | SIZING
    assert params["bp_target_fraction"] == Decimal("1")
    assert params["stop_ref_fraction"] == Decimal("0.010")
    assert params["portfolio_slots"] == 5
    assert params["stopout_size_decay"] == Decimal("1")
    assert params["stopout_size_floor"] == Decimal("1")
    assert params["confidence_size_floor"] == Decimal("1")
    assert params["chase_cap_fraction"] == Decimal("0.03")
    assert params["flow_share_cap"] == Decimal("0.05")
    assert params.ns("flow_share_window_s") == 300 * 1_000_000_000


def test_sizing_only_has_its_own_digest_and_first_leg_only_keeps_its_own() -> None:
    first_leg = EntryParameters.from_rows(registry_rows(), first_leg_only=True)
    sizing = EntryParameters.from_rows(registry_rows(), sizing_only=True)

    assert first_leg.params_hash != sizing.params_hash


def test_the_two_narrow_modes_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        EntryParameters.from_rows(registry_rows(), first_leg_only=True, sizing_only=True)


def test_the_full_load_still_refuses_an_unavailable_row() -> None:
    with pytest.raises(ValueError, match="unavailable"):
        EntryParameters.from_rows(registry_rows())
