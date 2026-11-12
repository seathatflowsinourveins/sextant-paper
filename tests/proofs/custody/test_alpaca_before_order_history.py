"""Run 3: 8,822 fills, but only the venue's listed order span can be cross-checked.

NT d53849b2 architect_ax execution.rs:1676-1695 bounds fill reads; node/mod.rs:992-996
refuses unexplained positions. Keep the adapter's existing in-span coverage rule.
Synthetic data only; the native node, reconciliation, pacer and gate are real.
"""

import asyncio
import gc
import json
from copy import deepcopy

import pytest

from sextant.execution.pacer import AccountPacer

from . import test_b_order_node
from .alpaca_venue import STAMP
from .conftest import controls
from .test_alpaca_client import client, client_module
from .test_alpaca_node_claims import _holding
from .test_alpaca_node_owner import _owner
from .test_alpaca_unloaded_history import SCOPE, _no_mutations
from .test_b_order_node import AccountVenue, expected, start  # noqa: F401

BEFORE = "2026-11-10T14:59:59.999999999Z"
REASON = "historical fill that no order report explains"


@pytest.fixture(autouse=True)
def history_read_burst(monkeypatch):
    # Fixture only: 89 paced activity pages fit without waiting a real minute.
    monkeypatch.setattr(test_b_order_node, "controls", lambda: controls(burst_fraction="0.5"))
    yield
    gc.collect()  # Release failed native-node cycles before another module builds on this thread.


def _fill(ident, order_id, *, stamp=STAMP, symbol="UNLOADED", side="buy"):
    return dict(
        id=ident,
        order_id=order_id,
        activity_type="FILL",
        transaction_time=stamp,
        type="fill",
        symbol=symbol,
        price="0.1234",
        qty="1",
        side=side,
        leaves_qty="0",
        cum_qty="1",
        order_status="filled",
    )


def _run3_history():
    venue = AccountVenue()
    # Coordinator's clarification: 400 explained, of which 398 are unloaded.
    for index in range(437):
        filled = index < 398 or index >= 435
        symbol = "UNLOADED" if index < 435 else "ZA"
        row = _owner(
            venue,
            f"owner-history-{index}",
            symbol=symbol,
            side="buy" if index % 2 else "sell",
            qty="1",
            status="filled" if filled else "canceled",
            filled_qty="1" if filled else "0",
            filled_avg_price="0.1234" if filled else None,
        )
        if filled:
            venue.activities.append(
                _fill(f"explained-{index}", row["id"], symbol=symbol, side=row["side"])
            )
    venue.activities.extend(
        _fill(f"old-fill-{index}", f"unlisted-{index}", stamp=BEFORE) for index in range(8422)
    )
    return venue


def _counts(trace, *, before=0, unlisted=0, excess=0):
    assert trace["before_order_history"] == before
    assert trace["unlisted_order"] == unlisted
    assert trace["quantity_exceeds"] == excess


def _private_values_absent(text, venue):
    assert "UNLOADED" not in text and "0.1234" not in text
    assert STAMP not in text and BEFORE not in text
    assert all(row["id"] not in text for row in venue.rows.values())
    assert all(row["order_id"] not in text for row in venue.activities)


def test_run3_flat_history_opens_gate_and_receipt_counts_all_89_pages(tmp_path, capfd):
    venue = _run3_history()
    assert len(venue.rows) == 437 and len(venue.activities) == 8822 and not venue.positions
    before, seeded = deepcopy(venue.rows), len(venue.requests)
    # The 89 paced pages plus the account verification need more than the 2-second default.
    body, code, stand = start(tmp_path, venue, duration="5")
    assert (code, body["phase"]) == (0, "stopped"), body
    assert all(value == 0 for value in body["gate"].values()) and stand.subscribed == 3
    trace = body["reconciliation"]
    _counts(trace["fills"], before=8422)
    assert trace["orders"]["skipped_unloaded"] == 435
    assert trace["fills"]["skipped_unloaded"] == 398
    assert trace["fills"]["requests"] == {
        "/account/activities/FILL": {"requests": 89, "rows": 8822},
        "/orders": {"requests": 1, "rows": 437},
    }
    assert trace["orders"]["requests"]["/orders"] == {"requests": 1, "rows": 437}
    _private_values_absent(json.dumps(trace), venue)
    assert "startup reconciliation failed" not in capfd.readouterr().err
    _no_mutations(venue, seeded, before)


