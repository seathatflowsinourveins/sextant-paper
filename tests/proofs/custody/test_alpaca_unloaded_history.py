"""LEAD-1618: rehearse run 2's history shape through the real order node and gate.

BUILD ON NT d53849b2 spot/execution.rs:3542-3545 (uncached report skip), with
terminal-only admission and the existing gate's withheld replacement lineage.
All values are synthetic; only aggregate counts belong in startup diagnostics.
"""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest
from nautilus_trader.model import InstrumentId

from sextant.execution.pacer import AccountPacer

from . import test_b_order_node
from .alpaca_venue import STAMP
from .conftest import controls
from .test_alpaca_client import client, client_module
from .test_alpaca_node_owner import _owner
from .test_alpaca_owner_rows import _bracket
from .test_b_order_node import AccountVenue, expected, start  # noqa: F401

SCOPE = NS(open_only=False, instrument_id=None, start=None, end=None)
REASON = "report instrument is unavailable for non-terminal order"


@pytest.fixture(autouse=True)
def history_read_burst(monkeypatch):
    # Fixture-only: avoid waiting a real minute for historical reads in this offline proof.
    monkeypatch.setattr(test_b_order_node, "controls", lambda: controls(burst_fraction="0.5"))


def _history():
    venue = AccountVenue()
    for index in range(261):
        row = _owner(
            venue,
            f"O-history-{index}",
            symbol="UNLOADED",
            side="buy" if index % 2 == 0 else "sell",
            qty="5",
            status="filled" if index < 260 else "canceled",
            filled_qty="5" if index < 260 else "0",
            filled_avg_price="0.1234" if index < 260 else None,
        )
        if index < 260:
            for cumulative in range(1, 6):
                venue.activities.append(
                    dict(
                        id=f"fixture-fill-{index}-{cumulative}",
                        order_id=row["id"],
                        activity_type="FILL",
                        transaction_time=STAMP,
                        type="fill",
                        symbol="UNLOADED",
                        price="0.1234",
                        qty="1",
                        side=row["side"],
                        leaves_qty=str(5 - cumulative),
                        cum_qty=str(cumulative),
                        order_status="filled" if cumulative == 5 else "partially_filled",
                    )
                )
    # Each pair walks child once and parent twice: eleven pairs give 33 single reads.
    for index in range(11):
        old = _owner(venue, f"O-pair-{index}", symbol="ZA", status="replaced")
        new = _owner(
            venue, f"O-pair-{index}-r1", symbol="ZA", status="canceled", replaces=old["id"]
        )
        old["replaced_by"] = new["id"]
    for index in range(154):
        _owner(venue, f"O-ended-{index}", symbol="ZA", status="canceled")
    return venue


def _no_mutations(venue, seeded, before):
    assert venue.rows == before
    assert all(request[0] == "GET" for request in venue.requests[seeded:])


def test_run2_history_reaches_the_gate_flat_and_counts_skips_in_receipt(tmp_path, capfd):
    venue = _history()
    assert len(venue.rows) == 437 and len(venue.activities) == 1300 and not venue.positions
    before, seeded = deepcopy(venue.rows), len(venue.requests)
    body, code, _ = start(tmp_path, venue, count=100)
    assert (code, body["phase"]) == (0, "stopped"), body.get("reason")
    assert body["gate"] == dict(
        open_positions=0, open_house_orders=0, withheld_holdings=0, unresolved_replacements=0
    )
    trace = body["reconciliation"]
    assert trace["orders"]["skipped_unloaded"] == 261
    assert trace["fills"]["skipped_unloaded"] == 1300
    assert trace["positions"]["skipped_unloaded"] == 0
    assert trace["orders"]["requests"] == {
        "/orders": {"requests": 1, "rows": 437},
        "/orders/{id}": {"requests": 33, "rows": 33},
    }
    assert trace["fills"]["requests"]["/account/activities/FILL"] == {
        "requests": 14,
        "rows": 1300,
    }
    text = json.dumps(trace)
    assert all(row["id"] not in text for row in venue.rows.values())
    assert "UNLOADED" not in text and "0.1234" not in text
    assert "skipped_unloaded=261" in capfd.readouterr().out
    _no_mutations(venue, seeded, before)


