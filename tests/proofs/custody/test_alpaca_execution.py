"""Execution boundary proofs: NT d53849b Python adapter; AP cc4cb3b7 REST.

Synthetic fixtures only. Alpaca orders/streaming documentation read 2026-11-10.
"""

import asyncio
import importlib.util
import json
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from nautilus_trader.model import (
    ClientOrderId,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    TimeInForce,
    TrailingOffsetType,
)

from sextant.execution.pacer import AccountPacer, ActionKind


def boundary():
    from sextant import alpaca

    assert importlib.util.find_spec(f"{alpaca.__name__}.wire") is not None, "wire boundary missing"
    return importlib.import_module(f"{alpaca.__name__}.wire")


def order(**changes):
    return NS(
        **dict(
            client_order_id=ClientOrderId("fixture-entry"),
            instrument_id=NS(symbol=NS(value="TEST")),
            quantity=Quantity.from_str("9"),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            price=Price.from_str("0.1234"),
            trailing_offset=Decimal("212.5"),
            trailing_offset_type=TrailingOffsetType.BASIS_POINTS,
        )
        | changes
    )


def command(**changes):
    return NS(**dict(quantity=None, price=None, trigger_price=None, params={}) | changes)


def test_sdk_payload_preserves_exact_decimal_strings():
    wire = boundary()
    payload = wire.submit_payload(order(), {}, "fixture-")
    encoded = json.dumps(payload)
    assert json.loads(encoded)["limit_price"] == "0.1234"
    assert json.loads(encoded)["qty"] == "9"
    assert payload["order_class"] == "simple"
    assert payload["extended_hours"] is False
    trail = wire.submit_payload(
        order(order_type=OrderType.TRAILING_STOP_MARKET, side=OrderSide.SELL), {}, "fixture-"
    )
    assert trail["trail_percent"] == "2.125"
    assert trail["type"] == "trailing_stop"


@pytest.mark.parametrize(
    "changes,params",
    [
        ({"client_order_id": ClientOrderId("owner-entry")}, {}),
        ({"client_order_id": ClientOrderId("fixture-entry-r1")}, {}),
        ({"quantity": Quantity.from_str("1.5")}, {}),
        ({"price": Price.from_str("1.234")}, {}),
        ({"order_type": OrderType.TRAILING_STOP_MARKET}, {"extended_hours": True}),
        ({"order_type": OrderType.MARKET}, {}),
        ({}, {"extended_hours": "true"}),
    ],
)
def test_unsupported_request_is_refused_before_wire(changes, params):
    with pytest.raises(ValueError):
        boundary().submit_payload(order(**changes), params, "fixture-")


def test_modify_uses_engine_offset_convention_and_whole_quantity():
    payload = boundary().modify_payload(
        command(
            quantity=Quantity.from_str("12"),
            trigger_price=Price.from_str("0.12"),
            params={"trailing_offset": Decimal("125")},
        ),
        {"type": "trailing_stop"},
    )
    assert payload == {"qty": "12", "trail": "1.25"}
    with pytest.raises(ValueError, match="offset"):
        boundary().modify_payload(
            command(trigger_price=Price.from_str("0.12")), {"type": "trailing_stop"}
        )


@pytest.mark.parametrize(
    "row",
    [
        {"status": "partially_filled", "filled_qty": "1", "side": "sell"},
        {"status": "new", "filled_qty": "1", "side": "sell"},
        {"status": "new", "filled_qty": "0", "side": "sell", "triggered_at": "fixture"},
        {"status": "filled", "filled_qty": "9", "side": "sell"},
    ],
)
def test_exit_that_has_started_execution_cannot_be_cancelled_or_resized(row):
    with pytest.raises(ValueError, match="exit"):
        boundary().guard_mutation(row | {"client_order_id": "fixture-entry"}, "fixture-")


def test_owner_order_never_passes_mutation_guard():
    with pytest.raises(ValueError, match="namespace"):
        boundary().guard_mutation({"client_order_id": "owner-entry"}, "fixture-")


def test_gateway_is_off_loop_and_counts_each_sdk_request():
    async def run():
        from sextant.risk.controls import Controls

        from .conftest import controls

        wire = boundary()
        pacer = AccountPacer(controls(), lambda: 0)
        assert isinstance(pacer.controls, Controls)
        sdk = Mock()
        sdk.get.return_value = {"ok": True}
        gateway = wire.AccountGateway(sdk, pacer, lambda: (0, 0))
        result = await gateway.call(ActionKind.QUERY, "get", "/account")
        assert result == {"ok": True}
        assert pacer.wire_requests == 1
        sdk.get.assert_called_once_with("/account", {})
        assert sdk._retry == 0

    asyncio.run(run())


