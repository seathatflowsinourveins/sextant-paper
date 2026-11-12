"""LEAD-1452 part 4: B's opt-in sizing dependencies; all paths/accounts are fixtures."""

import importlib
import json
from dataclasses import fields, replace
from decimal import Decimal as D
from typing import Any

import pytest
from test_b_lifecycle_entry import PARAMS, house, run_entry

from sextant.entry.sizing import SegmentBasis, size_entry
from sextant.exits.policy import ExitParams, buffer
from sextant.paper import node_order
from sextant.paper.b_lifecycle import BLifecycle

from .test_b_order_node import ROOT, argv


def dependencies():
    return importlib.import_module("sextant.paper.b_entry_dependencies")


def exit_rows():
    return json.loads((ROOT / "lanes/paper/b-gain-lock.params.json").read_text())


def fee_rows():
    return json.loads((ROOT / "lanes/paper/shared-node.params.json").read_text())


def state_file(tmp_path):
    home = tmp_path / "state"
    home.mkdir(exist_ok=True)
    path = tmp_path / "entry-state.params"
    rows = [
        dict(
            name=name,
            value=str(value),
            scope="alpaca-paper-b",
            unit="path",
            source="TEST ONLY approved fixture state location; SegmentBasis contract",
            last_change="2026-11-11",
        )
        for name, value in (("basis_state_home", home), ("basis_path", home / "basis.json"))
    ]
    path.write_text(json.dumps(rows))
    return path, rows


def test_b_maps_its_own_exit_rows_and_never_turns_on_chase():
    params = dependencies().load_exit_params(exit_rows())
    assert isinstance(params, ExitParams)
    assert params.prearm_buffer_fraction == D("0.01")
    assert params.prearm_range_multiple == D("1")
    assert params.prearm_range_window_s == D("10")
    assert params.breach_persist_count == 3
    assert params.quote_max_age_s == D("1")
    assert params.chase_cap_fraction is None and params.chase_min_interval_s is None
    assert params.chase_on is False
    assert all(
        type(getattr(params, f.name)) is D
        for f in fields(params)
        if f.name not in {"breach_persist_count", "chase_cap_fraction", "chase_min_interval_s"}
    )
    changed = [
        r | {"value": "0.02"} if r["name"] == "prearm_buffer_fraction" else r for r in exit_rows()
    ]
    assert dependencies().load_exit_params(changed).prearm_buffer_fraction == D("0.02")


@pytest.mark.parametrize(
    "name", [f.name for f in fields(ExitParams) if not f.name.startswith("chase_")]
)
@pytest.mark.parametrize("damage", ["absent", "source", "scope", "value"])
def test_each_required_exit_input_names_itself_and_refuses(name, damage):
    rows = exit_rows()
    if damage == "absent":
        rows = [r for r in rows if r["name"] != name]
    else:
        value = None if damage == "value" else ""
        rows = [r | {damage: value} if r["name"] == name else r for r in rows]
    with pytest.raises(ValueError, match=name):
        dependencies().load_exit_params(rows)


@pytest.mark.parametrize("name", ["round_trip_cost", "chase_min_interval_s"])
def test_unavailable_cost_and_exit_interval_cannot_be_silently_enabled(name):
    rows = [
        r | {"value": "0.002", "calibration_state": "UNCALIBRATED"} if r["name"] == name else r
        for r in exit_rows()
    ]
    with pytest.raises(ValueError, match=name):
        dependencies().load_exit_params(rows)


@pytest.mark.parametrize("damage", ["duplicate", "float", "bool", "nonfinite", "extra"])
def test_exit_loader_refuses_malformed_authority(damage):
    rows = exit_rows()
    name = "prearm_buffer_fraction"
    if damage == "duplicate":
        rows.append(rows[0])
    elif damage == "extra":
        rows.append(rows[0] | {"name": "chase_cap_fraction", "value": "0.0020"})
        name = "chase_cap_fraction"
    else:
        value = {"float": 0.01, "bool": True, "nonfinite": "NaN"}[damage]
        rows[0] = rows[0] | {"value": value}
    with pytest.raises((ValueError, TypeError), match=name):
        dependencies().load_exit_params(rows)


