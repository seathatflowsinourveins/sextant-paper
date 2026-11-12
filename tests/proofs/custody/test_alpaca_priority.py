"""Keep the native FIFO bounded while the account gateway owns wire priority.

NT d53849b python_adapters.md:167-174 and AX execution.rs command spawn_task.
"""

import asyncio
import threading
from types import SimpleNamespace as NS
from typing import Any, cast

from nautilus_trader.model import ClientOrderId

from sextant.alpaca.execution import AlpacaExecutionClient

from .test_alpaca_client import client
from .test_alpaca_execution import command, order


def test_native_command_fifo_releases_paced_entry_so_cancel_can_reach_gateway():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        original = next(iter(venue.rows.values()))
        start = len(venue.requests)
        node.gateway.pacer.bucket.tokens = 0
        queued = asyncio.Event()
        request = node.gateway.pacer.request

        def record_queue(*args, **kwargs):
            result = request(*args, **kwargs)
            if len(node.gateway.pacer.queue) == 2:
                queued.set()
            return result

        node.gateway.pacer.request = record_queue
        # Call the real native hook; the fixture's convenience wrapper waits for receipts.
        await asyncio.wait_for(
            AlpacaExecutionClient._submit_order(
                node,
                cast(
                    Any, NS(order=order(client_order_id=ClientOrderId("fixture-next")), params={})
                ),
            ),
            0.1,
        )
        await asyncio.wait_for(
            AlpacaExecutionClient._cancel_order(
                node,
                cast(
                    Any,
                    command(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None),
                ),
            ),
            0.1,
        )
        await asyncio.wait_for(queued.wait(), 1)
        assert len(node.gateway.pacer.queue) == 2
        deleted = asyncio.Event()
        loop = asyncio.get_running_loop()
        delete = venue.delete

        def record_delete(path, body):
            result = delete(path, body)
            loop.call_soon_threadsafe(deleted.set)
            return result

        venue.delete = record_delete
        node.gateway.pacer.bucket.tokens = 2
        node.gateway.pacer.drain()
        await asyncio.wait_for(deleted.wait(), 1)
        writes = [r for r in venue.requests[start:] if r[0] != "GET"]
        assert [(r[0], r[1]) for r in writes] == [("DELETE", f"/orders/{original['id']}")]
        node.gateway.pacer.bucket.tokens = node.gateway.pacer.bucket.burst
        node.gateway.pacer.drain()
        await asyncio.gather(*tuple(node._actions))
        assert [r[0] for r in venue.requests[start:] if r[0] != "GET"] == ["DELETE", "POST"]

    asyncio.run(run())


def test_cancel_withdraws_a_house_entry_that_has_not_reached_wire():
    async def run():
        node = client()
        node.gateway.pacer.bucket.tokens = 0
        queued, cancelled = asyncio.Event(), asyncio.Event()
        request = node.gateway.pacer.request

        def record_queue(*args, **kwargs):
            result = request(*args, **kwargs)
            queued.set()
            return result

        node.gateway.pacer.request = record_queue
        node.generate_order_canceled = lambda *args: cancelled.set()
        await AlpacaExecutionClient._submit_order(node, cast(Any, NS(order=order(), params={})))
        await asyncio.wait_for(queued.wait(), 1)
        await AlpacaExecutionClient._cancel_order(
            node,
            cast(Any, command(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None)),
        )
        await asyncio.wait_for(cancelled.wait(), 0.1)
        node.gateway.pacer.drain()
        await asyncio.wait_for(asyncio.gather(*tuple(node._actions)), 1)
        node.gateway.pacer.bucket.tokens = node.gateway.pacer.bucket.burst
        node.gateway.pacer.drain()
        assert not node.gateway.sdk.requests
        assert node.gateway.pacer.wire_requests == 0

    asyncio.run(run())


def test_disconnect_cancels_and_awaits_in_flight_command_tasks():
    # execution.py _disconnect: cancel then gather owned command tasks (AX shutdown).
    async def run():
        node = client()
        node.gateway.pacer.bucket.tokens = 0
        queued = asyncio.Event()
        request = node.gateway.pacer.request

        def record_queue(*args, **kwargs):
            result = request(*args, **kwargs)
            queued.set()
            return result

        node.gateway.pacer.request = record_queue
        await AlpacaExecutionClient._submit_order(node, cast(Any, NS(order=order(), params={})))
        await asyncio.wait_for(queued.wait(), 1)
        (task,) = tuple(node._actions)
        await asyncio.wait_for(node._disconnect(), 1)
        assert task.done() and task.cancelled()
        assert not node._actions
        node.gateway.pacer.bucket.tokens = node.gateway.pacer.bucket.burst
        node.gateway.pacer.drain()
        await asyncio.sleep(0)
        assert not node.gateway.sdk.requests

    asyncio.run(run())


def test_disconnect_rejects_a_queued_entry_with_positive_not_sent_evidence():
    async def run():
        node = client()
        node.gateway.pacer.bucket.tokens = 0
        queued = asyncio.Event()
        request = node.gateway.pacer.request

        def record_queue(*args, **kwargs):
            result = request(*args, **kwargs)
            queued.set()
            return result

        node.gateway.pacer.request = record_queue
        entry = order()
        await AlpacaExecutionClient._submit_order(node, cast(Any, NS(order=entry, params={})))
        await asyncio.wait_for(queued.wait(), 1)
        await asyncio.wait_for(node._disconnect(), 1)
        node.generate_order_rejected.assert_called_once()
        rejected, reason = node.generate_order_rejected.call_args.args[:2]
        assert rejected is entry and "not sent" in reason
        node.gateway.pacer.bucket.tokens = node.gateway.pacer.bucket.burst
        node.gateway.pacer.drain()
        await asyncio.sleep(0)
        assert not node.gateway.sdk.requests and not node.gateway.pacer.wire_requests

    asyncio.run(run())


def test_disconnect_leaves_a_sent_entry_for_reconciliation():
    async def run():
        node = client()
        started, release = threading.Event(), threading.Event()
        post = node.gateway.sdk.post

        def slow_post(path, body):
            started.set()
            release.wait(1)
            return post(path, body)

        node.gateway.sdk.post = slow_post
        await AlpacaExecutionClient._submit_order(node, cast(Any, NS(order=order(), params={})))
        await asyncio.to_thread(started.wait, 1)
        disconnect = asyncio.create_task(node._disconnect())
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(disconnect, 2)
        node.generate_order_rejected.assert_not_called()

    asyncio.run(run())
