# Reviewer witness, unchanged in logic: formatting, import order and these pragma lines only.
# ruff: noqa: RUF059
# pyright: reportArgumentType=false
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from nautilus_trader.model import ClientOrderId, Money, OrderStatus, Price

from sextant.execution.pacer import ActionKind

from .test_alpaca_client import client, client_module
from .test_alpaca_execution import command, order

ROOT = ClientOrderId("fixture-entry")


def mutation():
    return command(client_order_id=ROOT, venue_order_id=None, price=Price.from_str("0.1235"))


def envelope(event, row, execution_id="fixture-r5-held"):
    snapshot = deepcopy(row)
    data = {"event": event, "order": snapshot, "timestamp": snapshot["updated_at"]}
    if event in ("fill", "partial_fill"):
        snapshot.update(status="partially_filled", filled_qty="1", filled_avg_price="0.1235")
        data.update(execution_id=execution_id, qty="1", price="0.1235")
    return {"stream": "trade_updates", "data": data}


def fills(node):
    return [
        call.args[0]
        for call in node._handle_report.call_args_list
        if hasattr(call.args[0], "trade_id")
    ]


async def held_successor():
    node = client()
    await node._submit_order(SimpleNamespace(order=order(), params={}))
    await node._modify_order(mutation())
    pending = node.gateway.sdk.pending
    assert pending is not None
    update = envelope("partial_fill", pending[1])
    node.commission_for_fill = lambda data: Money.from_str("0.01 USD")
    with pytest.raises(client_module().IncompleteCoverage):
        await node._on_trade_update(update)
    assert not fills(node)
    assert node._held
    successor = node.gateway.sdk.confirm()
    node.gateway.sdk.rows[successor["id"]].update(update["data"]["order"])
    return node, update, successor


@pytest.mark.parametrize("replay_before_confirmation", [False, True])
def test_held_execution_replayed_before_confirmation_is_forwarded_once(replay_before_confirmation):
    async def run():
        node, update, successor = await held_successor()
        try:
            if replay_before_confirmation:
                await node._on_trade_update(update)
            else:
                await node._on_trade_update(envelope("new", successor))
                await node._on_trade_update(update)
            reports = fills(node)
            print(
                f"replay_before_confirmation={replay_before_confirmation}; reports={len(reports)}; "
                f"trades={[str(report.trade_id) for report in reports]}"
            )
            assert len(reports) == 1
            assert reports[0].client_order_id == ROOT
            assert not node._held
        finally:
            await node._disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("replay_while_release_waits", [False, True])
def test_replay_during_held_execution_identity_await_is_forwarded_once(replay_while_release_waits):
    async def run():
        node, update, successor = await held_successor()
        original_call = node._call
        entered, release = asyncio.Event(), asyncio.Event()
        confirmation = None
        parent_reads = []

        async def pause_release(kind, method, path, data=None, **kwargs):
            if (
                asyncio.current_task() is confirmation
                and path == f"/orders/{successor['replaces']}"
            ):
                parent_reads.append(path)
                if len(parent_reads) == 2:
                    entered.set()
                    await release.wait()
            return await original_call(kind, method, path, data, **kwargs)

        node._call = pause_release
        try:
            confirmation = asyncio.create_task(
                node._generate_order_status_report(
                    SimpleNamespace(client_order_id=ROOT, venue_order_id=None)
                )
            )
            await asyncio.wait_for(entered.wait(), 1)
            if replay_while_release_waits:
                await node._on_trade_update(update)
            release.set()
            await asyncio.wait_for(confirmation, 1)
            if not replay_while_release_waits:
                await node._on_trade_update(update)
            reports = fills(node)
            print(
                f"replay_while_release_waits={replay_while_release_waits}; reports={len(reports)}"
            )
            assert len(reports) == 1
            assert not node._held
        finally:
            release.set()
            if confirmation is not None:
                await asyncio.gather(confirmation, return_exceptions=True)
            await node._disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("confirmation_read", ["single", "bulk"])
