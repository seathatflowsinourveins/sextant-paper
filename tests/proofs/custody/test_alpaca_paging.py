"""The bulk order listing pages past the venue's 500-row cap and fails closed (E5 u9).

alpaca-py cc4cb3b7 requests.py:202-213 (GetOrdersRequest: limit max 500, until,
direction desc by default; no page token). The engine's lookback stays unbounded
(NT d53849b node_config.rs:442-443), so an owner holding whose fills predate any
window is still explained by its order. A failed page or a listing that cannot be
proven complete is incomplete coverage, never a truncated success.
"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS
from uuid import UUID

import pytest

from .conftest import controls
from .test_alpaca_client import client, client_module
from .test_alpaca_execution import boundary, order

ALL = NS(open_only=False, instrument_id=None, start=None, end=None)


def _stamp(second):
    return f"2026-10-01T{second // 3600:02d}:{second // 60 % 60:02d}:{second % 60:02d}Z"


def seed(venue, count, *, stamp=_stamp):
    """Insert ``count`` house rows directly, oldest first (``stamp(i)`` per row)."""
    template = venue.post("/orders", boundary().submit_payload(order(), {}, "fixture-"))
    venue.rows.clear()
    venue.requests.clear()
    for index in range(count):
        ident = str(UUID(int=10_000 + index))
        venue.rows[ident] = deepcopy(template) | dict(
            id=ident, client_order_id=f"fixture-entry-{index}", submitted_at=stamp(index)
        )


def listings(venue):
    return [
        params for method, path, params in venue.requests if (method, path) == ("GET", "/orders")
    ]


def test_listing_pages_past_the_vendor_cap_and_reads_every_order_once():
    async def run():
        node = client()
        seed(node.gateway.sdk, 1200)
        reports = await node._generate_order_status_reports(ALL)
        assert len({str(r.venue_order_id) for r in reports}) == len(reports) == 1200
        pages = listings(node.gateway.sdk)
        assert len(pages) == 3 and all(
            p["limit"] == 500 and p["direction"] == "desc" for p in pages
        )
        assert "until" not in pages[0] and pages[1]["until"] == _stamp(700)
        assert node.coverage_complete

    asyncio.run(run())


def test_rows_sharing_the_page_boundary_instant_are_read_once():
    async def run():
        node = client()
        # Rows 498-502 (counting from the oldest of 1000) share one submission instant.
        seed(node.gateway.sdk, 1000, stamp=lambda i: _stamp(500 if 498 <= i <= 502 else i))
        reports = await node._generate_order_status_reports(ALL)
        assert len({str(r.venue_order_id) for r in reports}) == len(reports) == 1000

    asyncio.run(run())


def test_a_failed_page_is_incomplete_coverage_not_a_truncated_listing():
    async def run():
        node = client()
        venue = node.gateway.sdk
        seed(venue, 700)
        get = venue.get

        def second_page_fails(path, params):
            if path == "/orders" and "until" in params:
                raise ConnectionError("fixture page transport failure")
            return get(path, params)

        venue.get = second_page_fails
        with pytest.raises(client_module().IncompleteCoverage) as failure:
            await node._generate_order_status_reports(ALL)
        assert isinstance(failure.value.failures[-1][1], ConnectionError)
        assert not failure.value.reports and not node.coverage_complete

    asyncio.run(run())


def test_a_listing_that_cannot_be_proven_contiguous_fails_closed():
    async def run():
        node = client()
        venue = node.gateway.sdk
        seed(venue, 700)
        venue.until_exclusive = True  # Rows at the boundary instant would then be skipped.
        with pytest.raises(client_module().IncompleteCoverage) as failure:
            await node._generate_order_status_reports(ALL)
        assert "contiguous" in str(failure.value.failures[-1][1])

    asyncio.run(run())


def test_a_full_page_at_one_instant_makes_no_progress_and_fails_closed():
    async def run():
        node = client()
        seed(node.gateway.sdk, 600, stamp=lambda i: _stamp(1))
        with pytest.raises(client_module().IncompleteCoverage) as failure:
            await node._generate_order_status_reports(ALL)
        assert "progress" in str(failure.value.failures[-1][1])

    asyncio.run(run())


def test_listing_is_bounded_by_the_registered_request_budget():
    async def run():
        node = client()
        node.gateway.pacer.controls = controls(request_budget=10)
        seed(node.gateway.sdk, 5200)
        with pytest.raises(client_module().IncompleteCoverage) as failure:
            await node._generate_order_status_reports(ALL)
        assert "request budget" in str(failure.value.failures[-1][1])
        assert len(listings(node.gateway.sdk)) == 10

    asyncio.run(run())


def test_engine_lookback_stays_unbounded():
    with pytest.raises(ValueError, match="unbounded"):
        client_module().execution_engine_config(
            universe=["TEST.ALPACA"], reconciliation_lookback_mins=60
        )
    config = client_module().execution_engine_config(universe=["TEST.ALPACA"])
    assert config.reconciliation_lookback_mins is None
