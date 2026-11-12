"""Executed protection-path witnesses from bpreg-602479b Codex review, round 1."""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, TrailingStopOrderRequest
from test_b_protector import (
    NOW_NS,
    FakeTradingClient,
    api_order,
    api_position,
    api_update,
    b_account,
    b_params,
)
from test_b_protector_main import write_config

from sextant.execution.pacer import AccountPacer, ActionKind, PacerRefused
from sextant.paper import b_protector_main
from sextant.paper.b_protector import Protector
from sextant.paper.b_protector_main import BudgetControls, PacedTradingClient
from sextant.paper.config import PaperBudget
from sextant.risk.controls import Controls

ROOT = Path(b_protector_main.__file__).resolve().parents[3]
CONTROL_PATH = ROOT / "lanes/custody-parameters.json"


class WitnessBroker(FakeTradingClient):
    def submit_order(self, order_data):
        if order_data.side != OrderSide.BUY:
            return super().submit_order(order_data)
        self.calls.append("submit_entry")
        self.submitted.append(order_data)
        order = api_order(
            client_order_id=order_data.client_order_id,
            symbol=order_data.symbol,
            qty=str(order_data.qty),
            side="buy",
            limit_price=str(order_data.limit_price),
        )
        self.orders.append(order)
        return order

    def replace_order_by_id(self, order_id, order_data):
        self.replaced.append((order_id, order_data))
        self.calls.append("replace_order_by_id")
        return order_data


def buy(symbol="AAPL", identity="B001r-review", quantity=5):
    return LimitOrderRequest(
        symbol=symbol,
        qty=quantity,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=Decimal("100.01"),
        client_order_id=identity,
    )


def trail(symbol="AAPL", identity="B001-trail-review"):
    return TrailingStopOrderRequest(
        symbol=symbol,
        qty=5,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        trail_percent=Decimal("2.5"),
        client_order_id=identity,
    )


def setup_client():
    clock = [NOW_NS]
    controls = BudgetControls(PaperBudget(180, 60, 200, 0, "review fixture"))
    pacer = AccountPacer(cast(Controls, controls), now_ns=lambda: clock[0])
    raw = WitnessBroker()
    return (
        clock,
        pacer,
        raw,
        PacedTradingClient(
            raw, pacer, entry_client=SimpleNamespace(_retry=3, submit_order=raw.submit_order)
        ),
    )


def test_entry_preserves_the_existing_exiting_position_reserve():
    _clock, pacer, raw, client = setup_client()
    for _sequence in range(13):
        client.get_orders()
    assert pacer.bucket.tokens == 5
    counts_seen = []

    def position_counts():
        counts_seen.append((1, 1))
        return 1, 1

    with pytest.raises(PacerRefused):
        client.submit_order(
            buy(symbol="MSFT"), kind=ActionKind.SUBMIT, position_counts=position_counts
        )
    assert counts_seen == [(1, 1), (1, 1)]
    assert raw.submitted == []
    assert pacer.bucket.tokens == 5
    for sequence in range(5):
        client.cancel_order_by_id(f"house-protection-{sequence}")
    assert len(raw.cancelled) == 5
    print("EXIT-RESERVE-TRACE", dict(counts=counts_seen, cancellations=len(raw.cancelled)))


@pytest.mark.parametrize("with_entry", [False, True])
def test_real_protector_can_cover_a_fill_using_the_reserve(with_entry):
    clock, pacer, raw, client = setup_client()
    for _sequence in range(15):
        client.get_orders()
    assert pacer.bucket.tokens == 3
    if with_entry:
        try:
            client.submit_order(buy(), kind=ActionKind.SUBMIT, position_counts=lambda: (0, 0))
        except PacerRefused:
            assert pacer.bucket.tokens == 3
    raw.orders.clear()
    raw.positions = [api_position(qty="5")]
    protector = Protector(client, b_account(), b_params(), now_ns=lambda: clock[0])
    filled = api_order(
        client_order_id="B001r-review" if with_entry else "owner-buy-control",
        qty="5",
        filled_qty="5",
        status="filled",
    )
    protector.handle(api_update(filled, position_qty=5))
    protective = [request for request in raw.submitted if request.side == OrderSide.SELL]
    print(
        "FILL-TRACE",
        dict(
            entry=with_entry,
            tokens=pacer.bucket.tokens,
            calls=raw.calls[-4:],
            protective=len(protective),
            pages=[page.reason for page in protector.pages],
            queued=[ActionKind(item.kind).name for item in pacer.queue],
        ),
    )
    assert len(protective) == 1, "entry used the token needed after the two snapshot reads"
    assert Decimal(str(protective[0].qty)) == Decimal("5")


