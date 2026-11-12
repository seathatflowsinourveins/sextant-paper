"""R1 offline exact-assembled-head witnesses, native pinned client and SDK."""

import asyncio
import weakref
from unittest.mock import AsyncMock

import pytest
from alpaca.trading.stream import TradingStream
from nautilus_trader.model import ExecutionMassStatus
from websockets.exceptions import WebSocketException

from sextant.alpaca.execution import _observe

from .test_alpaca_client import client
from .test_alpaca_execution import order

LISTENING = {"stream": "listening", "data": {"streams": ["trade_updates"]}}


def mass_reports(node):
    return [
        call.args[0]
        for call in node._handle_report.call_args_list
        if isinstance(call.args[0], ExecutionMassStatus)
    ]


def test_recovery_is_bound_to_the_socket_ack_that_scheduled_it():
    async def run():
        node = client()
        node._listening = asyncio.get_running_loop().create_future()
        node._listening.set_result(None)
        node.stream_live = True
        scheduled = []
        node.create_task = lambda coroutine, name: scheduled.append(coroutine)
        try:
            node._on_stream("socket")
            node._on_stream("listening")
            assert len(scheduled) == 1
            node._on_stream("socket")  # Next loss before the scheduled task first runs.
            assert not node.stream_live
            await scheduled.pop()
            assert not node.coverage_complete and not mass_reports(node), (
                "recovery committed on a generation which has no subscription acknowledgement"
            )
        finally:
            for coroutine in scheduled:
                coroutine.close()
            await node._disconnect()

    asyncio.run(run())


def test_disconnect_revokes_coverage_before_sdk_backoff():
    async def run():
        node = client()
        node._listening = asyncio.get_running_loop().create_future()
        stream = TradingStream("offline", "offline", paper=True, raw_data=True)
        stream._connect = AsyncMock()
        stream._auth = AsyncMock()
        stream.close = AsyncMock()
        stream._trade_updates_handler = AsyncMock()

        async def subscribe():
            await stream._dispatch(LISTENING)

        stream._subscribe_trade_updates = subscribe
        _observe(stream, weakref.ref(node), asyncio.get_running_loop())
        observed = {}

        async def consume():
            await asyncio.sleep(0)  # Deliver the ACK to the node loop.
            assert node.stream_live
            node.coverage_complete = True
            raise WebSocketException("offline disconnect")

        async def backoff(retries):
            observed.update(live=node.stream_live, coverage=node.coverage_complete)
            stream._should_run = False

        stream._consume = consume
        stream._wait_before_reconnect = backoff
        try:
            await stream._run_forever()  # Real pinned SDK catch/close/backoff path, no socket.
            assert observed == {"live": False, "coverage": False}, (
                "SDK entered reconnect backoff while adapter coverage still authorizes absence"
            )
        finally:
            await node._disconnect()

    asyncio.run(run())


def test_superseded_ack_cannot_borrow_a_newer_acknowledgement():
    async def run():
        node = client()
        node._listening = asyncio.get_running_loop().create_future()
        node._listening.set_result(None)
        node.stream_live = True
        scheduled = []
        node.create_task = lambda coroutine, name: scheduled.append(coroutine)
        try:
            node._on_stream("socket")
            node._on_stream("listening")
            node._on_stream("socket")
            node._on_stream("listening")
            assert node.stream_live and len(scheduled) == 2
            await scheduled.pop(0)
            assert not node.gateway.sdk.requests and not mass_reports(node)
        finally:
            for coroutine in scheduled:
                coroutine.close()
            await node._disconnect()

    asyncio.run(run())


def test_superseded_recovery_stops_before_collecting_positions(monkeypatch):
    async def run():
        node = client()
        node._listening = asyncio.get_running_loop().create_future()
        node._listening.set_result(None)
        node.stream_live = True
        scheduled = []
        node.create_task = lambda coroutine, name: scheduled.append(coroutine)
        orders = node._generate_order_status_reports

        async def replaced_ack(command):
            result = await orders(command)
            node._on_stream("socket")
            node._on_stream("listening")
            return result

        monkeypatch.setattr(node, "_generate_order_status_reports", replaced_ack)
        try:
            await node._recover()
            assert not node.coverage_complete and not mass_reports(node)
            assert not [
                request for request in node.gateway.sdk.requests if request[1] == "/positions"
            ]
        finally:
            for coroutine in scheduled:
                coroutine.close()
            await node._disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("sdk_thread", [False, True])
def test_close_revokes_bulk_authority_before_socket_close_finishes(sdk_thread):
    from threading import Event

    async def run():
        node = client()
        node._listening = asyncio.get_running_loop().create_future()
        node._listening.set_result(None)
        node.stream_live = node.coverage_complete = True
        stream = TradingStream("offline", "offline", paper=True, raw_data=True)
        entered, release = Event(), Event()
        closing = None

        async def close_socket():
            entered.set()
            await asyncio.to_thread(release.wait)

        stream.close = close_socket
        _observe(stream, weakref.ref(node), asyncio.get_running_loop())
        try:
            if sdk_thread:
                closing = asyncio.create_task(
                    asyncio.to_thread(lambda: asyncio.run(stream.close()))
                )
            else:
                closing = asyncio.create_task(stream.close())
            assert await asyncio.to_thread(entered.wait, 1)
            assert not closing.done()
            assert not node.stream_live and not node.coverage_complete
            assert not node._provides_bulk_position_coverage(order().instrument_id)
        finally:
            release.set()
            if closing is not None:
                await asyncio.wait_for(closing, 1)
            await node._disconnect()

    asyncio.run(run())