def test_run2_order_generator_preserves_loaded_reports_and_skips_only_terminal_history():
    async def run():
        node = client()
        stock = test_b_order_node.equity(InstrumentId.from_str("ZA.ALPACA"))
        node.cache.instrument.side_effect = lambda iid: stock if str(iid) == "ZA.ALPACA" else None
        node.cache.order.return_value = None
        node.house_prefix = "O-"
        node.gateway.sdk = _history()
        node.gateway.pacer = AccountPacer(controls(burst_fraction="0.5"), lambda: 0)
        reports = await node._generate_order_status_reports(SCOPE)
        assert len(reports) == 165
        assert node.report_trace["orders"]["skipped_unloaded"] == 261
        assert node.report_trace["orders"]["requests"]["/orders/{id}"]["requests"] == 33

    asyncio.run(run())


@pytest.mark.parametrize("prefix", ["O-", "BTEST-", "owner-"])
def test_an_unloaded_working_order_refuses_start_by_name(tmp_path, capfd, prefix):
    venue = _history()
    _owner(venue, prefix + "working", symbol="UNSEEN", status="new")
    before, seeded = deepcopy(venue.rows), len(venue.requests)
    body, code, stand = start(tmp_path, venue)
    assert code == 2 and body["phase"] in ("failed", "aborted_at_start"), body
    assert body["gate"] == {} and stand.subscribed == 0
    trace = body["reconciliation"]
    assert trace["orders"]["reason"] == REASON
    assert trace["orders"]["skipped_unloaded"] == 261
    (line,) = [
        line
        for line in capfd.readouterr().err.splitlines()
        if "startup reconciliation failed" in line
    ]
    assert "skipped_unloaded=261" in line and REASON in line
    assert "UNSEEN" not in line + json.dumps(trace)
    _no_mutations(venue, seeded, before)


@pytest.mark.parametrize(
    "status",
    [
        "new",
        "partially_filled",
        "pending_cancel",
        "pending_replace",
        "done_for_day",
        "stopped",
        "suspended",
        "future_status",
    ],
)
def test_no_working_or_unknown_status_can_be_skipped(status):
    async def run():
        node = client()
        node.cache.instrument.return_value = None
        _owner(node.gateway.sdk, "owner-working", symbol="UNSEEN", status=status)
        with pytest.raises(client_module().IncompleteCoverage):
            await node._generate_order_status_reports(SCOPE)
        assert node.report_trace["orders"]["reason"] == REASON
        assert not node.coverage_complete

    asyncio.run(run())


@pytest.mark.parametrize("status", ["filled", "canceled", "expired", "rejected", "replaced"])
def test_terminal_rows_are_counted_and_retained_for_gate_lineage(status):
    async def run():
        node = client()
        node.cache.instrument.return_value = None
        row = _owner(node.gateway.sdk, "fixture-ended", symbol="UNSEEN", status=status)
        assert await node._generate_order_status_reports(SCOPE) == []
        assert node.report_trace["orders"]["skipped_unloaded"] == 1
        assert [held for held, _ in node.withheld] == [row]
        assert not node._provides_bulk_position_coverage(InstrumentId.from_str("UNSEEN.ALPACA"))
        await node._generate_order_status_reports(SCOPE)
        assert node.report_trace["orders"]["skipped_unloaded"] == 1  # Per invocation.

    asyncio.run(run())


@pytest.mark.parametrize("shape", ["missing", "cycle"])
def test_terminal_history_cannot_hide_an_unresolved_replacement(tmp_path, shape):
    venue = AccountVenue()
    row = _owner(venue, "O-ended", symbol="UNSEEN", status="replaced")
    row["replaced_by"] = row["id"] if shape == "cycle" else "absent-successor"
    before, seeded = deepcopy(venue.rows), len(venue.requests)
    body, code, _ = start(tmp_path, venue)
    assert (code, body["phase"], body["reason"]) == (2, "refused", "replacement_unresolved")
    _no_mutations(venue, seeded, before)


