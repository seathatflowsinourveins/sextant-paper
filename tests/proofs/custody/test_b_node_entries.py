"""Part 6/G3: loaded entry dependencies reach native replay; gate off stays protection-only."""

import importlib
import importlib.util
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from nautilus_trader.model import OrderSide, OrderType
from test_b_lifecycle import PENNY
from test_b_lifecycle_entry import PARAMS, Recording, house, rows, run_entry
from test_ranking import baseline, publisher
from test_ranking_consumer import consumer as make_consumer

from sextant.alpaca.config import AlpacaDataClientConfig
from sextant.paper import node_order, node_rank
from sextant.paper.b_lifecycle import BLifecycle
from sextant.ranking import registry
from sextant.ranking.consumer import RankingConsumer
from sextant.ranking.node_ranking import RankingArguments, RankingInputs

from .test_b_order_node import NUMBER, TIMING, AccountVenue, Stand, argv

GOLDEN = Path(__file__).with_name("b-entries-off-receipt.json")


def enabled(monkeypatch):
    gate = replace(registry.RANK_GATE_ENABLED, value=True)
    monkeypatch.setattr(registry, "RANK_GATE_ENABLED", gate)
    monkeypatch.setattr(node_order, "RANK_GATE_ENABLED", gate)


def inputs(tmp_path, *, unavailable=None):
    # Paper's import omits the old node-ranking proof; reuse its shipped input shape
    # with the ranking fixtures present here. These inputs carry no runtime authority.
    args = node_order.parse(dependency_argv(tmp_path, unavailable=unavailable))
    pub = publisher()
    ranking = RankingArguments(
        universe=pub.universe,
        universe_source=pub.universe_source,
        baselines={"PRE": pub.baseline},
        registry=pub.registry,
        duration_s=args.duration_s,
        receipt_dir=tmp_path / "ranking",
        page_window_s=1,
        status_snapshot_window_s=None,
        data=AlpacaDataClientConfig(
            feed=pub.feed,
            api_key_env=args.api_key_env,
            api_secret_env=args.api_secret_env,
        ),
        inputs=RankingInputs(
            session=pub.segment.session_date,
            known_at=pub.segment.start_ns,
            closes={name: Decimal("0.50") for name in pub.universe},
            types={name: "common" for name in pub.universe},
            reference_source="TEST ONLY official-close fixture",
            asset_source="TEST ONLY vendor fixture",
        ),
    )
    args = replace(args, ranking=ranking)
    assert args.ranking is not None
    args = replace(
        args,
        universe=(PENNY,),
        rows=replace(
            args.rows,
            entry=PARAMS,
        ),
        basis_path=tmp_path / "basis.json",
        ranking=replace(args.ranking, baselines={"PRE": baseline()}),
    )
    return args


def dependency_argv(tmp_path, *, unavailable=None):
    parts = argv(tmp_path)
    if unavailable is not None:
        flag, name = {
            "exit": ("--exit-parameters", "prearm_buffer_fraction"),
            "commission_rate": ("--node-parameters", "paper_us_equity_commission_per_fill"),
        }[unavailable]
        path = Path(parts[parts.index(flag) + 1])
        data = json.loads(path.read_text())
        data = [
            r | {"value": None, "calibration_state": "UNAVAILABLE"} if r["name"] == name else r
            for r in data
        ]
        altered = tmp_path / "unavailable.json"
        altered.write_text(json.dumps(data))
        parts[parts.index(flag) + 1] = str(altered)
    return parts


@pytest.mark.parametrize("name", ["exit", "commission_rate"])
def test_normal_loader_supplies_the_entry_dependency(tmp_path, name):
    args = node_order.parse(argv(tmp_path))
    assert getattr(args.rows, name) is not None, f"G3 normal load_rows leaves {name} unset"


def loader():
    name = "sextant.paper.entry_inputs"
    assert importlib.util.find_spec(name) is not None, "Paper B needs its paper-local input loader"
    return importlib.import_module(name).load_entry_inputs


