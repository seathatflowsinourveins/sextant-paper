"""Combine P's once-only coverage contract and r5's paused-release witness.

P: tests/proofs/custody/test_alpaca_once.py:1-8,88-102;
P: tests/proofs/custody/test_alpaca_r5_attacks.py:82-125.
No SDK, key lookup or socket is used; the original native client uses FakeVenue.
"""

import asyncio
from types import SimpleNamespace as NS

import pytest
from nautilus_trader.model import Money

from .test_alpaca_client import client, client_module
from .test_alpaca_execution import order
from .test_alpaca_exits import _cmd
from .test_alpaca_once import BULK, ROOT, _fill, _fills


def test_position_coverage_waits_for_inflight_held_execution():
    async def run():
        node = client()
        release = asyncio.Event()
        confirmation = None
        try:
            await node._submit_order(NS(order=order(), params={}))
            await node._generate_order_status_reports(BULK)
            assert node._orders_complete
            node.commission_for_fill = lambda data: Money.from_str("0 USD")
            await node._modify_order(_cmd("modify"))
            update = _fill(node.gateway.sdk.pending[1])
            with pytest.raises(client_module().IncompleteCoverage):
                await node._on_trade_update(update)
            assert not node.coverage_complete and node._held and not _fills(node)
            successor = node.gateway.sdk.confirm()
            node.gateway.sdk.rows[successor["id"]].update(update["data"]["order"])
            original_call = node._call
            entered = asyncio.Event()
            parent_reads = []

            async def pause_release(kind, method, path, data=None, **kwargs):
                if (
                    asyncio.current_task() is confirmation
                    and node._releasing
                    and path == f"/orders/{successor['replaces']}"
                ):
                    parent_reads.append(path)
                    entered.set()
                    await release.wait()
                return await original_call(kind, method, path, data, **kwargs)

            node._call = pause_release
            confirmation = asyncio.create_task(
                node._generate_order_status_report(NS(client_order_id=ROOT, venue_order_id=None))
            )
            await asyncio.wait_for(entered.wait(), 1)
            assert node._releasing and not node._held and not _fills(node)
            await node._generate_position_status_reports(NS())
            pending = bool(node._releasing)
            forwarded = bool(_fills(node))
            coverage = bool(node.coverage_complete)
            flat_authority = bool(
                node._provides_bulk_position_coverage(node._instrument_id(successor))
            )
            assert not coverage and not flat_authority, (
                f"inflight_release={pending}; fill_forwarded={forwarded}; "
                f"coverage_complete={coverage}; bulk_position_authority={flat_authority}"
            )
        finally:
            release.set()
            if confirmation is not None:
                await asyncio.gather(confirmation, return_exceptions=True)
            await node._disconnect()

    asyncio.run(run())


def test_fill_arriving_during_release_is_forwarded_once_before_coverage_returns():
    async def run():
        node = client()
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        confirmation = None
        try:
            await node._submit_order(NS(order=order(), params={}))
            await node._generate_order_status_reports(BULK)
            node.commission_for_fill = lambda data: Money.from_str("0 USD")
            await node._modify_order(_cmd("modify"))
            updates = [
                _fill(node.gateway.sdk.pending[1], execution=execution)
                for execution in ("fixture-held-first", "fixture-held-last")
            ]
            for update in updates:
                with pytest.raises(client_module().IncompleteCoverage):
                    await node._on_trade_update(update)
            assert not _fills(node)
            successor = node.gateway.sdk.confirm()
            node.gateway.sdk.rows[successor["id"]].update(updates[-1]["data"]["order"])
            original_call = node._call

            async def pause_each_fill(kind, method, path, data=None, **kwargs):
                if (
                    asyncio.current_task() is confirmation
                    and node._releasing
                    and path == f"/orders/{successor['replaces']}"
                ):
                    # One parent read per replay; a re-entrant release must return immediately.
                    index = sum(str(fill.trade_id) != "fixture-arriving" for fill in _fills(node))
                    entered[index].set()
                    await release[index].wait()
                return await original_call(kind, method, path, data, **kwargs)

            node._call = pause_each_fill
            confirmation = asyncio.create_task(
                node._generate_order_status_report(NS(client_order_id=ROOT, venue_order_id=None))
            )
            await asyncio.wait_for(entered[0].wait(), 1)
            arriving = _fill(successor, execution="fixture-arriving")
            await node._on_trade_update(arriving)
            await node._on_trade_update(arriving)
            assert [str(fill.trade_id) for fill in _fills(node)] == ["fixture-arriving"]

            for index in range(2):
                await asyncio.wait_for(entered[index].wait(), 1)
                assert node._releasing
                # FakeVenue's clock is fixed; replenish pacing between observation rounds.
                node.gateway.pacer.bucket.tokens = node.gateway.pacer.bucket.burst
                # An order read must not restore authority behind the pending replay.
                await node._generate_order_status_reports(BULK)
                assert node._orders_complete
                assert not node.coverage_complete
                assert not node._provides_bulk_position_coverage(node._instrument_id(successor))
                await node._generate_position_status_reports(NS())
                assert not node.coverage_complete
                assert not node._provides_bulk_position_coverage(node._instrument_id(successor))
                assert [str(fill.trade_id) for fill in _fills(node)] == (
                    ["fixture-arriving"]
                    if index == 0
                    else ["fixture-arriving", "fixture-held-first"]
                )
                release[index].set()

            await asyncio.wait_for(confirmation, 1)
            node.gateway.pacer.bucket.tokens = node.gateway.pacer.bucket.burst
            for update in (*updates, arriving):
                await node._on_trade_update(update)
            assert [str(fill.trade_id) for fill in _fills(node)] == [
                "fixture-arriving",
                "fixture-held-first",
                "fixture-held-last",
            ]
            assert not node._held and not node._releasing
            await node._generate_position_status_reports(NS())
            assert node.coverage_complete
            assert node._provides_bulk_position_coverage(node._instrument_id(successor))
        finally:
            for event in release:
                event.set()
            if confirmation is not None:
                await asyncio.gather(confirmation, return_exceptions=True)
            await node._disconnect()

    asyncio.run(run())
