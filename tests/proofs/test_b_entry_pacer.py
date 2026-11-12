"""Generic entry reserve primitives remain without a B production caller."""

import ast
import json
from decimal import Decimal as D
from pathlib import Path
from typing import cast

import pytest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, TrailingStopOrderRequest
from test_b_protector import NOW_NS
from test_b_protector import FakeTradingClient as Client

from sextant.execution.pacer import AccountPacer, ActionKind, PacerRefused
from sextant.risk.controls import Controls
from sextant.risk.controls import Controls as RiskControls

ROOT = Path(__file__).parents[2]
NS = 1_000_000_000


def b_entry_callers(source):
    """B has no submit(kind=...) or direct SUBMIT pacer request outside its facade."""
    return [
        call
        for call in ast.walk(ast.parse(source))
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and (
            (call.func.attr == "submit_order" and any(k.arg == "kind" for k in call.keywords))
            or (
                call.func.attr == "request"
                and any(
                    isinstance(arg, ast.Attribute) and arg.attr == "SUBMIT" for arg in call.args
                )
            )
        )
    ]


def test_no_b_production_caller_reaches_entry_admission_and_a_new_caller_fails_the_proof():
    sources = list((ROOT / "src/sextant/paper").glob("b_*.py"))
    assert sources
    for path in sources:
        assert b_entry_callers(path.read_text()) == [], path
    # Mutation witness: adding the most direct B caller makes this exact predicate fail.
    candidate = "client.submit_order(order, kind=ActionKind.SUBMIT)"
    with pytest.raises(AssertionError):
        assert b_entry_callers(candidate) == []
    with pytest.raises(AssertionError):
        assert b_entry_callers("pacer.request(ActionKind.SUBMIT, send)") == []


def test_generic_entry_reserve_includes_each_requested_snapshot_pair():
    controls = Controls.from_rows(json.loads((ROOT / "lanes/custody-parameters.json").read_text()))
    pacer = AccountPacer(controls, lambda: NOW_NS)
    pacer.bucket.tokens = 3
    sent = []
    active = [True]
    pacer.request(
        ActionKind.SUBMIT,
        lambda before: (before(), sent.append("entry")),
        admission=lambda: active[0],
        snapshot_reads=2,
        position_counts=lambda: (0, 0),
    )
    assert sent == [] and pacer.wire_requests == 0
    active[0] = False
    pacer.drain()
    pacer.request(ActionKind.REDUCE, lambda before: (before(), sent.append("protection")))
    assert sent == ["protection"] and pacer.wire_actions[ActionKind.SUBMIT] == 0
    assert pacer.wire_actions[ActionKind.REDUCE] == 1 and pacer.queue == []


def test_ac5_submit_reserve_preserves_protective_preemption(tmp_path):
    # The same existing AccountPacer used by B; scarce tokens cannot be taken by entry.
    class Controls:
        burst = 3

        def __getitem__(self, key):
            return {
                "request_budget": 180,
                "reserve_per_open_position": 2,
                "reserve_per_exiting_position": 1,
            }[key]

        def ns(self, key):
            return 60 * NS

    raw = Client()
    pacer = AccountPacer(cast(RiskControls, Controls()), lambda: NOW_NS)
    pacer.bucket.tokens = 2
    buy = LimitOrderRequest(
        symbol="AAPL",
        qty=D(1),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=D(100),
        client_order_id="sextantb-entry-proof",
    )
    with pytest.raises(PacerRefused):
        pacer.request(
            ActionKind.SUBMIT, lambda wire: (wire(), raw.submit_order(buy)), snapshot_reads=2
        )
    sell = TrailingStopOrderRequest(
        symbol="AAPL",
        qty=1,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        trail_price=D(1),
        client_order_id="sextantb-trail-protect",
    )
    pacer.request(ActionKind.REDUCE, lambda wire: (wire(), raw.submit_order(sell)))
    assert raw.submitted == [sell] and pacer.wire_actions[ActionKind.REDUCE] == 1
    assert pacer.wire_actions[ActionKind.SUBMIT] == 0
    # A withdrawn buy cannot leak out later with an old rank.
    pacer.bucket.tokens = 3
    pacer.drain()
    assert raw.submitted == [sell]


def test_b_entry_reserves_are_the_reviewed_custody_rows():
    root = Path(__file__).parents[2]
    controls = RiskControls.from_rows(
        json.loads((root / "lanes/custody-parameters.json").read_text())
    )
    assert controls["reserve_per_open_position"] == 2
    assert controls["reserve_per_exiting_position"] == 1
