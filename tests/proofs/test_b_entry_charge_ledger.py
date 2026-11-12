"""Admission identity invariant and real protective-path measurements."""

import json
from decimal import Decimal
from random import Random
from typing import cast

import pytest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus
from requests import Response
from requests.exceptions import Timeout as RequestTimeout
from test_b_entry_reserve_review import buy, setup_client
from test_b_protector import NOW_NS, api_order, api_position, api_update, b_account, b_params

from sextant.execution.pacer import AccountPacer, ActionKind, PacerRefused, TokenBucket
from sextant.paper.b_protector import Protector
from sextant.paper.b_protector_main import BudgetControls, PacedTradingClient
from sextant.paper.config import PaperBudget
from sextant.risk.controls import Controls

NS = 1_000_000_000
SYMBOLS = ("AAPL", "MSFT", "NVDA", "GOOG", "AMZN", "META", "TSLA", "INTC")
FIXTURE = "review-fixture"


@pytest.mark.parametrize("held", [1, 4, 7])
def test_one_reconcile_has_one_pair_of_account_reads(held):
    _clock, pacer, raw, client = setup_client()
    raw.positions = [api_position("20", symbol) for symbol in SYMBOLS[:held]]
    Protector(client, b_account(), b_params(), now_ns=lambda: NOW_NS).reconcile()
    assert raw.calls.count("get_all_positions") == raw.calls.count("get_orders") == 1
    assert pacer.wire_requests == held + 2


@pytest.mark.parametrize("held", [1, 4, 7])
def test_full_rearm_counts_each_terminal_reconcile(held):
    _clock, _pacer, raw, _client = setup_client()
    controls = BudgetControls(PaperBudget(1000, 60, 1000, 0, "wire measurement only"))
    pacer = AccountPacer(cast(Controls, controls), lambda: NOW_NS)
    client = PacedTradingClient(raw, pacer)
    old = [
        api_order(
            client_order_id=f"B001-trail-{symbol}",
            symbol=symbol,
            qty="10",
            side="sell",
            order_type="trailing_stop",
            trail_percent="2.5",
        )
        for symbol in SYMBOLS[:held]
    ]
    raw.orders, raw.positions = list(old), [api_position("20", s) for s in SYMBOLS[:held]]
    protector = Protector(client, b_account(), b_params(), now_ns=lambda: NOW_NS)
    protector.reconcile()
    for order in old:
        protector.handle(
            api_update(order.model_copy(update={"status": OrderStatus.CANCELED}), event="canceled")
        )
    assert raw.calls.count("get_all_positions") == raw.calls.count("get_orders") == held + 1
    assert pacer.wire_requests == 2 + 4 * held
    assert len(raw.cancelled) == held
    assert {request.symbol: Decimal(str(request.qty)) for request in raw.submitted} == {
        symbol: Decimal(20) for symbol in SYMBOLS[:held]
    }


