# Reviewer witness, unchanged in logic: formatting, import order and these pragma lines only.
# ruff: noqa: B007, E501
# pyright: reportArgumentType=false
import asyncio
import weakref
from copy import deepcopy
from types import SimpleNamespace

import pytest
from nautilus_trader.model import ClientOrderId, Money, OrderSide, OrderStatus, Price, Quantity

from .test_alpaca_client import client, client_module
from .test_alpaca_execution import command, order

ROOT = ClientOrderId("fixture-entry")


def mutation():
    return command(client_order_id=ROOT, venue_order_id=None, price=Price.from_str("0.1235"))


def update(event, row):
    payload = {
        "event": event,
        "order": deepcopy(row),
        "timestamp": row["updated_at"],
    }
    if event in ("partial_fill", "fill"):
        payload |= {"execution_id": "fixture-held-fill", "qty": "1", "price": "0.1235"}
        payload["order"].update(
            status="partially_filled", filled_qty="1", filled_avg_price="0.1235"
        )
    elif event == "stopped":
        payload["order"]["status"] = "stopped"
    return {"stream": "trade_updates", "data": payload}


async def sell_successor():
    node = client()
    native = order(side=OrderSide.SELL, is_sell=True, filled_qty=Quantity.from_str("0"))
    node.cache.order.return_value = native
    await node._submit_order(SimpleNamespace(order=native, params={}))
    await node._modify_order(mutation())
    successor = node.gateway.sdk.confirm()
    await node._on_trade_update(update("new", successor))
    node.commission_for_fill = lambda data: Money.from_str("0.01 USD")
    return node, successor


@pytest.mark.parametrize("event", ["partial_fill", "stopped"])
@pytest.mark.parametrize("kind", ["cancel", "modify"])
@pytest.mark.parametrize("finish_identity_first", [False, True])
def test_executing_successor_is_fenced_while_ingress_waits_for_parent(
    event, kind, finish_identity_first
):
    async def run():
        node, successor = await sell_successor()
        venue = node.gateway.sdk
        original_call = node._call
        entered, release = asyncio.Event(), asyncio.Event()
        observed, resume_command = asyncio.Event(), asyncio.Event()
        original_current = node._current
        ingress = None
        pending_command = None

        async def pause_after_snapshot(native_command):
            snapshot = await original_current(native_command)
            observed.set()
            await resume_command.wait()
            return snapshot

        async def delayed_identity(action, method, path, data=None, **kwargs):
            if (
                asyncio.current_task() is ingress
                and method == "get"
                and path == "/orders/" + successor["replaces"]
            ):
                entered.set()
                await release.wait()
            return await original_call(action, method, path, data, **kwargs)

        node._call = delayed_identity
        node._current = pause_after_snapshot
        try:
            requested_change = mutation()
            if kind == "modify":
                requested_change.quantity = Quantity.from_str("8")
            pending_command = asyncio.create_task(
                getattr(node, "_" + kind + "_order")(requested_change)
            )
            await asyncio.wait_for(observed.wait(), 1)
            incoming = update(event, successor)
            venue.rows[successor["id"]].update(incoming["data"]["order"])
            ingress = asyncio.create_task(node._on_trade_update(incoming))
            await asyncio.wait_for(entered.wait(), 1)
            if finish_identity_first:
                release.set()
                await ingress
            before = len(venue.requests)
            resume_command.set()
            outcomes = await asyncio.gather(pending_command, return_exceptions=True)
            writes = [
                request for request in venue.requests[before:] if request[0] in ("DELETE", "PATCH")
            ]
            print(
                f"event={event}; mutation={kind}; finish_identity_first={finish_identity_first}; "
                f"executing_before_release={node._executing}; new_writes={writes}; "
                f"venue_qty={venue.rows[successor['id']]['qty']}; "
                f"venue_status={venue.rows[successor['id']]['status']}; "
                f"command_error={type(outcomes[0]).__name__}"
            )
            assert not writes, "an already observed executing SELL reached a mutation endpoint"
        finally:
            release.set()
            resume_command.set()
            if ingress is not None:
                await asyncio.gather(ingress, return_exceptions=True)
            if pending_command is not None:
                await asyncio.gather(pending_command, return_exceptions=True)
            await node._disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("replay_explicitly", [False, True])
