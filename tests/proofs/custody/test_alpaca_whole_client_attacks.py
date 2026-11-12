# Reviewer witness, unchanged in logic: formatting, import order and these pragma lines only.
# ruff: noqa: F841
# pyright: reportArgumentType=false
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from nautilus_trader.model import ClientOrderId, Money, OrderSide, OrderStatus, Price, Quantity

from .test_alpaca_client import client, client_module
from .test_alpaca_execution import command, order


def query():
    return SimpleNamespace(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None)


@pytest.mark.parametrize("venue_state", ["pending_cancel", "new"])
def test_acknowledged_delete_is_not_terminal_cancellation_evidence(venue_state):
    async def run():
        node = client()
        native = order(side=OrderSide.SELL, is_sell=True, filled_qty=Quantity.from_str("0"))
        await node._submit_order(SimpleNamespace(order=native, params={}))
        venue = node.gateway.sdk
        row = next(iter(venue.rows.values()))

        def acknowledge_cancel(path, body):
            venue.requests.append(("DELETE", path, dict(body or {})))
            row["status"] = "pending_cancel"

        venue.delete = acknowledge_cancel
        node.cache.order.return_value = order(
            side=OrderSide.SELL,
            is_sell=True,
            filled_qty=Quantity.from_str("0"),
            status=OrderStatus.PENDING_CANCEL,
        )
        await node._cancel_order(command(**vars(query())))
        row["status"] = venue_state
        report = await node._generate_order_status_report(query())
        print(
            f"204 acknowledged DELETE; venue={venue_state}; "
            f"report={report.order_status}; markers={node._cancels}; "
            f"refusals={node.generate_order_cancel_rejected.call_count}"
        )
        try:
            if venue_state == "pending_cancel":
                assert row["id"] in node._cancels
            else:
                assert report.order_status == OrderStatus.ACCEPTED
                node.generate_order_cancel_rejected.assert_called_once()
        finally:
            await node._disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("event", ["fill", "partial_fill"])
@pytest.mark.parametrize("bind_first", [False, True])
def test_first_successor_execution_uses_stable_engine_identity(event, bind_first):
    async def run():
        node = client()
        await node._submit_order(SimpleNamespace(order=order(), params={}))
        venue = node.gateway.sdk
        parent = next(iter(venue.rows.values()))
        await node._modify_order(command(**vars(query()), price=Price.from_str("0.1235")))
        successor = venue.confirm()
        if bind_first:
            await node._on_trade_update(
                {
                    "stream": "trade_updates",
                    "data": {
                        "event": "new",
                        "order": successor,
                        "timestamp": successor["updated_at"],
                    },
                }
            )
        quantity = "9" if event == "fill" else "1"
        successor.update(
            status="filled" if event == "fill" else "partially_filled",
            filled_qty=quantity,
            filled_avg_price="0.1235",
        )
        venue.rows[successor["id"]] = deepcopy(successor)
        node.commission_for_fill = lambda data: Money.from_str("0.01 USD")
        node._handle_report.reset_mock()
        try:
            await node._on_trade_update(
                {
                    "stream": "trade_updates",
                    "data": {
                        "event": event,
                        "order": successor,
                        "execution_id": "fixture-first-fill",
                        "price": "0.1235",
                        "qty": quantity,
                        "timestamp": successor["updated_at"],
                    },
                }
            )
            report = node._handle_report.call_args.args[0]
            print(
                f"first successor {event}, bind_first={bind_first}; "
                f"report client={report.client_order_id}; "
                f"venue={report.venue_order_id}; active={node._active}; "
                f"updates={node.generate_order_updated.call_count}"
            )
            assert report.client_order_id == ClientOrderId("fixture-entry")
            assert node._active["fixture-entry"] == successor["id"]
        finally:
            await node._disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("cold", [False, True])
def test_cold_two_hop_binding_does_not_adopt_a_late_original_snapshot(cold):
    async def run():
        node = client()
        await node._submit_order(SimpleNamespace(order=order(), params={}))
        venue = node.gateway.sdk
        original = deepcopy(next(iter(venue.rows.values())))
        parent = original
        for suffix in (1, 2):
            venue.patch(
                "/orders/" + parent["id"],
                {
                    "client_order_id": f"fixture-entry-r{suffix}",
                    "limit_price": "0.1235",
                },
            )
            parent = venue.confirm()
        if cold:
            node._active.clear()
            node._roots.clear()
        try:
            await node._report(parent)
            assert node._active["fixture-entry"] == parent["id"]
            report = await node._report(original)
            print(
                f"two-hop cold={cold}; latest={parent['id']}; active={node._active}; late={report}"
            )
            assert node._active["fixture-entry"] == parent["id"]
            assert report is None
        finally:
            await node._disconnect()

    asyncio.run(run())


def test_successfully_canceled_delete_read_is_closed_without_refusal():
    async def run():
        node = client()
        await node._submit_order(SimpleNamespace(order=order(), params={}))
        node.cache.order.return_value = order(status=OrderStatus.PENDING_CANCEL)
        await node._cancel_order(command(**vars(query())))
        report = await node._generate_order_status_report(query())
        assert report.order_status == OrderStatus.CANCELED
        node.generate_order_cancel_rejected.assert_not_called()
        assert not node._cancels
        await node._disconnect()

    asyncio.run(run())


def test_disposed_native_runtime_refuses_unsent_submit_without_a_real_node():
    async def run():
        node = client()
        node._runtime.bind(asyncio.get_running_loop())
        node._runtime.dispose()
        node.create_task = lambda coroutine, name: client_module().ExecutionClient.create_task(
            node,
            coroutine,
            name,
        )
        with pytest.raises(RuntimeError, match="not bound"):
            await node._submit_order(SimpleNamespace(order=order(), params={}))
        assert not node.gateway.sdk.requests
        node.generate_order_denied.assert_called_once()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["submit", "modify", "cancel"])
def test_scheduling_failure_refuses_unsent_command(kind):
    async def run():
        node = client()

        def scheduling_failure(coroutine, name):
            raise RuntimeError("fixture: task scheduling failed before wire")

        node.create_task = scheduling_failure
        native_command = (
            SimpleNamespace(order=order(), params={})
            if kind == "submit"
            else command(**vars(query()), price=Price.from_str("0.1235"))
        )
        with pytest.raises(RuntimeError, match="scheduling failed"):
            await getattr(node, "_" + kind + "_order")(native_command)
        assert not node.gateway.sdk.requests
        if kind == "submit":
            node.generate_order_denied.assert_called_once()
        else:
            getattr(node, "generate_order_" + kind + "_rejected").assert_called_once()

    asyncio.run(run())
