"""E5 u13: the trade stream's state is known, and startup reads wait for its subscription.

The broker acknowledges a listen request with {"stream": "listening", "data": {"streams":
["trade_updates"]}} (Alpaca websocket-streaming page, read 2026-11-11, updatedAt 2025-09-24).
alpaca-py cc4cb3b7 sends the listen request without awaiting that acknowledgement
(alpaca/trading/stream.py:141-145, 164-168), its dispatcher drops every non trade_updates
message (:117-126), and it reconnects on any error without telling its caller (:204-241).
The official adapters read history only after the subscription is confirmed (NautilusTrader
d53849b2 crates/adapters/hyperliquid/src/execution.rs:3114-3120; derive/src/execution.rs:
1139-1156), and a lost stream is made visible at once (derive/src/execution.rs:805-807,
842-846).
"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from alpaca.trading.stream import TradingStream
from nautilus_trader.model import Money

from .conftest import controls
from .test_alpaca_client import client, client_module

LISTENING = {"stream": "listening", "data": {"streams": ["trade_updates"]}}
READS = {
    "orders": ("_generate_order_status_reports", "/orders"),
    "positions": ("_generate_position_status_reports", "/positions"),
    "fills": ("_generate_fill_reports", "/account/activities/FILL"),
}
QUERY = NS(open_only=True, instrument_id=None, start=None, end=None)


async def _connected(monkeypatch, *, run=None):
    """A node connected over a real SDK stream object with no socket; returns (node, stream)."""
    node, stream = client(), TradingStream("fixture-key", "fixture-secret", paper=True)
    gateway = node.gateway
    for name in ("_connect", "_auth", "_subscribe_trade_updates", "close"):
        setattr(stream, name, AsyncMock())
    if run is not None:
        stream._run_forever = run
    monkeypatch.setenv("FIXTURE_KEY", "dummy-key")
    monkeypatch.setenv("FIXTURE_SECRET", "dummy-secret")
    fixture_controls = controls()
    monkeypatch.setattr(client_module().Controls, "from_rows", lambda rows: fixture_controls)
    monkeypatch.setattr(client_module(), "TradingClient", Mock())
    monkeypatch.setattr(client_module(), "TradingStream", lambda *a, **k: stream)
    node.config = NS(pacing_rows=[], api_key_env="FIXTURE_KEY", api_secret_env="FIXTURE_SECRET")  # noqa: S106 - fixture environment name
    node._do_query_account = AsyncMock()
    node.commission_for_fill = lambda data: Money.from_str("0 USD")  # Fixture evidence only.
    if run is None:
        node.create_task = lambda coroutine, name: coroutine.close()  # No SDK thread here.
    await node._connect()
    node.gateway = gateway  # The fake venue answers the reads.
    return node, stream


@pytest.mark.parametrize("read", READS)
def test_startup_reads_wait_for_the_trade_updates_subscription(monkeypatch, read):
    async def run():
        node, stream = await _connected(monkeypatch)
        method, path = READS[read]
        task = asyncio.create_task(getattr(node, method)(QUERY))
        await asyncio.sleep(0.05)
        assert not task.done()  # Not before the venue confirms the subscription.
        assert not [r for r in node.gateway.sdk.requests if r[1] == path]
        await stream._start_ws()
        await stream._dispatch(LISTENING)
        await asyncio.wait_for(task, 1)
        assert node.stream_live

    asyncio.run(run())


def test_a_dropped_trade_stream_is_visible_at_once(monkeypatch):
    async def run():
        node, stream = await _connected(monkeypatch)
        await stream._start_ws()
        await stream._dispatch(LISTENING)
        await asyncio.sleep(0)
        node.coverage_complete = True  # As after a complete reconciliation.
        await stream._start_ws()  # The SDK reconnects: the socket it had was lost.
        await asyncio.sleep(0)
        assert not node.stream_live and not node.coverage_complete
        assert node.stream_restarts == 1
        await stream._dispatch(LISTENING)
        await asyncio.sleep(0)
        assert node.stream_live

    asyncio.run(run())


def test_a_trade_stream_that_ends_unexpectedly_is_visible(monkeypatch):
    async def run():
        node, _ = await _connected(monkeypatch, run=AsyncMock())  # Returns at once.
        node.coverage_complete = True
        await asyncio.wait_for(asyncio.shield(node._stream_task), 1)
        await asyncio.sleep(0)
        assert not node.stream_live and not node.coverage_complete
        assert node.stream_ended

    asyncio.run(run())
