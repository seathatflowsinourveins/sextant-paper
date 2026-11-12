"""P-R2 (MODELED): B's first-leg entry on a real BacktestNode over fixture trades and quotes.

Every row below is a test input with no deployment authority: B's entry rows do not exist
(task R1 registers them), so the Strategy must refuse entries without them.
"""

from __future__ import annotations

import ast
from decimal import Decimal as D
from typing import Any

from nautilus_trader.config import StrategyConfig
from nautilus_trader.execution import StaticLatencyModel
from nautilus_trader.model import (
    AggressorSide,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
    StrategyId,
    TimeInForce,
    TradeId,
    TradeTick,
)
from nautilus_trader.trading import Strategy
from test_b_lifecycle import AT, EQUITY, MODULE, NS, PENNY, generation, quote, run_node

from sextant.entry.parameters import FIRST_LEG, SIZING, EntryParameters
from sextant.entry.sizing import size_entry
from sextant.exits.policy import ExitParams, buffer
from sextant.paper.b_lifecycle import BLifecycle
from sextant.ranking.registry import RANK_GATE_ENABLED

VALUES: dict[str, object] = {
    "first_leg_window_s": "120",
    "first_leg_trades_count": 3,
    "first_leg_span_s": "1",
    "first_leg_max_move": "0.10",
    "entry_max_vwap_extension_atr": "2",
    "entry_chase_bps": "10",
    "entry_ttl_s": "5",
    "reclaim_lookback_s": "30",
    "reclaim_trades_count": 3,
    "reclaim_trades_span_s": "1",
    "reclaim_hold_s": "2",
    "reclaim_max_above_buffers": "1",
    "atr_period": 3,
    "atr_bar_minutes": 1,
    "max_data_age_s": "5",
    "max_armed_symbols": 5,
    "bp_target_fraction": "1",
    "stop_ref_fraction": "0.01",
    "portfolio_slots": 5,
    "stopout_size_decay": "0.5",
    "stopout_size_floor": "0.25",
    "confidence_size_floor": "0.5",
    "chase_cap_fraction": "0.01",
    "flow_share_cap": "0.05",
    "flow_share_window_s": "60",
    "covered_flow_window_s": "2",
    "covered_flow_baseline_min": "1",
    "covered_flow_ratio": "1",
}
EXIT = ExitParams.starts()  # the exit registry's recorded starts, passed explicitly
TAPE = [
    ("-40", "0.5000", 100),
    ("-15", "0.5050", 100),
    ("-14", "0.5050", 100),
    ("-10", "0.5000", 100),
    ("-9", "0.5150", 500),
    ("-8", "0.5200", 500),
    ("-7", "0.5250", 500),  # completes the surge30 flag against the -40 quiet price
    ("-2", "0.5100", 100),  # a lower trade: the first-leg run starts here
    ("-1", "0.5100", 100),
    ("0.5", "0.5100", 100),  # completes the first-leg run
]


def at(seconds: str) -> int:
    return AT + int(D(seconds) * NS)


def rows(names=None) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "unit": "count" if type(value) is int else "fixture unit",
            "value": value,
            "source": "ADR-0004; TEST ONLY, no deployment authority",
            "calibration": "UNCALIBRATED",
            "last_change": "2026-11-10",
            "scope": "FIXTURE_ONLY",
        }
        for name, value in VALUES.items()
        if names is None or name in names
    ]


PARAMS = EntryParameters.from_rows(rows(), fixture=True, sizing_only=True)


def ticks(tape=TAPE) -> list[TradeTick]:
    return [
        TradeTick(
            PENNY,
            Price.from_str(price),
            Quantity.from_int(size),
            AggressorSide.BUY,
            TradeId(f"b-{seconds}"),
            at(seconds),
            at(seconds),
        )
        for seconds, price, size in tape
    ]


QUOTES = [quote(at("-8"), "0.5200", "0.5300"), quote(at("-3"), "0.5050", "0.5100")]