def test_zero_paper_fees_derive_a_rate_and_reach_the_native_sizer():
    rate = dependencies().sizing_commission_rate(fee_rows())
    assert type(rate) is D and rate == D("0")
    from test_b_lifecycle import EQUITY

    result = size_entry(
        instrument=EQUITY,
        params=PARAMS,
        setup="FIRST_LEG",
        entry=D("0.51"),
        initial_trigger=D("0.50"),
        equity=D("100000"),
        basis=D("100000"),
        buying_power=D("1000"),
        dollar_volume=D("1000"),
        confidence=None,
        prearm_stopouts=0,
        commission_rate=rate,
    )
    assert result.refusal is None and result.quantity.as_decimal() == D("98")


@pytest.mark.parametrize(
    "name", ["paper_us_equity_commission_per_fill", "paper_us_equity_regulatory_fees_per_fill"]
)
@pytest.mark.parametrize("damage", ["absent", "source", "account_scope", "nonzero"])
def test_fee_authority_and_units_are_never_assumed(name, damage):
    rows = fee_rows()
    if damage == "absent":
        rows = [r for r in rows if r["name"] != name]
    else:
        key, value = ("value", "0.01") if damage == "nonzero" else (damage, "")
        rows = [r | {key: value} if r["name"] == name else r for r in rows]
    with pytest.raises((ValueError, TypeError), match=name):
        dependencies().sizing_commission_rate(rows)


def test_basis_path_is_durable_and_reused_for_the_same_segment(tmp_path):
    file, _ = state_file(tmp_path)
    path = dependencies().load_basis_path(file)
    assert path == tmp_path / "state/basis.json" and not path.exists()
    store = SegmentBasis(path, "FIXTURE", PARAMS)
    assert store.freeze("fixture:PRE:1", D("100"), first_read="node_start") == D("100")
    original = path.read_bytes()
    second = SegmentBasis(dependencies().load_basis_path(file), "FIXTURE", PARAMS)
    assert second.freeze("fixture:PRE:1", D("900"), first_read="node_start") == D("100")
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="account mismatch"):
        SegmentBasis(path, "OTHER", PARAMS)
    changed = replace(PARAMS, params_hash="other")
    with pytest.raises(ValueError, match="parameters changed"):
        SegmentBasis(path, "FIXTURE", changed).freeze(
            "fixture:PRE:1", D("900"), first_read="node_start"
        )


@pytest.mark.parametrize(
    "damage",
    ["missing", "relative", "escape", "symlink", "directory", "source", "scope", "home", "parent"],
)
def test_basis_path_refuses_missing_or_unapproved_locations(tmp_path, damage):
    file, rows = state_file(tmp_path)
    basis = tmp_path / "state/basis.json"
    if damage == "missing":
        rows = rows[:1]
    elif damage in {"source", "scope"}:
        rows[1][damage] = ""
    elif damage == "home":
        rows[0]["value"] = str(tmp_path / "absent")
    elif damage == "parent":
        rows[1]["value"] = str(basis.parent / "absent/basis.json")
    elif damage == "directory":
        basis.mkdir()
    elif damage == "symlink":
        basis.symlink_to(tmp_path / "outside.json")
    else:
        rows[1]["value"] = "basis.json" if damage == "relative" else str(tmp_path / "outside")
    file.write_text(json.dumps(rows))
    with pytest.raises((ValueError, OSError), match="basis_"):
        dependencies().load_basis_path(file)


def test_basis_path_refuses_an_unwritable_parent(tmp_path, monkeypatch):
    file, _ = state_file(tmp_path)

    # The OS boundary only: run the actual validation but deny the write probe.
    def denied(*args, **kwargs):
        raise PermissionError("fixture read-only filesystem")

    monkeypatch.setattr(dependencies().tempfile, "TemporaryFile", denied)
    with pytest.raises(ValueError, match=r"basis_path.*writ"):
        dependencies().load_basis_path(file)


def test_node_opt_in_maps_the_three_dependencies_and_entry_chase_stays_separate(tmp_path):
    file, _ = state_file(tmp_path)
    args = node_order.parse([*argv(tmp_path), "--entry-state-parameters", str(file)])
    args = replace(args, universe=(__import__("test_b_lifecycle").PENNY,))
    # V1's future sourced entry value is a fixture mutation, never an exit input.
    from sextant.entry.parameters import EntryParameters

    rows = json.loads((ROOT / "lanes/paper/b-entry-parameters.json").read_text())
    rows = [
        r | {"value": "0.0020", "scope": "FIXTURE_ONLY", "calibration": "UNCALIBRATED"}
        if r["name"] == "chase_cap_fraction"
        else r
        for r in rows
    ]
    entry = EntryParameters.from_rows(rows, fixture=True, sizing_only=True)
    args = replace(args, rows=replace(args.rows, entry=entry))
    config = node_order.strategy_config(args, consumer=None, journal=None)
    assert config.exit_params is not None
    assert config.exit_params.chase_cap_fraction is None
    assert config.exit_params.chase_min_interval_s is None
    assert config.commission_rate == D("0")
    assert config.basis_path == tmp_path / "state/basis.json"
    assert BLifecycle(config).missing == ["ranking_consumer", "baseline"]
    assert "round_trip_cost" in args.rows.steps["gain_lock"]


