"""E5 u14: what the trade stream missed while it was lost reaches the engine's reconciliation.

alpaca-py reconnects on its own and reads nothing back (alpaca/trading/stream.py:204-241). The
official Derive client hands the engine one mass status after each reconnect (NautilusTrader
d53849b2 crates/adapters/derive/src/execution.rs:2563-2575), and the engine reconciles a
runtime mass status itself (crates/execution/src/engine/mod.rs:1119-1121), as at startup.
"""

import asyncio
from types import SimpleNamespace as NS

from nautilus_trader.model import ExecutionMassStatus, OrderStatus

from .test_alpaca_client import client
from .test_alpaca_execution import order
from .test_alpaca_stream_state import LISTENING, _connected


async def _subscribed(monkeypatch):
    node, stream = await _connected(monkeypatch)
    node.create_task = lambda coroutine, name: asyncio.create_task(coroutine)  # After connect.
    await stream._start_ws()
    await stream._dispatch(LISTENING)
    await asyncio.sleep(0.01)
    return node, stream


async def _lost_and_resubscribed(node, stream):
    await stream._start_ws()  # The SDK reconnects: the old socket was lost.
    await stream._dispatch(LISTENING)
    for _ in range(20):
        await asyncio.sleep(0.01)


def _mass(node):
    return [
        c.args[0]
        for c in node._handle_report.call_args_list
        if isinstance(c.args[0], ExecutionMassStatus)
    ]


def test_a_resubscription_after_a_loss_hands_the_engine_one_mass_status(monkeypatch):
    async def run():
        node, stream = await _subscribed(monkeypatch)
        await node._submit_order(NS(order=order(), params={}))
        row = next(iter(node.gateway.sdk.rows.values()))
        assert not _mass(node)  # The first subscription: startup reconciliation reads.
        row["status"] = "canceled"  # Ended at the venue while the stream was lost.
        await _lost_and_resubscribed(node, stream)
        (mass,) = _mass(node)
        (report,) = mass.order_reports.values()
        assert str(report.venue_order_id) == row["id"]
        assert report.order_status == OrderStatus.CANCELED

    asyncio.run(run())


def test_a_failed_recovery_read_is_kept_and_coverage_stays_revoked(monkeypatch):
    async def run():
        node, stream = await _subscribed(monkeypatch)

        def unreadable(path, params):
            raise ConnectionError("fixture venue unreadable")

        node.gateway.sdk.get = unreadable
        await _lost_and_resubscribed(node, stream)
        assert not _mass(node) and not node.coverage_complete
        assert any("unreadable" in str(error) for _, error in node.failures)

    asyncio.run(run())


# Gate e5u14-b435d5b (Codex max, review-gpt.md sha256 b83c3b19...): its witnesses, carried in.


def test_the_mass_status_is_stamped_before_the_first_read(monkeypatch):  # P1, unit level
    async def run():
        node = client()
        node.clock.set_time(100)
        positions = node._generate_position_status_reports

        async def fill_during_the_reads(command):
            node.clock.set_time(200)  # A fill reaches the engine during the reads.
            return await positions(command)

        monkeypatch.setattr(node, "_generate_position_status_reports", fill_during_the_reads)
        await node._recover()
        (mass,) = _mass(node)
        assert mass.ts_init == 100  # Taken before the first await, as the composer does.

    asyncio.run(run())


def test_a_second_loss_during_recovery_keeps_coverage_revoked(monkeypatch):  # P2
    async def run():
        node = client()
        node._listening = asyncio.get_running_loop().create_future()
        node._listening.set_result(None)
        node.stream_live = True
        positions = node._generate_position_status_reports

        async def lost_while_reading(command):
            node._on_stream("socket")
            return await positions(command)

        monkeypatch.setattr(node, "_generate_position_status_reports", lost_while_reading)
        await node._recover()
        assert not node.coverage_complete and not _mass(node)

    asyncio.run(run())


def test_a_failed_position_row_is_retained():  # P2
    async def run():
        node = client()
        node.gateway.sdk.positions = [{"symbol": "TEST", "qty": "invalid"}]
        await node._recover()
        assert [row for row, _ in node.failures] == [{"symbol": "TEST", "qty": "invalid"}]
        assert not node.coverage_complete and not _mass(node)

    asyncio.run(run())
