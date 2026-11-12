"""T2 in time (E5 r5, review e5r4-266edf5): safety facts before the first await.

Every safety fact an event carries is recorded synchronously on arrival, keyed by the venue
order id the raw event names, before the first await: executing (a SELL's fill, partial fill
or stop), terminal (no cancel can still act), refused (the venue answered the mutation).
Lineage work afterwards only adds the stable binding. Every value read before an await is
re-validated after it. One proof per await; the list is EXIT-TABLE-r5.md.
"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from nautilus_trader.model import ClientOrderId, Money, OrderSide, OrderStatus, Quantity

from .test_alpaca_answers import QUERY
from .test_alpaca_client import client, client_module
from .test_alpaca_execution import order
from .test_alpaca_exits import _api, _cmd

EXECUTING = ["partial_fill", "stopped"]


def _event(event, row):
    row = deepcopy(row) | {"status": "partially_filled" if event == "partial_fill" else event}
    data = {"event": event, "order": row, "timestamp": row["updated_at"]}
    if event in ("fill", "partial_fill"):
        row.update(filled_qty="1", filled_avg_price="0.1234")
        data |= {"execution_id": f"fixture-{event}", "price": "0.1234", "qty": "1"}
    return {"stream": "trade_updates", "data": data}


async def _sell(node):
    native = order(side=OrderSide.SELL, is_sell=True, filled_qty=Quantity.from_str("0"))
    node.cache.order.return_value = native
    node.commission_for_fill = lambda data: Money.from_str("0 USD")
    await node._submit_order(NS(order=native, params={}))
    return next(iter(node.gateway.sdk.rows.values()))


def _hold(node, path):
    """Pause the first GET of path; return (entered, release)."""
    entered, release, call = asyncio.Event(), asyncio.Event(), node._call

    async def held(kind, method, where, data=None, **kwargs):
        if method == "get" and where == path and not entered.is_set():
            entered.set()
            await release.wait()
        return await call(kind, method, where, data, **kwargs)

    node._call = held
    return entered, release


async def _successor(node, *, sell=True):
    """A confirmed, bound successor of a house order; returns its row."""
    if sell:
        await _sell(node)
    else:
        await node._submit_order(NS(order=order(), params={}))
    await node._modify_order(_cmd("modify"))
    row = node.gateway.sdk.confirm()
    await node._on_trade_update(_event("new", row) | {})
    return row


FACTS = ["fill", "partial_fill", "stopped", "canceled", "expired", "rejected"]
FACTS += ["order_cancel_rejected", "order_replace_rejected"]


@pytest.mark.parametrize("event", FACTS)
def test_every_safety_fact_is_recorded_before_the_lineage_await(event):
    """While the successor's parent read is out (await A), the fact is already recorded."""

    async def run():
        node = client()
        row = await _successor(node)
        venue = node.gateway.sdk  # a sent, unanswered DELETE and PATCH on the successor
        venue.delete = lambda path, body: venue.rows[row["id"]].update(status="pending_cancel")
        venue.patch = lambda path, body: venue.requests.append(("PATCH", path, {}))
        for kind in ("cancel", "modify"):  # the modify's own read sees pending_cancel
            await getattr(node, f"_{kind}_order")(_cmd(kind))
            await asyncio.gather(*tuple(node._actions))
        assert node._cancels == {row["id"]} and node._proposals
        entered, release = _hold(node, f"/orders/{row['replaces']}")
        task = asyncio.create_task(node._on_trade_update(_event(event, row)))
        await asyncio.wait_for(entered.wait(), 1)
        if event in ("fill", "partial_fill", "stopped"):
            assert row["id"] in node._executing
        if event != "order_replace_rejected" and event not in ("partial_fill", "stopped"):
            assert row["id"] not in node._cancels  # terminal or refused: no cancel can act
        if event == "order_replace_rejected":
            assert not node._proposals  # the venue refused the replacement
        release.set()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


# One RED witness per r5 fix (each fails on the r4 client).


def test_a_cancel_refusal_a_read_emitted_is_not_emitted_again():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        row = next(iter(venue.rows.values()))
        node.cache.order.return_value = order(status=OrderStatus.PENDING_CANCEL)
        venue.delete = lambda path, body: venue.requests.append(("DELETE", path, {}))
        await node._cancel_order(_cmd("cancel"))  # an attempt; the order keeps working
        await node._generate_order_status_report(
            NS(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None)
        )  # the read finds it working and refuses the cancel
        await node._on_trade_update(_event("order_cancel_rejected", row))
        node.generate_order_cancel_rejected.assert_called_once()

    asyncio.run(run())


def test_an_executing_successor_is_fenced_before_its_parent_read():
    async def run():
        node = client()
        row = await _successor(node)
        entered, release = _hold(node, f"/orders/{row['replaces']}")
        task = asyncio.create_task(node._on_trade_update(_event("partial_fill", row)))
        await asyncio.wait_for(entered.wait(), 1)
        assert row["id"] in node._executing  # before the await, not after it
        release.set()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_a_newer_binding_made_during_an_older_parent_read_stands():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue, chain = node.gateway.sdk, []
        parent = next(iter(venue.rows.values()))
        for number in (1, 2, 3):
            venue.patch(f"/orders/{parent['id']}", {"client_order_id": f"fixture-entry-r{number}"})
            parent = venue.confirm()
            chain.append(deepcopy(parent))
        for memory in (node._active, node._roots, node._active_n):  # a cold restart
            memory.clear()
        entered, release = _hold(node, f"/orders/{chain[0]['replaces']}")
        older = asyncio.create_task(node._report(chain[0]))
        await asyncio.wait_for(entered.wait(), 1)
        await node._report(chain[2])
        release.set()
        await older
        assert node._active["fixture-entry"] == chain[2]["id"]

    asyncio.run(run())