def test_node_default_path_keeps_all_three_dependencies_missing(tmp_path):
    args = node_order.parse(argv(tmp_path))
    args = replace(args, universe=(__import__("test_b_lifecycle").PENNY,))
    config = node_order.strategy_config(args, consumer=None, journal=None)
    assert config.exit_params is config.commission_rate is config.basis_path is None
    assert {"exit_params", "commission_rate", "basis_path"} <= set(BLifecycle(config).missing)
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("name", ["exit_params", "commission_rate", "basis_path"])
def test_each_absent_dependency_refuses_the_native_entry_replay(tmp_path, name):
    journal, orders, strategy = run_entry(tmp_path, **{name: None})
    (row,) = [r for r in journal if r["kind"] == "entry_refused"]
    assert row["missing"] == [name] == strategy.missing
    assert house(orders) == [] and strategy.sent == []


@pytest.mark.parametrize("name", ["basis", "commission_rate"])
def test_missing_sizing_inputs_never_receive_a_pretend_balance_or_rate(name):
    from test_b_lifecycle import EQUITY

    kwargs: dict[str, Any] = dict(
        instrument=EQUITY,
        params=PARAMS,
        setup="FIRST_LEG",
        entry=D("0.51"),
        initial_trigger=D("0.50"),
        equity=D("100000"),
        basis=D("100000"),
        buying_power=D("1000"),
        dollar_volume=D("1000"),
        confidence=None,
        prearm_stopouts=0,
        commission_rate=D("0"),
    )
    kwargs[name] = None
    result = size_entry(**kwargs)
    assert result.refusal is not None
    assert name in result.refusal and result.quantity.as_decimal() == 0


def test_missing_exit_range_refuses_the_buffer():
    params = dependencies().load_exit_params(exit_rows())
    assert buffer(D("0.01"), D("0.02"), D("10"), None, params) is None


def test_mapped_dependencies_reach_the_native_entry_and_durable_basis(tmp_path):
    file, _ = state_file(tmp_path)
    path = dependencies().load_basis_path(file)
    journal, orders, strategy = run_entry(
        tmp_path,
        exit_params=dependencies().load_exit_params(exit_rows()),
        commission_rate=dependencies().sizing_commission_rate(fee_rows()),
        basis_path=path,
    )
    assert strategy.missing == [] and len(house(orders)) == 1
    receipt = json.loads(path.read_text())
    assert D(receipt["basis"]) == D("100000") and receipt["first_read"] == "node_start"
    assert receipt["params_hash"] == PARAMS.params_hash
    assert any(row["kind"] == "submit" for row in journal)


@pytest.mark.parametrize("dependency", ["exit_params", "commission_rate", "basis_path"])
def test_node_refuses_a_supplied_dependency_without_source(tmp_path, dependency):
    file, rows = state_file(tmp_path)
    cli = [*argv(tmp_path), "--entry-state-parameters", str(file)]
    if dependency == "basis_path":
        rows[1]["source"] = ""
        file.write_text(json.dumps(rows))
    else:
        rows = exit_rows() if dependency == "exit_params" else fee_rows()
        name = (
            "prearm_buffer_fraction"
            if dependency == "exit_params"
            else "paper_us_equity_commission_per_fill"
        )
        rows = [r | {"source": ""} if r["name"] == name else r for r in rows]
        altered = tmp_path / "altered.json"
        altered.write_text(json.dumps(rows))
        flag = "--exit-parameters" if dependency == "exit_params" else "--node-parameters"
        cli[cli.index(flag) + 1] = str(altered)
    with pytest.raises(node_order.OrderModeRefused) as error:
        node_order.parse(cli)
    assert error.value.reason == dependency
    assert "source" in error.value.detail