class Recording(BLifecycle):
    """Captures each command's own arguments, then sends it unchanged."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self.sent: list[tuple[str, Any, Any]] = []
        self.modifies: list[dict[str, Any]] = []  # every argument of each modify command

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        self.sent.append(("submit", order.client_order_id, params))
        super().submit_order(order, position_id=position_id, client_id=client_id, params=params)

    def modify_order(
        self,
        client_order_id,
        quantity=None,
        price=None,
        trigger_price=None,
        client_id=None,
        params=None,
    ) -> None:
        self.sent.append(("modify", client_order_id, price))
        self.modifies.append(
            dict(cid=client_order_id, quantity=quantity, price=price, trigger=trigger_price)
            | dict(params=params)
        )
        super().modify_order(client_order_id, quantity, price, trigger_price, client_id, params)

    def cancel_order(self, client_order_id, client_id=None, params=None) -> None:
        self.sent.append(("cancel", client_order_id, None))
        super().cancel_order(client_order_id, client_id=client_id, params=params)


class OwnerStandIn(Strategy):
    """An order that is not B's: one resting BUY LIMIT on the same instrument."""

    def __init__(self) -> None:
        super().__init__(StrategyConfig(strategy_id=StrategyId.from_str("OWNER-STANDIN")))
        self.placed = False

    def on_start(self) -> None:
        self.subscribe_quotes(PENNY)

    def on_quote(self, quote) -> None:
        if not self.placed:
            self.placed = True
            order = self.order_factory.limit(
                PENNY, OrderSide.BUY, Quantity.from_int(10), Price.from_str("0.4000")
            )
            self.submit_order(order)


def run_entry(tmp_path, *, tape=TAPE, quotes=QUOTES, extra_trades=(), **kwargs: Any):
    kwargs.setdefault("entry_params", PARAMS)
    kwargs.setdefault("exit_params", EXIT)
    kwargs.setdefault("commission_rate", D(0))
    kwargs.setdefault("basis_path", tmp_path / "basis.json")
    kwargs.setdefault("baseline", __import__("test_ranking").baseline())
    kwargs.setdefault("cls", Recording)
    holder: dict[str, Any] = {}

    class Captured(kwargs.pop("cls")):
        def __init__(self, config) -> None:
            super().__init__(config)
            holder["strategy"] = self

    journal, _, orders = run_node(
        tmp_path / "catalog",
        lambda pub: [(AT, generation(pub))],
        ticks(tape) + list(extra_trades),
        quotes=quotes,
        cls=Captured,
        start=at(tape[0][0]) - NS,
        generation_age=2,
        **kwargs,
    )
    return journal, orders, holder["strategy"]


def house(orders):
    return [order for order in orders if str(order.strategy_id).startswith("BLIFECYCLE-")]


def test_the_completing_trade_submits_one_limit_at_the_ask_sized_by_the_sizer(tmp_path) -> None:
    journal, orders, strategy = run_entry(tmp_path)
    (order,) = house(orders)
    assert order.order_type is OrderType.LIMIT and order.side is OrderSide.BUY
    assert order.price == Price.from_str("0.5100") and order.time_in_force is TimeInForce.DAY
    assert order.ts_init == at("0.5")  # built in the completing trade's handler
    trades = [(D(price), size, at(s)) for s, price, size in TAPE]
    window = [price for price, _, ts in trades if ts >= at("0.5") - 10 * NS]
    entry = D("0.5100")
    width = buffer(D("0.0001"), D("0.0050"), entry, max(window) - min(window), EXIT)
    assert width is not None
    trigger = entry - width
    expected = size_entry(
        instrument=EQUITY,
        params=PARAMS,
        setup="FIRST_LEG",
        entry=entry,
        initial_trigger=trigger,
        equity=D(100000),
        basis=D(100000),
        buying_power=D(100000),
        dollar_volume=sum((price * size for price, size, _ in trades), D(0)),
        confidence=None,
        prearm_stopouts=0,
        commission_rate=D(0),
    )
    assert expected.refusal is None and order.quantity == expected.quantity
    assert strategy.sent == [("submit", order.client_order_id, {"extended_hours": True})]
    assert [row["kind"] for row in journal if row["kind"] == "submit"] == ["submit"]