def test_unconfirmed_fill_is_released_after_its_parent_confirms(replay_explicitly):
    async def run():
        node = client()
        await node._submit_order(SimpleNamespace(order=order(), params={}))
        await node._modify_order(mutation())
        venue = node.gateway.sdk
        successor = deepcopy(venue.pending[1])
        fill = update("partial_fill", successor)
        node.commission_for_fill = lambda data: Money.from_str("0.01 USD")
        node._handle_report.reset_mock()
        try:
            await client_module()._deliver_update(weakref.ref(node), fill)
            assert len(node.failures) == 1
            assert node.failures[0][0] is fill
            assert not node.coverage_complete
            node._handle_report.assert_not_called()
            confirmed = venue.confirm()
            venue.rows[confirmed["id"]].update(fill["data"]["order"])
            await node._on_trade_update(update("new", venue.rows[confirmed["id"]]))
            await node._generate_order_status_report(
                SimpleNamespace(client_order_id=ROOT, venue_order_id=None)
            )
            if replay_explicitly:
                await node._on_trade_update(fill)
            fills = [
                call.args[0]
                for call in node._handle_report.call_args_list
                if hasattr(call.args[0], "trade_id")
            ]
            print(
                f"explicit_replay={replay_explicitly}; fills={len(fills)}; "
                f"active={node._active}; diagnostic_failures={len(node.failures)}"
            )
            assert len(fills) == 1, "the held execution was not released by confirmed lineage"
            assert fills[0].client_order_id == ROOT
        finally:
            await node._disconnect()

    asyncio.run(run())


def test_replace_ack_then_successor_rejection_is_forwarded():
    async def run():
        node = client()
        await node._submit_order(SimpleNamespace(order=order(), params={}))
        await node._modify_order(mutation())
        assert node._proposals
        successor = node.gateway.sdk.confirm()
        successor["status"] = "rejected"
        node.gateway.sdk.rows[successor["id"]] = deepcopy(successor)
        try:
            await node._on_trade_update(update("rejected", successor))
            report = node._handle_report.call_args.args[0]
            assert report.client_order_id == ROOT
            assert report.order_status == OrderStatus.REJECTED
            assert not node._proposals
        finally:
            await node._disconnect()

    asyncio.run(run())


def test_replaced_event_not_listed_in_t2_binds_the_confirmed_successor():
    async def run():
        node = client()
        await node._submit_order(SimpleNamespace(order=order(), params={}))
        await node._modify_order(mutation())
        successor = node.gateway.sdk.confirm()
        parent = node.gateway.sdk.rows[successor["replaces"]]
        try:
            await node._on_trade_update(update("replaced", parent))
            assert node._active[str(ROOT)] == successor["id"]
            assert not node._proposals
            assert node.generate_order_updated.call_count == 1
        finally:
            await node._disconnect()

    asyncio.run(run())


def test_bound_successor_pending_replace_releases_on_confirmed_next_successor():
    async def run():
        node, successor = await sell_successor()
        await node._modify_order(mutation())
        pending = node.gateway.sdk.rows[successor["id"]]
        try:
            await node._on_trade_update(update("pending_replace", pending))  # PR 588 P1-a
            assert node._active[str(ROOT)] == successor["id"]
            child = node.gateway.sdk.confirm()
            await node._on_trade_update(update("new", child))
            assert node._active[str(ROOT)] == child["id"]
            assert not node._proposals
            await node._cancel_order(mutation())
            assert node.gateway.sdk.requests[-1][0:2] == ("DELETE", "/orders/" + child["id"])
        finally:
            await node._disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["cancel", "modify"])
def test_executing_successor_remains_protected_after_ordinary_ingress(kind):
    async def run():
        node, successor = await sell_successor()
        try:
            await node._on_trade_update(update("partial_fill", successor))
            before = len(node.gateway.sdk.requests)
            await getattr(node, "_" + kind + "_order")(mutation())
            assert not any(
                request[0] in ("DELETE", "PATCH") for request in node.gateway.sdk.requests[before:]
            )
        finally:
            await node._disconnect()

    asyncio.run(run())


def test_cold_first_successor_fill_uses_stable_identity_without_a_modify_hook():
    async def run():
        node = client()
        native = order()
        node.cache.order.side_effect = lambda identity: native if identity == ROOT else None
        await node._submit_order(SimpleNamespace(order=native, params={}))
        venue = node.gateway.sdk
        original = next(iter(venue.rows.values()))
        venue.patch(
            "/orders/" + original["id"],
            {"client_order_id": str(ROOT) + "-r1", "limit_price": "0.1235"},
        )
        successor = venue.confirm()
        node._active.clear()
        node._roots.clear()
        if hasattr(node, "_active_n"):
            node._active_n.clear()
        node.commission_for_fill = lambda data: Money.from_str("0.01 USD")
        try:
            await node._on_trade_update(update("partial_fill", successor))
            report = node._handle_report.call_args.args[0]
            print(f"cold successor fill stable={report.client_order_id}; active={node._active}")
            assert report.client_order_id == ROOT
            assert node.cache.order(report.client_order_id) is native
        finally:
            await node._disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("finish_older_first", [False, True])
