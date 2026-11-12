# Reviewer witness, unchanged in logic: formatting, import order and these pragma lines only.
# ruff: noqa: E501
# pyright: reportArgumentType=false
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from nautilus_trader.model import ClientOrderId, Money, Price

from .test_alpaca_client import client
from .test_alpaca_execution import command, order


@pytest.mark.parametrize("bind_before_first", [False, True])
def test_replayed_execution_does_not_switch_the_engine_order_that_owns_it(bind_before_first):
    async def run():
        node = client()
        native = order()
        node.cache.order.side_effect = lambda client_order_id: (
            native if client_order_id == native.client_order_id else None
        )
        await node._submit_order(SimpleNamespace(order=native, params={}))
        venue = node.gateway.sdk
        change = command(
            client_order_id=native.client_order_id,
            venue_order_id=None,
            price=Price.from_str("0.1235"),
        )
        await node._modify_order(change)
        successor = venue.confirm()
        if bind_before_first:
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
        successor.update(status="partially_filled", filled_qty="1", filled_avg_price="0.1235")
        venue.rows[successor["id"]] = deepcopy(successor)
        node.commission_for_fill = lambda data: Money.from_str("0.01 USD")
        update = {
            "stream": "trade_updates",
            "data": {
                "event": "partial_fill",
                "order": successor,
                "execution_id": "fixture-same-execution",
                "price": "0.1235",
                "qty": "1",
                "timestamp": successor["updated_at"],
            },
        }
        try:
            await node._on_trade_update(update)
            first = node._handle_report.call_args.args[0]
            first_lookup = node.cache.order(first.client_order_id)
            first_venue_known = successor["id"] in node._roots
            await node._generate_order_status_report(
                SimpleNamespace(
                    client_order_id=native.client_order_id,
                    venue_order_id=None,
                )
            )
            await node._on_trade_update(update)
            second = node._handle_report.call_args.args[0]
            print(
                f"bind_before_first={bind_before_first}; trade IDs={first.trade_id}/{second.trade_id}; "
                f"client IDs={first.client_order_id}/{second.client_order_id}; "
                f"first lookup exists={first_lookup is not None}; "
                f"first venue known={first_venue_known}"
            )
            assert first.trade_id == second.trade_id
            assert first.client_order_id == second.client_order_id == ClientOrderId("fixture-entry")
        finally:
            await node._disconnect()

    asyncio.run(run())
