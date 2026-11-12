"""Catch authority defaults, risk double-counting and funding-bound violations."""

import json
from decimal import Decimal as D

import pytest

from sextant.entry.parameters import EntryParameters
from sextant.entry.sizing import SegmentBasis, size_entry

from .conftest import params, registry_rows, rows


def test_fixture_values_never_supply_deployment_authority() -> None:
    with pytest.raises(ValueError, match="fixture"):
        EntryParameters.from_rows(rows())
    with pytest.raises(ValueError, match="unavailable"):
        EntryParameters.from_rows(registry_rows())
    broken = rows()
    broken[0]["source"] = ""
    with pytest.raises(ValueError, match="source"):
        EntryParameters.from_rows(broken, fixture=True)
    broken = rows()
    broken[0]["value"] = 1.5
    with pytest.raises(ValueError, match="Decimal"):
        EntryParameters.from_rows(broken, fixture=True)


def test_basis_is_frozen_durable_and_account_segment_scoped(tmp_path) -> None:
    path = tmp_path / "basis.json"
    basis = SegmentBasis(path, "fixture-account", params())
    assert basis.freeze("pre", D("10000"), first_read="segment_start") == D("10000")
    assert basis.freeze("pre", D("9000"), first_read="node_start") == D("10000")
    restarted = SegmentBasis(path, "fixture-account", params())
    assert restarted.freeze("pre", None, first_read="node_start") == D("10000")
    assert restarted.freeze("rth", None, first_read="node_start") is None
    assert restarted.freeze("rth", D("8000"), first_read="node_start") == D("8000")
    assert restarted.receipt["first_read"] == "node_start"
    with pytest.raises(ValueError, match="account"):
        SegmentBasis(path, "other-account", params())
    path.write_text("{")
    with pytest.raises(ValueError):
        SegmentBasis(path, "fixture-account", params())
    assert path.read_text() == "{"


@pytest.mark.parametrize("first_read", ["node_start", "segment_start"])
def test_zero_basis_first_read_is_durable_and_refuses_entry(
    instrument, tmp_path, first_read
) -> None:
    path = tmp_path / "basis.json"
    store = SegmentBasis(path, "fixture-account", params())
    assert store.freeze("pre", D(0), first_read=first_read) == 0
    restarted = SegmentBasis(path, "fixture-account", params())
    frozen = restarted.freeze("pre", D("10000"), first_read="node_start")
    assert frozen == 0
    result = sizing(instrument, basis=frozen, buying_power=D(0))
    assert result.quantity.as_decimal() == 0
    assert result.refusal


def test_zero_basis_receipt_loads_without_gaining_new_capital(instrument, tmp_path) -> None:
    path = tmp_path / "basis.json"
    path.write_text(
        json.dumps(
            dict(
                account="fixture-account",
                segment="pre",
                basis="0",
                first_read="node_start",
                params_hash=params().params_hash,
            )
        )
    )
    store = SegmentBasis(path, "fixture-account", params())
    frozen = store.freeze("pre", D("10000"), first_read="node_start")
    assert frozen == 0
    result = sizing(instrument, basis=frozen)
    assert result.quantity.as_decimal() == 0
    assert result.refusal


def sizing(instrument, **overrides):
    kwargs = {
        "instrument": instrument,
        "params": params(),
        "setup": "FIRST_LEG",
        "entry": D("10"),
        "initial_trigger": D("9.90"),
        "equity": D("10000"),
        "basis": D("10000"),
        "buying_power": D("10000"),
        "dollar_volume": D("100000"),
        "confidence": None,
        "prearm_stopouts": 0,
        "commission_rate": D(0),
    }
    return size_entry(**(kwargs | overrides))


def test_native_sizing_counts_chase_once_and_applies_hard_bounds(instrument) -> None:
    result = sizing(instrument)
    assert result.quantity.as_decimal() == D("100")  # $20 risk / ($.10 stop + $.10 chase).
    assert result.planned_risk == D("20")
    assert result.stop_distance == D("0.20")
    assert result.confidence_multiplier is None
    assert sizing(instrument, buying_power=D("250")).quantity.as_decimal() == D("25")
    flow = sizing(instrument, dollar_volume=D("1000"))
    assert flow.quantity.as_decimal() == D("5")
    assert flow.binding_term == "flow"


def test_confidence_and_stopout_multipliers_do_not_invent_unscored_confidence(instrument) -> None:
    result = sizing(instrument, setup="RECLAIM", confidence=D("0.5"), prearm_stopouts=1)
    assert result.quantity.as_decimal() == D("37")
    assert result.planned_risk == D("7.500")
    assert result.confidence_multiplier == D("0.75")
    assert sizing(instrument, setup="RECLAIM").quantity.as_decimal() == 0
    assert sizing(instrument, prearm_stopouts=10).quantity.as_decimal() == D("25")


@pytest.mark.parametrize(
    "override",
    [
        {"buying_power": None},
        {"equity": D(0)},
        {"basis": None},
        {"dollar_volume": None},
        {"dollar_volume": D(0)},
        {"initial_trigger": D("-1")},
        {"initial_trigger": D("10")},
        {"confidence": D("NaN")},
        {"confidence": D("1.1")},
        {"confidence": D("0.5")},
        {"entry": 10.0},
        {"prearm_stopouts": -1},
        {"buying_power": D("9.99")},
    ],
)
def test_invalid_or_missing_sizing_terms_refuse_order(instrument, override) -> None:
    result = sizing(instrument, **override)
    assert result.quantity.as_decimal() == 0
    assert result.refusal
