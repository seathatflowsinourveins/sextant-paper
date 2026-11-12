"""F1/F2 probes from the Opus review of 18c6226, with asserted protection outcomes."""

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus
from requests import Response
from requests.exceptions import Timeout
from test_b_entry_reserve_review import WitnessBroker, buy, setup_client
from test_b_protector import NOW_NS, api_position, api_update, b_account, b_params

from sextant.execution.pacer import AccountPacer, ActionKind, PacerRefused
from sextant.paper.b_protector import Protector
from sextant.paper.b_protector_main import BudgetControls, PacedTradingClient
from sextant.paper.config import PaperBudget
from sextant.risk.controls import Controls

SEC = 1_000_000_000
FIXTURE = "review-fixture"


def rig(requests=180):
    clock = [NOW_NS]
    controls = BudgetControls(PaperBudget(requests, 60, requests + 20, 0, "Opus F1/F2 probe only"))
    pacer = AccountPacer(cast(Controls, controls), lambda: clock[0])
    raw = WitnessBroker()
    seen = {}
    original_cancel = raw.cancel_order_by_id

    def remembering_cancel(order_id):
        seen.update({str(o.id): o for o in raw.orders})
        return original_cancel(order_id)

    raw.cancel_order_by_id = remembering_cancel
    client = PacedTradingClient(
        raw, pacer, entry_client=SimpleNamespace(_retry=3, submit_order=raw.submit_order)
    )
    return clock, pacer, raw, client, seen


def entry_path_reserve(pacer):
    # Measured: three snapshot pairs, two trail POSTs and a cancel, at the reviewed 2/1 rows.
    reads = 2
    return (
        pacer.controls["reserve_per_open_position"]
        + pacer.controls["reserve_per_exiting_position"]
        + reads
        + reads
        + reads
    )


def covered(raw, symbol):
    return sum(
        (
            Decimal(str(o.qty))
            for o in raw.orders
            if o.symbol == symbol and o.side is OrderSide.SELL and str(o.id) not in raw.cancelled
        ),
        Decimal(0),
    )


def partial_then_full(clock, pacer, raw, client, seen, admitted, gap_ms):
    protector = Protector(client, b_account(), b_params(), now_ns=lambda: clock[0])
    start_wires = pacer.wire_requests
    raw.orders = [o for o in raw.orders if o.id != admitted.id]
    partial = admitted.model_copy(
        update={"status": OrderStatus.PARTIALLY_FILLED, "filled_qty": "5"}
    )
    raw.orders.append(partial)
    raw.positions.append(api_position("5", admitted.symbol))
    protector.handle(api_update(partial, event="partial_fill"))
    assert covered(raw, admitted.symbol) == Decimal(5)
    clock[0] += gap_ms * 1_000_000
    raw.orders = [o for o in raw.orders if o.id != admitted.id]
    raw.positions = [p for p in raw.positions if p.symbol != admitted.symbol]
    raw.positions.append(api_position("10", admitted.symbol))
    full = admitted.model_copy(update={"status": OrderStatus.FILLED, "filled_qty": "10"})
    protector.handle(api_update(full, event="fill"))
    for order_id in list(raw.cancelled):
        protector.handle(
            api_update(
                seen[order_id].model_copy(update={"status": OrderStatus.CANCELED}), event="canceled"
            )
        )
    out = {
        "symbol": admitted.symbol,
        "wires": pacer.wire_requests - start_wires,
        "covered": str(covered(raw, admitted.symbol)),
        "held": "10",
        "pages": [p.reason for p in protector.pages],
        "tokens_left": pacer.bucket.tokens,
    }
    print("PARTIAL-THEN-FULL", json.dumps(out))
    assert covered(raw, admitted.symbol) == Decimal(10)
    assert protector.pages == []
    return out


@pytest.mark.parametrize("held", [0, 1])
@pytest.mark.parametrize("gap_ms", [0, 100, 370, 1000])
def test_reviewers_old_boundary_cannot_admit_an_underfunded_partial_then_full_fill(held, gap_ms):
    clock, pacer, raw, client, seen = rig()
    raw.positions = [api_position("20", f"S{i}") for i in range(held)]
    # Give existing positions their matching trails, as in the reviewer's exact probe.
    from test_b_protector import api_order

    raw.orders = [
        api_order(
            client_order_id=f"B001-trail-S{i}",
            symbol=f"S{i}",
            qty="20",
            side="sell",
            order_type="trailing_stop",
            trail_percent="2.5",
        )
        for i in range(held)
    ]
    old_floor = (pacer.controls["reserve_per_open_position"] + 2) * (held + 1)
    while pacer.bucket.tokens > old_floor + 1:
        client.get_account()
    try:
        admitted = client.submit_order(
            buy("NEW", "B001r-partial", quantity=10),
            kind=ActionKind.SUBMIT,
            position_counts=lambda: (held, 0),
        )
    except PacerRefused:
        assert client._entry_charges == {} and pacer.wire_actions[ActionKind.SUBMIT] == 0
        return
    partial_then_full(clock, pacer, raw, client, seen, admitted, gap_ms)