@pytest.mark.parametrize("held", range(9))
def test_burst_18_admission_table_keeps_the_per_position_read_charge(held):
    _clock, _pacer, raw, client = setup_client()
    raw.positions = [api_position("20", symbol) for symbol in SYMBOLS[:held]]
    if held <= 2:
        client.submit_order(buy(), kind=ActionKind.SUBMIT, position_counts=lambda: (held, 0))
        assert len(raw.submitted) == 1
    else:
        with pytest.raises(PacerRefused):
            client.submit_order(buy(), kind=ActionKind.SUBMIT, position_counts=lambda: (held, 0))
        assert raw.submitted == []


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("lost", [False, True])
@pytest.mark.parametrize("sdk", [False, True])
def test_same_id_retry_and_lost_ack_preserve_the_original_charge(monkeypatch, retry, lost, sdk):
    clock, pacer, raw, client = setup_client()
    # One own partial/full path is nine wires; start one POST above that floor.
    reads = 2
    path = (
        pacer.controls["reserve_per_open_position"]
        + pacer.controls["reserve_per_exiting_position"]
        + reads
        + reads
        + reads
    )
    while pacer.bucket.tokens > path + 1:
        client.get_orders()
    original, lose = raw.submit_order, [lost]
    if sdk:
        entry = TradingClient(api_key=FIXTURE, secret_key=FIXTURE, paper=True)

        def respond(request, **options):
            payload = json.loads(request.body)
            order = original(buy(payload["symbol"], payload["client_order_id"]))
            if lose[0]:
                lose[0] = False
                raise RequestTimeout("accepted entry acknowledgement lost")
            response = Response()
            response.status_code, response._content = 200, order.model_dump_json().encode()
            return response

        monkeypatch.setattr(entry._session, "send", respond)
        client = PacedTradingClient(raw, pacer, entry_client=entry)
    elif lost:

        def accepted(order_data):
            original(order_data)
            raise TimeoutError("accepted entry acknowledgement lost")

        monkeypatch.setattr(client._entry_client, "submit_order", accepted)
    if lost:
        with pytest.raises((TimeoutError, RequestTimeout)):
            client.submit_order(buy(identity="B001r-first"), kind=ActionKind.SUBMIT)
    else:
        client.submit_order(buy(identity="B001r-first"), kind=ActionKind.SUBMIT)
    if not sdk:
        monkeypatch.setattr(client._entry_client, "submit_order", original)
    if retry:
        with pytest.raises(PacerRefused):
            client.submit_order(buy(identity="B001r-first"), kind=ActionKind.SUBMIT)
    second = False
    try:
        client.submit_order(buy("MSFT", "B001r-second"), kind=ActionKind.SUBMIT)
        second = True
    except PacerRefused:
        pass
    accepted_orders = {o.symbol: o for o in raw.orders if o.side is OrderSide.BUY}
    protector = Protector(client, b_account(), b_params(), now_ns=lambda: clock[0])
    for symbol, order in accepted_orders.items():
        raw.orders = [o for o in raw.orders if o.id != order.id]
        raw.positions.append(api_position("5", symbol))
        protector.handle(
            api_update(order.model_copy(update={"status": OrderStatus.FILLED, "filled_qty": "5"}))
        )
    assert {r.symbol for r in raw.submitted if r.side is OrderSide.SELL} == set(accepted_orders)
    assert not second


def test_raised_protective_drain_withdraws_unsent_entry_without_a_phantom_charge(monkeypatch):
    clock, pacer, raw, client = setup_client()
    original = raw.cancel_order_by_id

    def failed(order_id):
        raise RuntimeError("older protective cancel failed")

    monkeypatch.setattr(raw, "cancel_order_by_id", failed)
    for index in range(4):
        pacer.paused_until = clock[0] + NS
        with pytest.raises(PacerRefused):
            client.cancel_order_by_id(f"house-{index}")
        clock[0] += 60 * NS
        with pytest.raises(RuntimeError, match="older protective cancel"):
            client.submit_order(buy(identity=f"B001r-unsent-{index}"), kind=ActionKind.SUBMIT)
        pacer.drain()
        assert raw.submitted == [] and pacer.queue == []
    monkeypatch.setattr(raw, "cancel_order_by_id", original)
    clock[0] += 60 * NS
    client.submit_order(buy(identity="B001r-recovered"), kind=ActionKind.SUBMIT)
    assert len(raw.submitted) == 1
    assert client._unsettled == {"B001r-recovered"}