@pytest.mark.parametrize("stamp", [STAMP, "2026-11-10T15:00:00.000000001Z"])
def test_unlisted_fill_at_or_after_oldest_order_still_refuses_with_counts(tmp_path, capfd, stamp):
    venue = AccountVenue()
    _owner(venue, "owner-ended", symbol="ZA", status="canceled")
    venue.activities = [
        _fill("recent-fill", "unlisted-recent", stamp=stamp),
        _fill("old-fill", "unlisted-old", stamp=BEFORE),
    ]
    before, seeded = deepcopy(venue.rows), len(venue.requests)
    body, code, stand = start(tmp_path, venue)
    trace = body["reconciliation"]["fills"]
    assert code == 2 and body["gate"] == {} and stand.subscribed == 0
    assert trace["reason"] == REASON and trace["failures"] == 1
    _counts(trace, before=1, unlisted=1)
    (line,) = [
        x for x in capfd.readouterr().err.splitlines() if "startup reconciliation failed" in x
    ]
    for count in ("before_order_history=1", "unlisted_order=1", "quantity_exceeds=0"):
        assert count in line
    _private_values_absent(line + json.dumps(body["reconciliation"]), venue)
    _no_mutations(venue, seeded, before)


def test_even_an_older_listed_fill_with_excess_cumulative_quantity_refuses(tmp_path, capfd):
    venue = AccountVenue()
    row = _owner(venue, "owner-ended", symbol="UNLOADED", status="canceled")
    venue.activities = [_fill("excess-fill", row["id"], stamp=BEFORE)]
    before, seeded = deepcopy(venue.rows), len(venue.requests)
    body, code, stand = start(tmp_path, venue)
    trace = body["reconciliation"]["fills"]
    assert code == 2 and body["gate"] == {} and stand.subscribed == 0
    assert trace["reason"] == REASON and trace["failures"] == 1
    _counts(trace, excess=1)
    line = capfd.readouterr().err
    assert "quantity_exceeds=1" in line and "before_order_history=0" in line
    _private_values_absent(json.dumps(body["reconciliation"]), venue)
    _no_mutations(venue, seeded, before)


def test_empty_order_listing_has_no_span_and_cannot_skip_old_fills(tmp_path):
    venue = AccountVenue()
    venue.activities = [_fill("old-fill", "unlisted-old", stamp=BEFORE)]
    body, code, stand = start(tmp_path, venue)
    trace = body["reconciliation"]["fills"]
    assert code == 2 and body["gate"] == {} and stand.subscribed == 0
    assert trace["reason"] == REASON and trace["failures"] == 1
    _counts(trace, unlisted=1)
    assert all(request[0] == "GET" for request in venue.requests)


def test_pre_history_only_position_is_stopped_by_native_unresolved_position_check(tmp_path, capfd):
    venue = AccountVenue()
    _owner(venue, "owner-ended", symbol="ZA", status="canceled")
    venue.activities = [_fill("old-fill", "unlisted-old", stamp=BEFORE, symbol="ZA")]
    _holding(venue, "ZA", "1")
    before, seeded = deepcopy(venue.rows), len(venue.requests)
    body, code, stand = start(tmp_path, venue)
    trace = body["reconciliation"]["fills"]
    assert "error" not in trace, trace  # Fill coverage completes; engine owns the holding stop.
    _counts(trace, before=1)
    assert code == 2 and body["gate"] == {} and stand.subscribed == 0
    out = capfd.readouterr()
    assert "Unresolved positions during startup reconciliation" in out.out + out.err
    _no_mutations(venue, seeded, before)


def test_pre_history_boundary_uses_oldest_order_and_counts_reset_per_invocation():
    async def run():
        node = client()
        node.cache.instrument.return_value = None
        venue = node.gateway.sdk
        node.gateway.pacer = AccountPacer(controls(burst_fraction="0.5"), lambda: 0)
        _owner(venue, "fixture-newer", status="canceled", submitted_at="2026-11-11T15:00:00Z")
        _owner(venue, "fixture-oldest", status="canceled")
        venue.activities = [_fill("old-fill", "unlisted-old", stamp=BEFORE)]
        assert await node._generate_fill_reports(SCOPE) == []
        _counts(node.report_trace["fills"], before=1)
        venue.activities = [_fill("equal-fill", "unlisted-equal", stamp=STAMP)]
        with pytest.raises(client_module().IncompleteCoverage):
            await node._generate_fill_reports(SCOPE)
        _counts(node.report_trace["fills"], unlisted=1)
        venue.activities = []
        assert await node._generate_fill_reports(SCOPE) == []
        _counts(node.report_trace["fills"])

    asyncio.run(run())