def test_sdk_encodes_decimal_strings_without_request_model_float_roundtrip(monkeypatch):
    from alpaca.trading.client import TradingClient

    sdk = TradingClient("fixture-key", "fixture-secret", paper=True, raw_data=True)
    captured = []

    def request(method, url, **kwargs):
        captured.append((method, url, kwargs))
        return NS(raise_for_status=lambda: None, text="{}", json=lambda: {})

    monkeypatch.setattr(sdk._session, "request", request)
    payload = boundary().submit_payload(order(), {}, "fixture-")
    sdk.post("/orders", payload)
    method, url, kwargs = captured[0]
    assert method == "POST"
    assert url == "https://paper-api.alpaca.markets/v2/orders"
    assert kwargs["allow_redirects"] is False
    assert json.loads(json.dumps(kwargs["json"]))["limit_price"] == "0.1234"
    assert json.loads(json.dumps(kwargs["json"]))["qty"] == "9"


def test_cancelled_queued_gateway_call_never_reaches_wire():
    async def run():
        from .conftest import controls

        clock = [0]
        pacer = AccountPacer(controls(), lambda: clock[0])
        pacer.bucket.tokens = 0
        sdk = Mock()
        gateway = boundary().AccountGateway(sdk, pacer, lambda: (0, 0))
        queued = asyncio.create_task(gateway.call(ActionKind.QUERY, "get", "/account"))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        clock[0] = 60_000_000_000
        pacer.drain()
        await asyncio.sleep(0)
        sdk.get.assert_not_called()
        assert not gateway.tasks

    asyncio.run(run())


def test_missing_reserve_authority_is_an_explicit_refusal():
    async def run():
        from .conftest import controls

        pacer = AccountPacer(controls(reserve_per_open_position=None), lambda: 0)
        sdk = Mock()
        gateway = boundary().AccountGateway(sdk, pacer, lambda: (0, 0))
        with pytest.raises(ValueError, match="reserve"):
            await asyncio.wait_for(gateway.call(ActionKind.SUBMIT, "post", "/orders"), 0.1)
        sdk.post.assert_not_called()

    asyncio.run(run())


def test_rate_limited_reply_pauses_the_account_pacer():
    # wire.py perform(): a 429 APIError calls pacer.on_rate_limited (pacer.py).
    async def run():
        import requests
        from alpaca.common.exceptions import APIError

        from .conftest import SEC, controls

        response = requests.Response()
        response.status_code = 429
        sdk = Mock()
        sdk.get.side_effect = APIError("{}", requests.HTTPError(response=response))
        pacer = AccountPacer(controls(), lambda: 5 * SEC)
        gateway = boundary().AccountGateway(sdk, pacer, lambda: (0, 0))
        with pytest.raises(APIError):
            await gateway.call(ActionKind.QUERY, "get", "/account")
        assert pacer.consecutive_429 == 1
        assert pacer.paused_until == 6 * SEC  # Fixture backoff_base_s 1.
        assert pacer.bucket.tokens == 0

    asyncio.run(run())


@pytest.mark.parametrize("offset", [1.25, 125.0])
def test_trailing_offset_text_refuses_a_float(offset):
    with pytest.raises(ValueError, match="decimal"):
        boundary().offset_text(offset)
    with pytest.raises(ValueError, match="decimal"):
        boundary().modify_payload(
            command(params={"trailing_offset": offset}), {"type": "trailing_stop"}
        )


def test_pacer_refusal_is_a_handled_not_sent_refusal_at_the_gateway():
    # pacer.py request(): refuse() then PacerRefused when entry reserve >= burst.
    async def run():
        import gc

        from sextant.risk.controls import ENTRY_RESERVE_EXCEEDS_BURST

        from .conftest import controls

        unretrieved = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))
        pacer = AccountPacer(controls(), lambda: 0)
        sdk, sent = Mock(), []
        # Fixture rows: reserve 1 per open and per exiting position, burst 18;
        # nine positions each with a working sell need reserve 10 + 9 = 19.
        gateway = boundary().AccountGateway(sdk, pacer, lambda: (9, 9))
        with pytest.raises(ValueError, match=ENTRY_RESERVE_EXCEEDS_BURST):
            await gateway.call(
                ActionKind.SUBMIT, "post", "/orders", on_wire=lambda: sent.append(True)
            )
        gc.collect()
        await asyncio.sleep(0)
        sdk.post.assert_not_called()
        assert not sent and not pacer.wire_requests and not gateway.tasks
        assert not unretrieved, "refusal future exception must be retrieved"

    asyncio.run(run())


@pytest.mark.parametrize(
    "method,path",
    [
        ("delete", "/orders"),
        ("delete", "/positions"),
        ("patch", "/account"),
        ("post", "/positions"),
    ],
)
def test_account_wide_mutations_are_refused_by_gateway(method, path):
    async def run():
        from .conftest import controls

        sdk = Mock()
        gateway = boundary().AccountGateway(
            sdk, AccountPacer(controls(), lambda: 0), lambda: (0, 0)
        )
        with pytest.raises(ValueError, match="endpoint"):
            await gateway.call(ActionKind.CANCEL, method, path)
        assert not gateway.pacer.wire_requests
        getattr(sdk, method).assert_not_called()

    asyncio.run(run())