def test_a_lower_trade_restarts_the_run(tmp_path) -> None:
    tape = [*TAPE[:-1], ("-0.2", "0.5090", 100), ("0.5", "0.5100", 100)]
    _, orders, strategy = run_entry(tmp_path, tape=tape)
    assert house(orders) == [] and strategy.sent == []


def test_missing_rows_refuse_and_name_them(tmp_path) -> None:
    journal, orders, strategy = run_entry(tmp_path, entry_params=None)
    assert house(orders) == [] and strategy.sent == []
    (refusal,) = [row for row in journal if row["kind"] == "entry_refused"]
    assert set(refusal["missing"]) == FIRST_LEG | SIZING
    narrow = EntryParameters.from_rows(rows(), fixture=True, first_leg_only=True)
    journal, orders, _ = run_entry(tmp_path / "narrow", entry_params=narrow)
    (refusal,) = [row for row in journal if row["kind"] == "entry_refused"]
    assert set(refusal["missing"]) == SIZING - FIRST_LEG and house(orders) == []
    journal, orders, _ = run_entry(tmp_path / "exit", exit_params=None)
    (refusal,) = [row for row in journal if row["kind"] == "entry_refused"]
    assert refusal["missing"] == ["exit_params"] and house(orders) == []


def test_the_disabled_gate_sends_no_entry(tmp_path) -> None:
    _, orders, strategy = run_entry(tmp_path, gate=RANK_GATE_ENABLED)
    assert house(orders) == [] and strategy.sent == []


def test_a_reprice_stays_inside_the_chase_bound(tmp_path) -> None:
    latency = StaticLatencyModel(insert_latency_nanos=NS // 5)
    away = [quote(at("0.6"), "0.5150", "0.5200"), quote(at("0.9"), "0.5150", "0.5200")]
    inside = QUOTES + away + [quote(at("1"), "0.5050", "0.5103")]
    _, orders, strategy = run_entry(tmp_path, quotes=inside, latency=latency)
    assert len(house(orders)) == 1
    bound = D("0.5100") * (1 + D(10) / 10_000)
    modifies = [price for kind, _, price in strategy.sent if kind == "modify"]
    assert modifies == [Price.from_str("0.5103")] and all(p.as_decimal() <= bound for p in modifies)
    beyond = QUOTES + away + [quote(at("1"), "0.5150", "0.5200")]
    journal, orders, strategy = run_entry(tmp_path / "beyond", quotes=beyond, latency=latency)
    assert "modify" not in [kind for kind, _, _ in strategy.sent]
    assert any(row.get("reason") == "entry_chase_bps" for row in journal)


def test_the_ttl_cancels_only_the_house_order(tmp_path) -> None:
    latency = StaticLatencyModel(insert_latency_nanos=NS // 5)
    away = QUOTES + [quote(at(s), "0.5150", "0.5200") for s in ("0.6", "0.9", "6")]
    _, orders, strategy = run_entry(tmp_path, quotes=away, latency=latency, extra=[OwnerStandIn()])
    (order,) = house(orders)
    (owner,) = [o for o in orders if str(o.strategy_id) == "OWNER-STANDIN"]
    assert order.status is OrderStatus.CANCELED and owner.is_open
    assert [(kind, cid) for kind, cid, _ in strategy.sent] == [
        ("submit", order.client_order_id),
        ("cancel", order.client_order_id),
    ]


def test_no_float_in_the_strategy_money_path() -> None:
    tree = ast.parse(MODULE.read_text())
    floats = [
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.Constant) and type(node.value) is float)
        or (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "float")
    ]
    assert floats == []
