"""Owner rows the engine would skip must not block startup coverage (E5 fix round).

NT d53849b manager.rs:4665-4668 (unclaimed external order filtered) and
1056-1057 (order report without a cached instrument skipped); alpaca-py
cc4cb3b7 models.py:204,241 (nested legs) and enums.py:103-111 (order classes).
Design: handoffs/tasks/e5-execution-client-gpt/DESIGN-owner-activity.md.
"""

import asyncio
from types import SimpleNamespace as NS

import pytest
from nautilus_trader.model import ClientOrderId, InstrumentId

from .test_alpaca_client import client, client_module
from .test_alpaca_execution import boundary, order

TEST, OTHER = InstrumentId.from_str("TEST.ALPACA"), InstrumentId.from_str("OTHER.ALPACA")


def _owner(venue, name, **changes):
    payload = boundary().submit_payload(order(), {}, "fixture-")
    row = venue.post("/orders", payload | {"client_order_id": name})
    venue.rows[row["id"]].update(changes)
    return venue.rows[row["id"]]


def _replaced_pair(venue):
    old = _owner(venue, "owner-entry", status="replaced")
    new = _owner(venue, "owner-entry-2", replaces=old["id"])
    old["replaced_by"] = new["id"]
    return old, new


def _bracket(venue):
    parent = _owner(venue, "owner-bracket", order_class="bracket")
    leg = dict(parent, id="00000000-0000-0000-0000-0000000000b1", client_order_id="owner-leg")
    parent["legs"] = [leg | {"side": "sell", "status": "held"}]
    return parent


def _other_symbol(node, venue):
    stock = node.cache.instrument.return_value
    node.cache.instrument.side_effect = lambda iid: stock if iid == TEST else None
    return _owner(venue, "owner-other", symbol="OTHER", status="canceled")


async def _scan(node):
    return await node._generate_order_status_reports(
        NS(open_only=False, instrument_id=None, start=None, end=None)
    )


@pytest.mark.parametrize(
    "kind,instrument",
    [("replaced", TEST), ("unloaded", OTHER)],
)
def test_owner_row_outside_report_shape_is_withheld_not_a_startup_failure(kind, instrument):
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        house = next(iter(venue.rows.values()))
        if kind == "replaced":
            withheld, successor = _replaced_pair(venue)
            expected = {house["id"], successor["id"]}
        else:
            withheld = _other_symbol(node, venue)
            expected = {house["id"]}
        before = len(venue.requests)
        reports = await _scan(node)
        assert {str(r.venue_order_id) for r in reports} == expected
        assert node.coverage_complete
        assert [row["id"] for row, _ in node.withheld] == [withheld["id"]]
        # The engine never infers flat for an instrument with a withheld row.
        assert node._provides_bulk_position_coverage(instrument) is False
        assert all(r[0] == "GET" for r in venue.requests[before:]), "no owner mutation"

    asyncio.run(run())


def test_unloaded_owner_row_keeps_coverage_for_other_instruments():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        _other_symbol(node, node.gateway.sdk)
        await _scan(node)
        assert node._provides_bulk_position_coverage(TEST) is True
        assert node._provides_bulk_position_coverage(OTHER) is False

    asyncio.run(run())


def test_house_and_unclassified_rows_still_fail_closed():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        house = next(iter(venue.rows.values()))
        owner_successor = _owner(venue, "owner-took-over", replaces=house["id"])
        house.update(status="replaced", replaced_by=owner_successor["id"])
        odd = _owner(venue, "owner-odd", status="future_state")
        with pytest.raises(client_module().IncompleteCoverage) as failure:
            await _scan(node)
        assert {row["id"] for row, _ in failure.value.failures} == {house["id"], odd["id"]}
        assert not node.coverage_complete

    asyncio.run(run())


def test_targeted_query_of_an_unrepresentable_owner_order_never_reads_absent():
    async def run():
        node = client()
        _bracket(node.gateway.sdk)
        with pytest.raises(ValueError, match="simple"):
            await node._generate_order_status_report(
                NS(client_order_id=ClientOrderId("owner-bracket"), venue_order_id=None)
            )

    asyncio.run(run())


def test_owner_bracket_is_a_named_refusal_never_a_report():
    # Lead ruling 2026-11-11: an owner bracket is outside the managed-paper contract.
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        bracket = _bracket(node.gateway.sdk)
        with pytest.raises(client_module().IncompleteCoverage) as failure:
            await _scan(node)
        (row, error), *_ = failure.value.failures
        assert row["id"] == bracket["id"] and "owner-bracket" in str(error)
        assert "contract" in str(error)
        assert all(str(r.client_order_id) != "owner-bracket" for r in failure.value.reports)
        assert (row, error) in node.failures, "the refusal is retained on the client"

    asyncio.run(run())


def test_replaced_owner_order_with_fills_fails_closed():
    # A claim would need its fills; withholding them could misstate the position.
    async def run():
        node = client()
        old, _ = _replaced_pair(node.gateway.sdk)
        old.update(filled_qty="2", filled_avg_price="0.1234")
        with pytest.raises(client_module().IncompleteCoverage) as failure:
            await _scan(node)
        assert [row["id"] for row, _ in failure.value.failures] == [old["id"]]

    asyncio.run(run())


def test_position_in_an_unloaded_instrument_is_withheld_without_flat_coverage():
    # The engine skips it outside reconciliation_instrument_ids (manager.rs:1314-1319).
    async def run():
        from uuid import UUID

        node = client()
        venue = node.gateway.sdk
        stock = node.cache.instrument.return_value
        node.cache.instrument.side_effect = lambda iid: stock if iid == TEST else None
        venue.positions = [
            {
                "asset_id": str(UUID(int=101)),
                "symbol": symbol,
                "exchange": "NASDAQ",
                "asset_class": "us_equity",
                "avg_entry_price": "0.1234",
                "qty": "5",
                "side": "long",
                "cost_basis": "0.617",
            }
            for symbol in ("TEST", "OTHER")
        ]
        await _scan(node)
        reports = await node._generate_position_status_reports(NS())
        assert [str(r.instrument_id) for r in reports] == ["TEST.ALPACA"]
        assert [row["symbol"] for row, _ in node.withheld_positions] == ["OTHER"]
        assert node._provides_bulk_position_coverage(TEST) is True
        assert node._provides_bulk_position_coverage(OTHER) is False

    asyncio.run(run())


def test_malformed_position_row_keeps_sibling_reports_and_its_payload():
    # Review e5fix-2a7d367 P2: the unloaded-instrument guard ran before the row boundary.
    async def run():
        from uuid import UUID

        node = client()
        good = {
            "asset_id": str(UUID(int=101)),
            "symbol": "TEST",
            "exchange": "NASDAQ",
            "asset_class": "us_equity",
            "avg_entry_price": "0.1234",
            "qty": "5",
            "side": "long",
            "cost_basis": "0.617",
        }
        malformed = {"qty": "5"}
        node.gateway.sdk.positions = [good, malformed]
        with pytest.raises(client_module().IncompleteCoverage) as failure:
            await node._generate_position_status_reports(NS())
        assert len(failure.value.reports) == 1
        assert failure.value.failures[0][0] == malformed

    asyncio.run(run())
