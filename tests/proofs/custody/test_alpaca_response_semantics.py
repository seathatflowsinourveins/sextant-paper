"""T1: what each broker response means, and which evidence it keeps (E5 r4).

Alpaca API reference (orders, read 2026-11-10):
- POST /v2/orders "Places a new order" (200), 403/422 refuse;
- PATCH /v2/orders/{id}: "A success return code from a replaced order does NOT guarantee
  the existing open order has been replaced";
- DELETE /v2/orders/{id}: "Attempts to cancel an Open Order" (204), 422 "not cancelable".
alpaca-py cc4cb3b7 common/rest.py:197-210 maps 4xx/5xx to APIError and returns the 2xx body.
A 2xx that acknowledges an attempt never clears evidence; only a terminal venue confirmation
or an authoritative refusal does. The table is EXIT-TABLE-r4.md, T1.
"""

import asyncio
from types import SimpleNamespace as NS

import pytest
import requests
from nautilus_trader.model import ClientOrderId, Money, OrderStatus

from .test_alpaca_client import client
from .test_alpaca_execution import order
from .test_alpaca_exits import _api, _cmd, _read

CLASSES = ["2xx", "4xx", "5xx", "none"]


def _answer(venue, method, cls, *, apply):
    original = getattr(venue, method)

    def respond(path, body):
        if cls == "2xx":
            return original(path, body)
        venue.requests.append((method.upper(), path, dict(body or {})))
        if cls == "4xx":
            raise _api(422)
        if apply:
            original(path, body)
        raise _api(500) if cls == "5xx" else requests.ConnectionError("no response")

    setattr(venue, method, respond)


def _stream(node, event, row):
    return node._on_trade_update(
        {
            "stream": "trade_updates",
            "data": {"event": event, "order": row, "timestamp": row["updated_at"]},
        }
    )


@pytest.mark.parametrize("cls", CLASSES)
def test_post_response_classes(cls):
    async def run():
        node = client()
        _answer(node.gateway.sdk, "post", cls, apply=False)
        try:
            await node._submit_order(NS(order=order(), params={}))
        except Exception:
            assert cls in ("5xx", "none")  # Unresolved: the supervised task reports it.
        if cls == "2xx":  # The venue's own order: its status is reported, nothing cleared.
            assert node._handle_report.call_args.args[0].order_status == OrderStatus.ACCEPTED
            row = next(iter(node.gateway.sdk.rows.values()))
            await _stream(node, "rejected", row | {"status": "rejected"})  # A later venue answer.
            assert node._handle_report.call_args.args[0].order_status == OrderStatus.REJECTED
        elif cls == "4xx":
            node.generate_order_rejected.assert_called_once()
        else:  # SUBMITTED stays; the engine's own query reads the venue later.
            node.generate_order_rejected.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("cls", CLASSES)
def test_patch_response_classes(cls):
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        _answer(venue, "patch", cls, apply=False)
        try:
            await node._modify_order(_cmd("modify"))
        except Exception:
            assert cls in ("4xx", "5xx", "none")
        # 2xx is a proposal, not a replacement; the broker documents no PATCH refusal, so a
        # 4xx (E5 u12), a 5xx or no answer is unknown: the proposal stays.
        assert list(node._proposals) == ["fixture-entry-r1"]
        node.generate_order_modify_rejected.assert_not_called()
        if cls == "2xx":  # Resolved only by the venue: here its replace refusal.
            parent = next(iter(venue.rows.values()))
            await _stream(node, "order_replace_rejected", parent | {"status": "new"})
            node.generate_order_modify_rejected.assert_called_once()
            assert not node._proposals

    asyncio.run(run())


@pytest.mark.parametrize("cls", CLASSES)
def test_delete_response_classes(cls):
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        row = next(iter(venue.rows.values()))
        if cls == "2xx":  # The attempt is acknowledged; the venue has not cancelled yet.
            venue.delete = lambda path, body: venue.requests.append(("DELETE", path, {}))
        else:
            _answer(venue, "delete", cls, apply=False)
        try:
            await node._cancel_order(_cmd("cancel"))
        except Exception:
            assert cls in ("5xx", "none")
        if cls == "4xx":
            node.generate_order_cancel_rejected.assert_called_once()
            assert not node._cancels
            return
        assert node._cancels == {row["id"]}  # 2xx is an attempt; 5xx/none are unknown.
        row["status"] = "pending_cancel"
        await _read(node, "cancel")
        assert node._cancels == {row["id"]}  # Still waiting at the venue: kept.
        row["status"] = "new"
        await _read(node, "cancel")  # Authoritative: still working, so refused once.
        node.generate_order_cancel_rejected.assert_called_once()
        assert not node._cancels

    asyncio.run(run())


@pytest.mark.parametrize(
    "event", ["canceled", "fill", "expired", "rejected", "order_cancel_rejected"]
)
def test_an_acknowledged_delete_is_resolved_by_a_terminal_or_refusing_stream_event(event):
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        row = next(iter(venue.rows.values()))
        venue.delete = lambda path, body: venue.requests.append(("DELETE", path, {}))
        await node._cancel_order(_cmd("cancel"))
        assert node._cancels == {row["id"]}
        node.commission_for_fill = lambda data: Money.from_str("0 USD")
        status = {
            "canceled": "canceled",
            "fill": "filled",
            "expired": "expired",
            "rejected": "rejected",
        }
        changed = row | {"status": status.get(event, "new")}
        if event == "fill":
            changed |= {"filled_qty": changed["qty"], "filled_avg_price": "0.1234"}
            update = {
                "event": "fill",
                "order": changed,
                "execution_id": "fixture-x",
                "price": "0.1234",
                "qty": changed["qty"],
                "timestamp": row["updated_at"],
            }
            await node._on_trade_update({"stream": "trade_updates", "data": update})
        else:
            await _stream(node, event, changed)
        assert not node._cancels
        refused = node.generate_order_cancel_rejected.call_count
        assert refused == (1 if event == "order_cancel_rejected" else 0)

    asyncio.run(run())


def test_a_query_by_client_id_answers_an_unresolved_post():
    async def run():
        node = client()
        _answer(node.gateway.sdk, "post", "5xx", apply=True)  # Applied, reply lost.
        with pytest.raises(Exception, match="fixture"):  # The 5xx: unresolved, not rejected.
            await node._submit_order(NS(order=order(), params={}))
        report = await node._generate_order_status_report(
            NS(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None)
        )
        assert report.order_status == OrderStatus.ACCEPTED  # The venue's state, not a guess.
        node.generate_order_rejected.assert_not_called()

    asyncio.run(run())