def test_a_held_successor_fill_is_released_exactly_once_on_confirmation():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        node.commission_for_fill = lambda data: Money.from_str("0 USD")
        await node._modify_order(_cmd("modify"))
        fill = _event("partial_fill", node.gateway.sdk.pending[1])
        with pytest.raises(client_module().IncompleteCoverage):
            await node._on_trade_update(fill)  # lineage not yet confirmed: held
        confirmed = node.gateway.sdk.confirm()
        await node._on_trade_update(_event("new", confirmed))  # confirmation releases it

        def fills():
            calls = node._handle_report.call_args_list
            return [str(c.args[0].client_order_id) for c in calls if hasattr(c.args[0], "trade_id")]

        assert fills() == ["fixture-entry"]
        await node._on_trade_update(fill)  # a replay is not forwarded again
        assert fills() == ["fixture-entry"]

    asyncio.run(run())


def test_a_replace_refusal_a_read_emitted_is_not_emitted_again():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        row = next(iter(node.gateway.sdk.rows.values()))
        node.cache.order.return_value = order(status=OrderStatus.PENDING_UPDATE)
        node.gateway.sdk.patch = lambda path, body: node.gateway.sdk.requests.append(("PATCH",))
        await node._modify_order(_cmd("modify"))
        await node._generate_order_status_report(
            NS(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None)
        )  # the read finds the order unchanged and refuses the modify
        await node._on_trade_update(_event("order_replace_rejected", row | {"status": "new"}))
        node.generate_order_modify_rejected.assert_called_once()

    asyncio.run(run())


async def _held(node, ids=("a",)):  # Held fills, then the confirmed row.
    await node._submit_order(NS(order=order(), params={}))
    node.commission_for_fill = lambda data: Money.from_str("0 USD")
    await node._modify_order(_cmd("modify"))
    fills = [_event("partial_fill", node.gateway.sdk.pending[1]) for _ in ids]
    for fill, execution in zip(fills, ids, strict=True):
        fill["data"]["execution_id"] = execution
        await asyncio.gather(node._on_trade_update(fill), return_exceptions=True)
    return fills, node.gateway.sdk.confirm()


def _fills(node):
    reports = [c.args[0] for c in node._handle_report.call_args_list]
    return [str(r.trade_id) for r in reports if hasattr(r, "trade_id")]


def test_a_bound_successor_entering_its_own_replace_stays_confirmed():  # PR 588 P1-a
    async def run():
        node = client()
        row = await _successor(node, sell=False)
        await node._modify_order(_cmd("modify"))
        await node._on_trade_update(_event("pending_replace", node.gateway.sdk.rows[row["id"]]))
        assert str((await node._generate_order_status_report(QUERY)).venue_order_id) == row["id"]

    asyncio.run(run())


@pytest.mark.parametrize("parent", [True, False])  # P1-b; P2-a: the fill's own replay
def test_each_confirming_event_releases_a_held_fill_exactly_once(parent):
    async def run():
        node = client()
        fills, child = await _held(node)
        replaced = _event("replaced", node.gateway.sdk.rows[child["replaces"]])
        await node._on_trade_update(replaced if parent else fills[0])
        assert _fills(node) == ["a"]

    asyncio.run(run())


def test_a_failed_replay_keeps_every_held_fill_in_order():  # P1-c
    async def run():
        node = client()
        child = (await _held(node, ("a", "b")))[1]
        reads, call = iter(range(9)), node._call

        async def flaky(kind, method, where, data=None, **kwargs):
            if where.endswith(child["replaces"]) and next(reads) == 1:  # The first replay
                raise ConnectionError
            return await call(kind, method, where, data, **kwargs)

        node._call = flaky
        await asyncio.gather(node._on_trade_update(_event("new", child)), return_exceptions=True)
        await node._generate_order_status_report(QUERY)
        assert _fills(node) == ["a", "b"]

    asyncio.run(run())


def test_a_late_refusal_leaves_an_unsent_modify_reserved():  # P2-b
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue, pacer = node.gateway.sdk, node.gateway.pacer
        row, venue.patch = next(iter(venue.rows.values())), Mock(side_effect=_api(422))
        await node._modify_order(_cmd("modify"))  # Refused; its stream echo comes late.
        node.generate_order_modify_rejected.reset_mock()
        pacer.bucket.tokens = 1  # Read passes; PATCH waits, unsent.
        task = asyncio.create_task(node._modify_order(_cmd("modify")))
        await asyncio.sleep(0.01)
        assert pacer.queue
        await node._on_trade_update(_event("order_replace_rejected", row | {"status": "new"}))
        node.generate_order_modify_rejected.assert_not_called()
        assert list(node._proposals) == ["fixture-entry-r2"]
        task.cancel()

    asyncio.run(run())