def test_unloaded_fill_history_still_refuses_unexplained_or_excess_fills():
    async def run():
        node = client()
        node.cache.instrument.return_value = None
        venue = node.gateway.sdk = _history()
        venue.activities[0]["cum_qty"] = "6"
        venue.activities[1]["order_id"] = "missing-order"
        with pytest.raises(client_module().IncompleteCoverage) as failure:
            await node._generate_fill_reports(SCOPE)
        assert len(failure.value.failures) == 2
        assert not node.coverage_complete

    asyncio.run(run())


def _nested_history(prefix, status, depth):
    venue = AccountVenue()
    parent = _bracket(venue)
    parent.update(
        symbol="UNSEEN",
        client_order_id=prefix + "compound",
        order_class="oco",
        status="filled",
        qty="5",
        filled_qty="5",
        filled_avg_price="0.1234",
    )
    leg = parent["legs"][0]
    leg.update(symbol="UNSEEN", client_order_id=prefix + "leg", status="canceled")
    if depth == 2:
        leg["legs"] = [dict(leg, id="nested-fixture-leg", client_order_id=prefix + "deep-leg")]
        leg = leg["legs"][0]
    if status is None:
        leg.pop("status")
    else:
        leg["status"] = status
    venue.activities = [
        dict(
            id="fixture-compound-fill",
            order_id=parent["id"],
            activity_type="FILL",
            transaction_time=STAMP,
            type="fill",
            symbol="UNSEEN",
            price="0.1234",
            qty="5",
            side="buy",
            leaves_qty="0",
            cum_qty="5",
            order_status="filled",
        )
    ]
    return venue


@pytest.mark.parametrize("prefix", ["O-", "BTEST-", "owner-"])
@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("status", ["pending_cancel", "future_status", None])
def test_unloaded_terminal_parent_cannot_hide_a_working_nested_leg(
    tmp_path, capfd, prefix, depth, status
):
    venue = _nested_history(prefix, status, depth)
    before, seeded = deepcopy(venue.rows), len(venue.requests)
    body, code, stand = start(tmp_path, venue)
    _no_mutations(venue, seeded, before)
    assert code == 2, (body["phase"], body["gate"], body["reconciliation"])
    assert body["gate"] == {} and stand.subscribed == 0
    trace = body["reconciliation"]
    assert trace["orders"]["reason"] == REASON
    assert trace["orders"]["skipped_unloaded"] == 0
    assert trace["fills"]["skipped_unloaded"] == 0
    text = json.dumps(trace) + capfd.readouterr().err
    assert "UNSEEN" not in text and "0.1234" not in text
    assert all(row["id"] not in text for row in venue.rows.values())


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("status", ["pending_cancel", "future_status", None])
def test_unloaded_fill_cannot_hide_a_working_nested_leg(depth, status):
    # Native mass status stops after the order-stage refusal; exercise fills directly too.
    async def run():
        node = client()
        node.cache.instrument.return_value = None
        venue = node.gateway.sdk = _nested_history("owner-", status, depth)
        before, seeded = deepcopy(venue.rows), len(venue.requests)
        with pytest.raises(client_module().IncompleteCoverage):
            await node._generate_fill_reports(SCOPE)
        assert node.report_trace["fills"]["reason"] == REASON
        assert node.report_trace["fills"]["skipped_unloaded"] == 0
        assert not node.coverage_complete
        _no_mutations(venue, seeded, before)

    asyncio.run(run())


def test_unloaded_all_terminal_nested_history_is_skipped_and_counted(tmp_path):
    venue = _nested_history("owner-", "expired", 2)
    # A sibling at each depth must be checked too; all five terminal statuses occur.
    parent = next(iter(venue.rows.values()))
    leg = parent["legs"][0]
    leg["legs"].append(dict(leg["legs"][0], status="rejected"))
    parent["legs"].append(dict(leg, status="replaced", legs=None))
    before, seeded = deepcopy(venue.rows), len(venue.requests)
    body, code, _ = start(tmp_path, venue)
    _no_mutations(venue, seeded, before)
    assert (code, body["phase"]) == (0, "stopped"), body
    assert all(count == 0 for count in body["gate"].values())
    trace = body["reconciliation"]
    assert trace["orders"]["skipped_unloaded"] == 1
    assert trace["fills"]["skipped_unloaded"] == 1
    assert trace["positions"]["skipped_unloaded"] == 0
