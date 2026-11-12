"""Forwarded exactly once (E5 u11-r5, review e5r5-fb8c2d4); EXIT-TABLE-r5b.md.

- One execution identity: (venue order id, execution id). It is checked after every await and
  recorded at the moment of forwarding, whatever path forwards it.
- Every path that confirms a lineage releases what was held for it, and coverage is not
  complete while anything is held.
- A refusal is emitted once per command, ever. The record is kept until a new command of that
  kind is sent for the order.
"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest
from nautilus_trader.model import ClientOrderId, Money, OrderStatus

from .test_alpaca_client import client, client_module
from .test_alpaca_execution import order
from .test_alpaca_exits import _cmd

ROOT = ClientOrderId("fixture-entry")


def _fill(row, execution="fixture-once"):
    row = deepcopy(row) | {
        "status": "partially_filled",
        "filled_qty": "1",
        "filled_avg_price": "0.1234",
    }
    data = {"event": "partial_fill", "order": row, "timestamp": row["updated_at"]}
    return {
        "stream": "trade_updates",
        "data": data | {"execution_id": execution, "qty": "1", "price": "0.1234"},
    }


def _fills(node):
    return [c.args[0] for c in node._handle_report.call_args_list if hasattr(c.args[0], "trade_id")]


async def _held(node):
    """A successor fill held for its lineage, then the venue confirms the successor."""
    await node._submit_order(NS(order=order(), params={}))
    node.commission_for_fill = lambda data: Money.from_str("0 USD")
    await node._modify_order(_cmd("modify"))
    update = _fill(node.gateway.sdk.pending[1])
    with pytest.raises(client_module().IncompleteCoverage):
        await node._on_trade_update(update)
    successor = node.gateway.sdk.confirm()
    node.gateway.sdk.rows[successor["id"]].update(update["data"]["order"])
    return update, successor


BULK = NS(open_only=False, start=None, end=None, instrument_id=None)


async def _confirm(node, path, successor):
    if path == "stream":
        new = deepcopy(successor) | {"status": "new"}
        await node._on_trade_update(
            {
                "stream": "trade_updates",
                "data": {"event": "new", "order": new, "timestamp": new["updated_at"]},
            }
        )
    elif path == "single_read":
        await node._generate_order_status_report(NS(client_order_id=ROOT, venue_order_id=None))
    elif path == "bulk_read":
        await node._generate_order_status_reports(BULK)
    else:  # the admission read of a cancel command confirms the lineage too
        await node._cancel_order(_cmd("cancel"))


@pytest.mark.parametrize("path", ["stream", "single_read", "bulk_read", "command_read"])
def test_every_confirming_path_forwards_a_held_execution_exactly_once(path):
    async def run():
        node = client()
        update, successor = await _held(node)
        await _confirm(node, path, successor)
        await node._on_trade_update(update)  # a replay after the release
        assert [str(f.client_order_id) for f in _fills(node)] == [str(ROOT)]
        assert not node._held and node.duplicate_executions == 1

    asyncio.run(run())


def test_coverage_is_not_complete_while_an_execution_is_held():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        node.commission_for_fill = lambda data: Money.from_str("0 USD")
        await node._modify_order(_cmd("modify"))
        with pytest.raises(client_module().IncompleteCoverage):
            await node._on_trade_update(_fill(node.gateway.sdk.pending[1]))
        with pytest.raises(client_module().IncompleteCoverage) as caught:
            await node._generate_order_status_reports(BULK)  # the lineage is still pending
        assert not node.coverage_complete and node._held
        assert any("held for its unconfirmed lineage" in str(e) for _, e in caught.value.failures)

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["cancel", "modify"])
def test_a_new_command_after_a_refusal_can_be_refused_again(kind):
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        row = next(iter(venue.rows.values()))
        pending = OrderStatus.PENDING_CANCEL if kind == "cancel" else OrderStatus.PENDING_UPDATE
        node.cache.order.return_value = order(status=pending)
        venue.delete = lambda path, body: venue.requests.append(("DELETE", path, {}))
        venue.patch = lambda path, body: venue.requests.append(("PATCH", path, {}))
        event = "order_cancel_rejected" if kind == "cancel" else "order_replace_rejected"
        refusal = {
            "stream": "trade_updates",
            "data": {"event": event, "order": row, "timestamp": row["updated_at"]},
        }
        emitter = getattr(node, f"generate_order_{kind}_rejected")
        for _ in range(2):  # two commands, each refused by the stream and then replayed
            await getattr(node, f"_{kind}_order")(_cmd(kind))
            await node._on_trade_update(refusal)
            await node._on_trade_update(refusal)
        assert emitter.call_count == 2  # once per command, never twice for one

    asyncio.run(run())
