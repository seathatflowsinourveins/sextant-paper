"""Venue-named succession, LEAD E5 addendum; Alpaca replace reference read 2026-11-10."""

import asyncio
from types import SimpleNamespace as NS

import pytest
from nautilus_trader.model import ClientOrderId, Price

from .test_alpaca_client import client
from .test_alpaca_execution import boundary, command, order


def test_only_venue_named_successor_wins_among_identical_house_order_shapes():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        parent = next(iter(venue.rows.values()))
        change = command(
            client_order_id=ClientOrderId("fixture-entry"),
            venue_order_id=None,
            price=Price.from_str("0.1235"),
        )
        await node._modify_order(change)
        neighbor = venue.post(
            "/orders",
            boundary().submit_payload(
                order(client_order_id=ClientOrderId("fixture-independent")), {}, "fixture-"
            ),
        )
        successor = venue.confirm()
        await node._on_trade_update(
            {
                "stream": "trade_updates",
                "data": {"event": "replaced", "order": parent, "timestamp": parent["updated_at"]},
            }
        )
        assert node._active["fixture-entry"] == parent["replaced_by"] == successor["id"]
        assert successor["id"] != neighbor["id"]
        assert venue.rows[neighbor["id"]]["client_order_id"] == "fixture-independent"
        update = node.generate_order_updated.call_args.args
        assert str(update[1]) == successor["id"]
        assert str(update[0].client_order_id) == "fixture-entry"
        assert successor["symbol"] == neighbor["symbol"]
        assert successor["side"] == neighbor["side"]
        assert successor["qty"] == neighbor["qty"]

    asyncio.run(run())


def test_independent_order_beside_pending_replace_is_never_mutated_or_adopted():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        parent = next(iter(venue.rows.values()))
        change = command(
            client_order_id=ClientOrderId("fixture-entry"),
            venue_order_id=None,
            price=Price.from_str("0.1235"),
        )
        await node._modify_order(change)
        neighbor = venue.post(
            "/orders",
            boundary().submit_payload(
                order(client_order_id=ClientOrderId("fixture-independent")), {}, "fixture-"
            ),
        )
        await node._on_trade_update(
            {
                "stream": "trade_updates",
                "data": {"event": "new", "order": neighbor, "timestamp": neighbor["updated_at"]},
            }
        )
        assert node._active["fixture-entry"] == parent["id"]
        assert node._roots[neighbor["id"]] == "fixture-independent"
        assert len(node._proposals) == 1
        node.generate_order_updated.assert_not_called()
        start = len(venue.requests)
        await node._modify_order(change)
        await node._cancel_order(change)
        writes = [r for r in venue.requests[start:] if r[0] in ("PATCH", "DELETE")]
        assert all(not r[1].endswith(neighbor["id"]) for r in writes)
        assert venue.rows[neighbor["id"]] == neighbor
        successor = venue.confirm()
        await node._on_trade_update(
            {
                "stream": "trade_updates",
                "data": {"event": "new", "order": successor, "timestamp": successor["updated_at"]},
            }
        )
        assert node._roots[neighbor["id"]] == "fixture-independent"
        assert node._active["fixture-entry"] == successor["id"]

    asyncio.run(run())


def _stream(node, event, row):
    return node._on_trade_update(
        {
            "stream": "trade_updates",
            "data": {"event": event, "order": row, "timestamp": row["updated_at"]},
        }
    )


async def _two_hop_chain(node):
    """O0 -> O1 -> O2: the routine 'tighten twice' flow, both confirmed and bound."""
    await node._submit_order(NS(order=order(), params={}))
    venue = node.gateway.sdk
    chain = [next(iter(venue.rows.values()))]
    for price in ("0.1235", "0.1236"):
        change = command(
            client_order_id=ClientOrderId("fixture-entry"),
            venue_order_id=None,
            price=Price.from_str(price),
        )
        await node._modify_order(change)
        chain.append(venue.confirm())
        await _stream(node, "new", chain[-1])
    assert node._active["fixture-entry"] == chain[2]["id"]
    return [venue.rows[row["id"]] for row in chain], chain[1]


def test_full_report_with_replaced_middle_successor_is_complete_and_silent():
    async def run():
        node = client()
        (o0, o1, o2), _ = await _two_hop_chain(node)
        assert o0["status"] == o1["status"] == "replaced"
        node.generate_order_updated.reset_mock()
        reports = await node._generate_order_status_reports(
            NS(open_only=False, instrument_id=None, start=None, end=None)
        )
        assert node.coverage_complete
        assert [str(r.venue_order_id) for r in reports] == [o2["id"]]
        node.generate_order_updated.assert_not_called()
        assert node._active["fixture-entry"] == o2["id"]

    asyncio.run(run())


@pytest.mark.parametrize("event", ["new", "accepted", "replaced"])
def test_late_middle_successor_after_newer_binding_emits_nothing(event):
    async def run():
        node = client()
        (_, o1, o2), o1_snapshot = await _two_hop_chain(node)
        late = o1 if event == "replaced" else dict(o1_snapshot, status=event)
        node.generate_order_updated.reset_mock()
        node._handle_report.reset_mock()
        await _stream(node, event, late)
        node.generate_order_updated.assert_not_called()
        node._handle_report.assert_not_called()
        assert node._active["fixture-entry"] == o2["id"]

    asyncio.run(run())