@pytest.mark.parametrize("held", [0, 1])
@pytest.mark.parametrize("gap_ms", [0, 100, 370, 1000])
def test_measured_partial_then_full_path_is_protected_at_its_funded_boundary(held, gap_ms):
    from test_b_protector import api_order

    clock, pacer, raw, client, seen = rig()
    raw.positions = [api_position("20", f"S{i}") for i in range(held)]
    raw.orders = [
        api_order(
            client_order_id=f"B001-trail-S{i}",
            symbol=f"S{i}",
            qty="20",
            side="sell",
            order_type="trailing_stop",
            trail_percent="2.5",
        )
        for i in range(held)
    ]
    reserve = entry_path_reserve(pacer) + held * (pacer.controls["reserve_per_open_position"] + 2)
    while pacer.bucket.tokens > reserve + 1:
        client.get_account()
    admitted = client.submit_order(
        buy("NEW", "B001r-funded", quantity=10),
        kind=ActionKind.SUBMIT,
        position_counts=lambda: (held, 0),
    )
    assert pacer.bucket.tokens == reserve
    out = partial_then_full(clock, pacer, raw, client, seen, admitted, gap_ms)
    assert out["wires"] == entry_path_reserve(pacer) == 9
    assert client._entry_charges == {"B001r-funded": False}


def test_each_pending_entry_keeps_its_own_partial_resize_path():
    clock, pacer, raw, client, seen = rig(900)
    first = client.submit_order(buy("AAPL", "B001r-first", quantity=10), kind=ActionKind.SUBMIT)
    floor = entry_path_reserve(pacer) + entry_path_reserve(pacer)
    while pacer.bucket.tokens > floor:
        client.get_account()
    with pytest.raises(PacerRefused):
        client.submit_order(buy("MSFT", "B001r-unsent", quantity=10), kind=ActionKind.SUBMIT)
    assert client._entry_charges == {"B001r-first": True}
    clock[0] += 60 * SEC
    # At the exact two-entry boundary both eventual partial/full sequences must fit.
    pacer.bucket.refill(clock[0])
    while pacer.bucket.tokens > floor + 1:
        client.get_account()
    second = client.submit_order(buy("MSFT", "B001r-second", quantity=10), kind=ActionKind.SUBMIT)
    assert pacer.bucket.tokens == floor
    partial_then_full(clock, pacer, raw, client, seen, first, 0)
    seen.clear()
    raw.cancelled.clear()
    partial_then_full(clock, pacer, raw, client, seen, second, 0)
    assert {s for s in ("AAPL", "MSFT") if covered(raw, s) == Decimal(10)} == {"AAPL", "MSFT"}


@pytest.mark.parametrize("status", [403, 422])
def test_definitive_sdk_rejection_settles_only_its_charge_and_keeps_entries_available(
    monkeypatch, status
):
    clock, pacer, raw, _ = setup_client()
    sent = []

    def respond(prepared, **options):
        sent.append(json.loads(prepared.body)["client_order_id"])
        result = Response()
        result.status_code = status
        result._content = b'{"message":"synthetic definitive rejection"}'
        return result

    entry = TradingClient(api_key=FIXTURE, secret_key=FIXTURE, paper=True)
    monkeypatch.setattr(entry._session, "send", respond)
    client = PacedTradingClient(raw, pacer, entry_client=entry)
    for index in range(8):
        clock[0] += 60 * SEC
        with pytest.raises(APIError):
            client.submit_order(buy(identity=f"B001r-rejected-{index}"), kind=ActionKind.SUBMIT)
        assert not client._unsettled
        assert client._entry_charges[f"B001r-rejected-{index}"] is False
    assert len(sent) == 8 and pacer.wire_actions[ActionKind.SUBMIT] == 8


@pytest.mark.parametrize("outcome", [500, 502, 503, 504, "timeout", "no response"])
def test_ambiguous_dispatch_keeps_exactly_its_own_exposure(monkeypatch, outcome):
    _clock, pacer, raw, _ = setup_client()
    entry = TradingClient(api_key=FIXTURE, secret_key=FIXTURE, paper=True)

    def respond(prepared, **options):
        if outcome == "timeout":
            raise Timeout("response lost after dispatch")
        if outcome == "no response":
            return None
        result = Response()
        result.status_code, result._content = outcome, b'{"message":"synthetic uncertain result"}'
        return result

    monkeypatch.setattr(entry._session, "send", respond)
    client = PacedTradingClient(raw, pacer, entry_client=entry)
    client._entry_charges["B001r-already-settled"] = False
    with pytest.raises((APIError, PacerRefused, Timeout, AttributeError)):
        client.submit_order(buy(identity="B001r-uncertain"), kind=ActionKind.SUBMIT)
    assert client._entry_charges == {"B001r-already-settled": False, "B001r-uncertain": True}
    assert pacer.wire_actions[ActionKind.SUBMIT] == 1


@pytest.mark.parametrize("status", [403, 422])
def test_definitive_rejection_never_releases_a_different_pending_id(monkeypatch, status):
    from test_b_protector import api_order

    _clock, pacer, raw, _client, _seen = rig(900)
    entry = TradingClient(api_key=FIXTURE, secret_key=FIXTURE, paper=True)
    sent = []

    def respond(prepared, **options):
        identity = json.loads(prepared.body)["client_order_id"]
        sent.append(identity)
        result = Response()
        result.status_code = 200 if len(sent) == 1 else status
        result._content = api_order(client_order_id=identity, side="buy").model_dump_json().encode()
        return result

    monkeypatch.setattr(entry._session, "send", respond)
    client = PacedTradingClient(raw, pacer, entry_client=entry)
    client.submit_order(buy(identity="B001r-still-pending"), kind=ActionKind.SUBMIT)
    with pytest.raises(APIError):
        client.submit_order(buy(identity="B001r-definitive"), kind=ActionKind.SUBMIT)
    assert client._entry_charges == {"B001r-still-pending": True, "B001r-definitive": False}
    assert client._unsettled == {"B001r-still-pending"}
    assert pacer.wire_actions[ActionKind.SUBMIT] == 2