@pytest.mark.parametrize("identities", [1, 2, 3, 4])
def test_charge_identity_invariant_over_seeded_event_sequences(identities):
    rng = Random(8256 + identities)  # noqa: S311 - deterministic state-machine proof
    for _ in range(48):
        clock, pacer, raw, client = setup_client()
        assert client._entry_client is not None
        # Measurement-only capacity for the original four-ID oracle: five paths as
        # the refusal boundary, derived from the measured nine-wire path, not B authority.
        reads = 2
        path = (
            pacer.controls["reserve_per_open_position"]
            + pacer.controls["reserve_per_exiting_position"]
            + reads
            + reads
            + reads
        )
        pacer.bucket = TokenBucket(
            pacer.controls["request_budget"], pacer.controls.ns("window_s"), path * (4 + 1)
        )
        model = {}
        original_cancel, original_submit = raw.cancel_order_by_id, client._entry_client.submit_order

        def fail_cancel(order_id):
            raise RuntimeError("protective drain raised")

        def lost_submit(order_data, submit=original_submit):
            submit(order_data)
            raise TimeoutError("accepted POST response lost")

        for _ in range(32):
            cid = f"B001r-model-{rng.randrange(identities)}"
            action = rng.choice(("admit", "retry", "refuse", "drain-raises", "settle", "lost"))
            clock[0] += 60 * NS
            pacer.bucket.refill(clock[0])
            if action == "settle":
                client.entry_settled(cid)
                if cid in model:
                    model[cid] = False
            elif action == "drain-raises":
                raw.cancel_order_by_id = fail_cancel
                pacer.paused_until = clock[0] + NS
                with pytest.raises(PacerRefused):
                    client.cancel_order_by_id("house-model")
                pacer.paused_until = 0
                with pytest.raises((RuntimeError, PacerRefused)):
                    client.submit_order(buy(identity=cid), kind=ActionKind.SUBMIT)
                try:
                    pacer.drain()
                except RuntimeError:
                    pacer.drain()
                raw.cancel_order_by_id = original_cancel
            else:
                if action == "refuse":
                    pacer.bucket.tokens = 0
                permitted = cid not in model and action != "refuse" and sum(model.values()) < 4
                if action == "lost":
                    client._entry_client.submit_order = lost_submit
                if permitted:
                    if action == "lost":
                        with pytest.raises(TimeoutError):
                            client.submit_order(buy(identity=cid), kind=ActionKind.SUBMIT)
                    else:
                        client.submit_order(buy(identity=cid), kind=ActionKind.SUBMIT)
                    model[cid] = True
                else:
                    with pytest.raises(PacerRefused):
                        client.submit_order(buy(identity=cid), kind=ActionKind.SUBMIT)
                client._entry_client.submit_order = original_submit
            assert client._entry_charges == model
            assert client._unsettled == {key for key, active in model.items() if active}
            assert pacer.wire_actions[ActionKind.SUBMIT] == len(model)
            assert pacer.queue == []


def test_terminal_receipts_only_settle_their_admitted_identity_and_never_resurrect_it(monkeypatch):
    _clock, pacer, raw, client = setup_client()
    client.entry_update(api_order(client_order_id="owner-unadmitted", side="buy"))
    client.entry_update(api_order(client_order_id="unknown-terminal", side="buy"), "fill")
    assert client._entry_charges == {}
    client.submit_order(buy(identity="B001r-ledger"), kind=ActionKind.SUBMIT)
    pending = raw.orders[-1]
    client.entry_update(pending, "partial_fill")
    assert client._unsettled == {"B001r-ledger"}
    # A second facade shares this account's ledger, including settled tombstones.
    other = PacedTradingClient(raw, pacer, entry_client=client._entry_client)
    terminal = pending.model_copy(update={"status": OrderStatus.CANCELED})
    raw.orders = [terminal]
    monkeypatch.setattr(
        raw,
        "get_order_by_client_id",
        lambda cid: next(o for o in raw.orders if o.client_order_id == cid),
    )
    assert other.get_order_by_client_id("B001r-ledger").status is OrderStatus.CANCELED
    assert not client._unsettled
    other.entry_update(terminal, "canceled")
    other.entry_update(pending, "new")
    assert client._entry_charges == {"B001r-ledger": False}
    with pytest.raises(PacerRefused):
        other.submit_order(buy(identity="B001r-ledger"), kind=ActionKind.SUBMIT)
    assert pacer.wire_actions[ActionKind.SUBMIT] == 1