def test_read_confirming_held_execution_lineage_releases_it(confirmation_read):
    async def run():
        node, update, successor = await held_successor()
        try:
            if confirmation_read == "single":
                await node._generate_order_status_report(
                    SimpleNamespace(client_order_id=ROOT, venue_order_id=None)
                )
            else:
                for _ in range(3):
                    await node._generate_order_status_reports(
                        SimpleNamespace(open_only=False, start=None, end=None, instrument_id=None)
                    )
            print(
                f"confirmation_read={confirmation_read}; reports={len(fills(node))}; "
                f"held={list(node._held)}; active={node._active}; coverage={node.coverage_complete}"
            )
            assert node._active[str(ROOT)] == successor["id"]
            assert len(fills(node)) == 1
            assert not node._held
        finally:
            await node._disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["cancel", "modify"])
@pytest.mark.parametrize("late_replays", [1, 2])
def test_read_refusal_is_not_repeated_by_replayed_stream_answer(kind, late_replays):
    async def run():
        node = client()
        await node._submit_order(SimpleNamespace(order=order(), params={}))
        venue = node.gateway.sdk
        row = next(iter(venue.rows.values()))
        native = order(
            status=OrderStatus.PENDING_CANCEL if kind == "cancel" else OrderStatus.PENDING_UPDATE
        )
        node.cache.order.return_value = native
        if kind == "cancel":
            venue.delete = lambda path, body: venue.requests.append(("DELETE", path, {}))
        try:
            await getattr(node, f"_{kind}_order")(mutation())
            row["status"] = "new"
            await node._generate_order_status_report(
                SimpleNamespace(client_order_id=ROOT, venue_order_id=None)
            )
            emitter = getattr(node, f"generate_order_{kind}_rejected")
            assert emitter.call_count == 1
            native.status = OrderStatus.ACCEPTED
            for _ in range(late_replays):
                await node._on_trade_update(
                    envelope(f"order_{kind if kind == 'cancel' else 'replace'}_rejected", row)
                )
            print(f"kind={kind}; late_replays={late_replays}; refusals={emitter.call_count}")
            assert emitter.call_count == 1
        finally:
            await node._disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["submit", "modify", "cancel"])
def test_enqueue_raise_recover_drain_never_sends_refused_command(kind):
    async def run():
        node = client()
        if kind != "submit":
            await node._submit_order(SimpleNamespace(order=order(), params={}))
        pacer = node.gateway.pacer
        original_drain = pacer.drain
        target = {
            "submit": ActionKind.SUBMIT,
            "modify": ActionKind.MODIFY,
            "cancel": ActionKind.CANCEL,
        }[kind]
        refused_tokens = []
        before = len(node.gateway.sdk.requests)

        def raise_after_enqueue():
            if pacer.queue and pacer.queue[0].kind == target:
                refused_tokens.extend(token for token in node._commands if token.kind == kind)
                raise RuntimeError("fixture pre-send failure after enqueue")
            return original_drain()

        pacer.drain = raise_after_enqueue
        try:
            native_command = (
                SimpleNamespace(order=order(), params={}) if kind == "submit" else mutation()
            )
            await getattr(node, f"_{kind}_order")(native_command)
            assert refused_tokens and pacer.queue
            assert all(
                token.withdrawn and token.settled and not token.sent for token in refused_tokens
            )
            emitter = getattr(
                node,
                "generate_order_rejected"
                if kind == "submit"
                else f"generate_order_{kind}_rejected",
            )
            emitter.assert_called_once()
            pacer.drain = original_drain
            pacer.bucket.tokens = pacer.bucket.burst
            pacer.drain()
            pacer.drain()
            await asyncio.gather(*tuple(node.gateway.tasks))
            writes = [
                request
                for request in node.gateway.sdk.requests[before:]
                if request[0] in ("POST", "PATCH", "DELETE")
            ]
            print(
                f"kind={kind}; withdrawn={all(token.withdrawn for token in refused_tokens)}; "
                f"late_writes={writes}; remaining_queue={len(pacer.queue)}"
            )
            assert not writes
            assert not pacer.queue
            emitter.assert_called_once()
        finally:
            pacer.drain = original_drain
            await node._disconnect()

    asyncio.run(run())