def test_concurrent_cold_lineage_reads_cannot_reverse_a_newer_binding(finish_older_first):
    async def run():
        node = client()
        await node._submit_order(SimpleNamespace(order=order(), params={}))
        venue = node.gateway.sdk
        parent = next(iter(venue.rows.values()))
        ancestors = []
        for number in (1, 2, 3):
            venue.patch(
                "/orders/" + parent["id"],
                {"client_order_id": f"{ROOT}-r{number}", "limit_price": "0.1235"},
            )
            parent = venue.confirm()
            ancestors.append(deepcopy(parent))
        older, newest = ancestors[0], ancestors[-1]
        node._active.clear()
        node._roots.clear()
        node._active_n.clear()
        entered, release = asyncio.Event(), asyncio.Event()
        original_call = node._call
        delayed = None

        async def delay_older(action, method, path, data=None, **kwargs):
            if asyncio.current_task() is delayed and path == "/orders/" + older["replaces"]:
                entered.set()
                await release.wait()
            return await original_call(action, method, path, data, **kwargs)

        node._call = delay_older
        try:
            delayed = asyncio.create_task(node._report(older))
            await asyncio.wait_for(entered.wait(), 1)
            if finish_older_first:
                release.set()
                await delayed
            await node._report(newest)
            assert node._active[str(ROOT)] == newest["id"]
            release.set()
            await delayed
            print(
                f"finish_older_first={finish_older_first}; newest={newest['id']}; "
                f"final_active={node._active}; final_sequence={node._active_n}"
            )
            assert node._active[str(ROOT)] == newest["id"]
            assert node._active_n[str(ROOT)] == 3
        finally:
            release.set()
            if delayed is not None:
                await asyncio.gather(delayed, return_exceptions=True)
            await node._disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("newest_first", [False, True])
def test_cold_bulk_two_hop_reconciliation_is_independent_of_timestamp_ties(newest_first):
    async def run():
        node = client()
        await node._submit_order(SimpleNamespace(order=order(), params={}))
        venue = node.gateway.sdk
        for number in (1, 2):
            await node._modify_order(mutation())
            child = venue.confirm()
            await node._on_trade_update(update("new", child))
        if newest_first:
            venue.rows = dict(reversed(tuple(venue.rows.items())))
        node._active.clear()
        node._roots.clear()
        node._active_n.clear()
        try:
            reports = await node._generate_order_status_reports(
                SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)
            )
            print(
                f"cold bulk newest_first={newest_first}; reports={len(reports)}; "
                f"active={node._active}; coverage={node.coverage_complete}"
            )
            assert len(reports) == 1
            assert str(reports[0].venue_order_id) == child["id"]
            assert node.coverage_complete
        except client_module().IncompleteCoverage as failure:
            print(
                f"cold bulk newest_first={newest_first}; retained_reports={len(failure.reports)}; "
                f"errors={[(payload['client_order_id'], str(error)) for payload, error in failure.failures]}"
            )
            raise
        finally:
            await node._disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["cancel", "modify"])
@pytest.mark.parametrize("answer_by_read", [False, True])
def test_late_stream_refusal_does_not_repeat_an_authoritative_read_refusal(kind, answer_by_read):
    async def run():
        node = client()
        await node._submit_order(SimpleNamespace(order=order(), params={}))
        venue = node.gateway.sdk
        row = next(iter(venue.rows.values()))
        node.cache.order.return_value = order(
            status=OrderStatus.PENDING_CANCEL if kind == "cancel" else OrderStatus.PENDING_UPDATE
        )
        if kind == "cancel":
            venue.delete = lambda path, body: venue.requests.append(("DELETE", path, {}))
        await getattr(node, "_" + kind + "_order")(mutation())
        row["status"] = "new"
        emitter = getattr(node, "generate_order_" + kind + "_rejected")
        try:
            if answer_by_read:
                await node._generate_order_status_report(
                    SimpleNamespace(client_order_id=ROOT, venue_order_id=None)
                )
                assert emitter.call_count == 1
                node.cache.order.return_value.status = OrderStatus.ACCEPTED
            event = "order_cancel_rejected" if kind == "cancel" else "order_replace_rejected"
            await node._on_trade_update(update(event, row))
            print(f"kind={kind}; read_first={answer_by_read}; refusals={emitter.call_count}")
            assert emitter.call_count == 1, "the read and stream refused one command twice"
        finally:
            await node._disconnect()

    asyncio.run(run())