def test_enabled_node_composition_supplies_the_consumers_segment_baseline(tmp_path, monkeypatch):
    enabled(monkeypatch)
    args = inputs(tmp_path)
    assert args.ranking is not None
    entry = make_consumer(publisher())
    config = node_order.strategy_config(args, consumer=entry, journal=None)
    strategy = BLifecycle(config)
    assert strategy.missing == []
    assert strategy.baseline is args.ranking.baselines["PRE"]
    assert strategy.params is PARAMS
    assert strategy.commission == Decimal(0) and strategy.basis_path == tmp_path / "basis.json"
    assert strategy.exit is not None and strategy.exit is args.rows.exit
    assert strategy.exit.chase_on is False


@pytest.mark.parametrize("name", ["exit", "commission_rate"])
def test_unavailable_registry_dependency_still_refuses_native_entries(tmp_path, monkeypatch, name):
    enabled(monkeypatch)
    args = inputs(tmp_path, unavailable=name)
    missing = "exit_params" if name == "exit" else name

    class Composed(Recording):
        def __init__(self, original):
            super().__init__(
                node_order.strategy_config(
                    args, consumer=original.consumer, journal=original.journal
                )
            )

    journal, orders, strategy = run_entry(tmp_path / "replay", cls=Composed)
    assert strategy.missing == [missing]
    assert [r["missing"] for r in journal if r["kind"] == "entry_refused"] == [[missing]]
    assert house(orders) == [] and strategy.sent == []


@pytest.mark.parametrize("name", ["exit", "commission_rate"])
def test_unavailable_registry_dependency_allows_protection_start(tmp_path, monkeypatch, name):
    monkeypatch.setenv(node_order.EXPECTED_FULL_ENV, NUMBER)
    args = replace(node_order.parse(dependency_argv(tmp_path, unavailable=name)), duration_s=1)
    stand = Stand(AccountVenue())
    path, code = node_order.run(
        args,
        consumer=None,
        leases=node_order.take_leases,
        data_factory=stand.data,
        exec_factory=stand.exec,
        timing=TIMING,
    )
    body = json.loads(path.read_text())
    assert code == 0 and body["phase"] == "stopped" and body["reason"] is None
    assert stand.connected == 1 and all(r[0] == "GET" for r in stand.venue.requests)
    assert ("exit_params" if name == "exit" else name) in body["steps"]["entry"]


@pytest.mark.parametrize("name", ["exit", "commission_rate"])
def test_unavailable_registry_dependency_allows_attended_entry(tmp_path, monkeypatch, name):
    from . import test_b_attended_entry as attended

    monkeypatch.setenv(node_order.EXPECTED_FULL_ENV, NUMBER)

    def opted(path):
        # Alter only the registry input, preserving the existing attended fixture's intent.
        parts = dependency_argv(path, unavailable=name)
        return replace(
            node_order.parse([*parts, "--attended-test-entry", str(attended.IID)]), duration_s=1
        )

    monkeypatch.setattr(attended, "opted", opted)
    venue, strategy = attended.exercise(tmp_path, monkeypatch, outcome="filled")
    assert strategy.attended_attempted
    assert ("exit_params" if name == "exit" else name) in strategy.missing
    assert len([r for r in venue.rows.values() if r["type"] == "trailing_stop"]) == 1


def test_an_inconsistent_surge_baseline_refuses_the_configuration(tmp_path, monkeypatch):
    enabled(monkeypatch)
    args = inputs(tmp_path)
    entry = make_consumer(publisher())
    assert args.ranking is not None
    args = replace(args, ranking=replace(args.ranking, baselines={"PRE": baseline(zero=True)}))
    with pytest.raises(ValueError, match="baseline"):
        node_order.strategy_config(args, consumer=entry, journal=None)


def test_enabled_in_node_ranking_reaches_the_existing_lease_gate(tmp_path, monkeypatch):
    enabled(monkeypatch)
    args = inputs(tmp_path)
    monkeypatch.setenv(node_order.EXPECTED_FULL_ENV, NUMBER)

    def unavailable(held, args):
        raise node_order.OrderModeRefused("fixture_lease", "exclusive writer still owns it")

    path, code = node_order.run(args, consumer=None, leases=unavailable)
    body = json.loads(path.read_text())
    assert code == 2 and body["reason"] == "fixture_lease"


