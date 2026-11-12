"""Dormant B entry transport primitives, tested against the real pinned SDK."""

import json
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import cast

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from requests import Response
from requests.exceptions import InvalidJSONError, Timeout
from test_b_protector import NOW_NS, api_order

from sextant.execution.pacer import AccountPacer, ActionKind
from sextant.paper.b_feed import entry_wire, no_retries
from sextant.paper.b_protector import TradingClientLike
from sextant.paper.b_protector_main import PacedTradingClient
from sextant.risk.controls import Controls

FIXTURE = "review-fixture"


def clients():
    primary = TradingClient(api_key=FIXTURE, secret_key=FIXTURE, paper=True)
    entry = TradingClient(api_key=FIXTURE, secret_key=FIXTURE, paper=True)
    controls = Controls.from_rows(
        json.loads((Path(__file__).parents[2] / "lanes/custody-parameters.json").read_text())
    )
    pacer = AccountPacer(controls, lambda: NOW_NS)
    return primary, entry, pacer


def test_real_pinned_entry_client_retry_is_zero_and_primary_is_unchanged():
    assert version("alpaca-py") == "0.44.0"
    primary, entry, pacer = clients()
    original = primary._retry
    assert hasattr(entry, "_retry") and entry._retry == 3
    wrapper = PacedTradingClient(cast(TradingClientLike, primary), pacer)
    no_retries(entry, primary)
    assert entry._retry == 0
    assert primary._retry == original
    assert wrapper._client is primary


def test_missing_retry_attribute_refuses_entry_construction_without_breaking_protection(
    monkeypatch,
):
    primary, entry, pacer = clients()
    original = primary._retry
    protected = PacedTradingClient(cast(TradingClientLike, primary), pacer)
    del entry._retry
    with pytest.raises(TypeError, match="retry"):
        no_retries(entry, primary)
    assert primary._retry == original
    response = Response()
    response.status_code, response._content = 204, b""
    monkeypatch.setattr(primary._session, "request", lambda *a, **kw: response)
    protected.cancel_order_by_id("00000000-0000-4000-8000-000000000001")
    assert pacer.wire_actions[ActionKind.CANCEL] == 1


def test_same_client_cannot_disable_the_protective_clients_retries():
    primary, _entry, _pacer = clients()
    original = primary._retry
    with pytest.raises(TypeError):
        no_retries(primary, primary)
    assert primary._retry == original


@pytest.mark.parametrize("status", [200, 429, 504])
def test_real_sdk_dispatch_is_one_paced_wire_without_retry_sleep(monkeypatch, status):
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    primary, entry, pacer = clients()
    no_retries(entry, primary)
    calls, sleeps = [], []

    def respond(request, **kwargs):
        calls.append(request.method)
        response = Response()
        response.status_code = status
        response._content = api_order(qty="5").model_dump_json().encode()
        return response

    monkeypatch.setattr(entry._session, "send", respond)
    monkeypatch.setattr("alpaca.common.rest.time.sleep", sleeps.append)
    original_send = entry._session.send
    order = LimitOrderRequest(
        symbol="AAPL",
        qty=5,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price="100.01",
        client_order_id="B001r-transport",
    )

    def dispatch(before_wire):
        with entry_wire(entry, before_wire):
            entry.submit_order(order)

    if status == 200:
        pacer.request(ActionKind.SUBMIT, dispatch)
    else:
        with pytest.raises(APIError):
            pacer.request(ActionKind.SUBMIT, dispatch)
    assert calls == ["POST"] and sleeps == []
    assert pacer.wire_requests == 1 and pacer.wire_actions[ActionKind.SUBMIT] == 1
    assert primary._retry == 3 and entry._retry == 0
    assert entry._session.send is original_send


def test_sdk_preparation_precedes_wire_and_raised_dispatch_restores_the_session(monkeypatch):
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    _primary, entry, pacer = clients()
    no_retries(entry)
    calls = []

    def timeout(request, **kwargs):
        calls.append(request)
        raise Timeout("uncertain dispatched response")

    monkeypatch.setattr(entry._session, "send", timeout)
    order = LimitOrderRequest(
        symbol="AAPL",
        qty=5,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price="100.01",
        client_order_id="B001r-prepare",
    )
    invalid = order.model_copy(update={"limit_price": Decimal("NaN")})

    def dispatch(request):
        def send(before_wire):
            with entry_wire(entry, before_wire):
                entry.submit_order(request)

        return send

    with pytest.raises(InvalidJSONError):
        pacer.request(ActionKind.SUBMIT, dispatch(invalid))
    assert pacer.wire_requests == 0 and calls == []
    assert entry._session.send is timeout
    with pytest.raises(Timeout):
        pacer.request(ActionKind.SUBMIT, dispatch(order))
    assert pacer.wire_requests == 1 and len(calls) == 1
    assert entry._session.send is timeout