@pytest.mark.parametrize("with_entry", [False, True])
def test_two_symbol_cancel_replace_keeps_the_house_stop_covered(with_entry):
    clock, pacer, raw, client = setup_client()
    old = api_order(
        client_order_id="B001-trail-old",
        qty="10",
        side="sell",
        order_type="trailing_stop",
        trail_percent="2.5",
    )
    raw.orders = [old]
    raw.positions = [api_position(qty="20")]
    for _sequence in range(11):
        client.get_orders()
    if with_entry:
        try:
            client.submit_order(
                buy(symbol="MSFT"), kind=ActionKind.SUBMIT, position_counts=lambda: (1, 0)
            )
        except PacerRefused:
            assert pacer.bucket.tokens == 7
        raw.orders = [order for order in raw.orders if order.side != OrderSide.BUY]
    raw.positions.append(api_position(qty="5", symbol="MSFT"))
    protector = Protector(client, b_account(), b_params(), now_ns=lambda: clock[0])
    protector.reconcile()
    terminal = old.model_copy(update={"status": OrderStatus.CANCELED})
    protector.handle(api_update(terminal, event="canceled"))
    protective = {
        request.symbol: Decimal(str(request.qty))
        for request in raw.submitted
        if request.side == OrderSide.SELL
    }
    print(
        "REPLACE-TRACE",
        dict(
            entry=with_entry,
            tokens=pacer.bucket.tokens,
            protective=protective,
            cancelled=raw.cancelled,
            pages=[page.reason for page in protector.pages],
            queued=[ActionKind(item.kind).name for item in pacer.queue],
        ),
    )
    assert protective == {"AAPL": Decimal("20"), "MSFT": Decimal("5")}
    assert raw.cancelled == [str(old.id)]


@pytest.mark.parametrize(
    "payload",
    ["null", "42", "[null]", "{}", "[]", "[{}]", "broken-json", "bad-decimal", "reviewed-rows"],
)
def test_wrong_shaped_optional_controls_must_not_abort_protection(tmp_path, monkeypatch, payload):
    config = write_config(tmp_path, "b-slot2.json")
    original_read_text = Path.read_text
    runs = []

    def read_text(path, *args, **kwargs):
        if path == CONTROL_PATH and payload != "reviewed-rows":
            if payload == "bad-decimal":
                rows = json.loads(original_read_text(path, *args, **kwargs))
                next(row for row in rows if row["name"] == "window_s")["value"] = "not-decimal"
                return json.dumps(rows)
            return payload
        return original_read_text(path, *args, **kwargs)

    class Stream:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            runs.append("protection started")

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(b_protector_main, "ProtectorStream", Stream)
    monkeypatch.setenv("ALPACA_PAPER_2_API_KEY", "review-fixture")
    monkeypatch.setenv("ALPACA_PAPER_2_SECRET_KEY", "review-fixture")
    monkeypatch.setenv("B_EXPECTED_ACCOUNT_SUFFIX", "ture")
    monkeypatch.delenv("B_EXPECTED_ACCOUNT_ID", raising=False)
    result = b_protector_main.main(
        ["--config", str(config)],
        client_factory=lambda **kwargs: WitnessBroker(),
        data_client_factory=cast(Any, lambda **kwargs: None),
    )
    assert result == 0
    assert runs == ["protection started"]
    if payload != "reviewed-rows":
        _clock, _pacer, raw, client = setup_client()
        with pytest.raises(PacerRefused):
            client.submit_order(buy(), kind=ActionKind.SUBMIT)
        client.submit_order(trail())
        assert [request.side for request in raw.submitted] == [OrderSide.SELL]


def test_owner_order_custody_and_decimal_survive_entry_admission():
    clock, _pacer, raw, client = setup_client()
    owner = api_order(client_order_id="owner-sell", qty="3", side="sell")
    raw.orders = [owner]
    raw.positions = [api_position(qty="10")]
    request = buy(symbol="MSFT")
    client.submit_order(request, kind=ActionKind.SUBMIT, position_counts=lambda: (1, 0))
    assert raw.submitted[0] is request
    assert Decimal(str(request.limit_price)) == Decimal("100.01")
    protector = Protector(client, b_account(), b_params(), now_ns=lambda: clock[0])
    protector.reconcile()
    assert raw.cancelled == []
    assert raw.replaced == []
    assert owner in raw.orders
    sells = [order for order in raw.submitted if order.side == OrderSide.SELL]
    assert len(sells) == 1
    assert Decimal(str(sells[0].qty)) == Decimal("7")
