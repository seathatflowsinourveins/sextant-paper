"""Bulk coverage against a fake that honours GET /orders parameters.

alpaca-py cc4cb3b7 requests.py:202-213 (status, limit max 500, after, until,
symbols); NT d53849b adapter guide 384-385: a propagated failure fails startup.
"""

import asyncio
from types import SimpleNamespace as NS

import pytest
from nautilus_trader.model import ClientOrderId

from .test_alpaca_client import client, client_module
from .test_alpaca_execution import boundary, order

OLD, WINDOW, NEW = "2026-11-09T15:00:00Z", 1_794_268_800_000_000_000, "2026-11-10T15:00:00Z"
# WINDOW is 2026-11-10T00:00:00Z: OLD precedes it, NEW follows it.


def _house(venue, name, *, submitted=NEW, status="new"):
    row = venue.post(
        "/orders",
        boundary().submit_payload(order(client_order_id=ClientOrderId(name)), {}, "fixture-"),
    )
    venue.rows[row["id"]].update(submitted_at=submitted, status=status)
    return venue.rows[row["id"]]


def _scan(open_only, start=None):
    return NS(open_only=open_only, instrument_id=None, start=start, end=None)


@pytest.mark.parametrize("count", [499, 500])
def test_listing_at_the_venue_page_cap_pages_on_instead_of_refusing(count):
    # E5 u9: a full page is followed by the next page (test_alpaca_paging.py).
    async def run():
        node = client()
        venue = node.gateway.sdk
        for index in range(count):
            stamp = f"2026-11-10T14:{index // 60 % 60:02d}:{index % 60:02d}Z"
            _house(venue, f"fixture-entry-{index}", submitted=stamp)
        assert len(await node._generate_order_status_reports(_scan(True))) == count
        assert node.coverage_complete

    asyncio.run(run())


def test_windowed_listing_merges_older_still_working_orders():
    async def run():
        node = client()
        venue = node.gateway.sdk
        older = _house(venue, "fixture-older", submitted=OLD)
        _house(venue, "fixture-closed", submitted=OLD, status="canceled")
        recent = _house(venue, "fixture-recent")
        reports = await node._generate_order_status_reports(_scan(False, start=WINDOW))
        assert {str(r.venue_order_id) for r in reports} == {older["id"], recent["id"]}
        windowed = venue.listing({"status": "all", "after": "2026-11-10T00:00:00Z", "limit": 500})
        assert older["id"] not in {r["id"] for r in windowed}, "fake honours after"

    asyncio.run(run())


def test_nonempty_fill_history_is_incomplete_not_empty():
    async def run():
        node = client()
        command = NS(start=None, end=None, instrument_id=None)
        assert await node._generate_fill_reports(command) == []
        activity = {"activity_type": "FILL", "id": "fixture-activity", "qty": "1"}
        node.gateway.sdk.activities = [activity]
        with pytest.raises(client_module().IncompleteCoverage) as failure:
            await node._generate_fill_reports(command)
        assert failure.value.failures[0][0] == activity
        assert not failure.value.reports and not node.coverage_complete

    asyncio.run(run())