def test_current_registry_never_constructs_or_reads_consumer_and_keeps_receipt_bytes(
    tmp_path,
    monkeypatch,
):
    assert registry.RANK_GATE_ENABLED.value is False and registry.B_ENTRY_AGE_ROWS == ()

    def forbidden(*args, **kwargs):
        raise AssertionError("gate-off path called the ranking consumer")

    monkeypatch.setattr(node_rank, "consumer", forbidden)
    monkeypatch.setattr(RankingConsumer, "__init__", forbidden)
    monkeypatch.setattr(RankingConsumer, "entry_rank", forbidden)
    monkeypatch.setenv(node_order.EXPECTED_FULL_ENV, NUMBER)
    args = node_order.parse(argv(tmp_path, count=1))
    stand = Stand(AccountVenue())
    path, code = node_order.run(
        args,
        consumer=None,
        leases=node_order.take_leases,
        data_factory=stand.data,
        exec_factory=stand.exec,
        timing=TIMING,
    )
    assert code == 0
    assert path.read_bytes() == GOLDEN.read_bytes().replace(b"CASE_ROOT", str(tmp_path).encode())
    assert stand.connected == 1 and all(r[0] == "GET" for r in stand.venue.requests)
    assert "ranking_consumer" in json.loads(path.read_text())["steps"]["entry"]


def test_paper_loader_uses_s1s_file_rows_and_existing_objects_without_defaults(tmp_path):
    path = tmp_path / "entry.json"
    path.write_text(json.dumps([r | {"scope": "REVIEWED"} for r in rows()]))
    args = inputs(tmp_path)
    assert args.ranking is not None
    loaded = loader()(
        path,
        exit_params=args.rows.exit,
        commission_rate=args.rows.commission_rate,
        basis_path=args.basis_path,
        baseline=args.ranking.baselines["PRE"],
    )
    assert loaded["entry_params"].sizing_only is True
    assert loaded["entry_params"]["chase_cap_fraction"] == Decimal("0.01")
    assert loaded["exit_params"] is args.rows.exit
    assert loaded["commission_rate"] == Decimal(0)
    assert loaded["basis_path"] == tmp_path / "basis.json"
    assert loaded["baseline"].identity == baseline().identity


@pytest.mark.parametrize("damage", ["missing", "fixture"])
def test_paper_loader_never_fills_an_unavailable_or_fixture_entry_value(tmp_path, damage):
    data = [r | {"scope": "REVIEWED"} for r in rows()]
    data = [
        r | ({"value": None} if damage == "missing" else {"scope": "FIXTURE_ONLY"})
        if r["name"] == "chase_cap_fraction"
        else r
        for r in data
    ]
    path = tmp_path / "entry.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="chase_cap_fraction"):
        loader()(path, exit_params=None, commission_rate=None, basis_path=None, baseline=None)


def test_composed_inputs_reach_the_existing_native_first_leg_entry(tmp_path, monkeypatch):
    enabled(monkeypatch)
    args = inputs(tmp_path)

    class Composed(Recording):
        def __init__(self, original):
            config = node_order.strategy_config(
                args, consumer=original.consumer, journal=original.journal
            )
            super().__init__(config)

    journal, orders, strategy = run_entry(tmp_path / "replay", cls=Composed)
    assert strategy.missing == []
    (order,) = [order for order in house(orders) if order.side is OrderSide.BUY]
    assert order.order_type is OrderType.LIMIT
    assert order.price.as_decimal() == Decimal("0.5100")
    assert any(row["kind"] == "submit" for row in journal)


@pytest.mark.parametrize("value", [0, 1, None, "true", Decimal(0)])
def test_invalid_gate_with_ranking_still_refuses_before_any_lease(tmp_path, monkeypatch, value):
    monkeypatch.setenv(node_order.EXPECTED_FULL_ENV, NUMBER)
    gate = replace(registry.RANK_GATE_ENABLED, value=value)
    monkeypatch.setattr(node_order, "RANK_GATE_ENABLED", gate)
    monkeypatch.setattr(registry, "RANK_GATE_ENABLED", gate)
    args = inputs(tmp_path)

    def unavailable(held, args):
        raise node_order.OrderModeRefused("fixture_lease", "must not reach the lease")

    path, code = node_order.run(args, consumer=None, leases=unavailable)
    assert code == 2 and json.loads(path.read_text())["reason"] == "ranking_consumer"
