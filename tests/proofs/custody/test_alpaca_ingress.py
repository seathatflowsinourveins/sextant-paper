"""T2: every inbound trade update passes one identity resolution before it is forwarded.

Events: alpaca-py cc4cb3b7 TradeEvent (trading/enums.py:321-333) and the documented
status and refusal events (Alpaca streaming, read 2026-11-10). Identity states: bound,
confirmed successor not yet bound, unconfirmed successor, stale predecessor (warm or
cold), and an owner order. An event whose lineage is unknown is incomplete coverage,
never forwarded under another id; the active binding only moves forward. EXIT-TABLE-r4.md, T2.
"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest
from nautilus_trader.model import ClientOrderId, Money

from .test_alpaca_client import client, client_module
from .test_alpaca_execution import order
from .test_alpaca_exits import _cmd

STATUS = [
    "new", "accepted", "pending_new", "pending_cancel", "pending_replace", "canceled",
    "expired", "rejected", "done_for_day", "stopped", "calculated", "suspended", "held",
    "accepted_for_bidding", "pending_review",
]  # fmt: skip
FILLS = ["fill", "partial_fill"]
REFUSALS = ["order_cancel_rejected", "order_replace_rejected"]
# "restated" is an alpaca-py TradeEvent (enums.py:321-333) with no documented meaning;
# trade corrections need reconciliation. Neither is forwarded: incomplete coverage.
UNCLASSIFIED = ["restated", "fixture_unknown_event", "trade_bust", "trade_correct"]
ROOT = ClientOrderId("fixture-entry")


def _update(event, row):
    row = deepcopy(row)
    data = {"event": event, "order": row, "timestamp": row["updated_at"]}
    if event in FILLS:
        row.update(status="filled" if event == "fill" else "partially_filled")
        row.update(filled_qty=row["qty"] if event == "fill" else "1", filled_avg_price="0.1234")
        data |= {"execution_id": f"fixture-{event}", "price": "0.1234", "qty": row["filled_qty"]}
    elif event in STATUS:
        row["status"] = event
    return {"stream": "trade_updates", "data": data}


async def _state(node, state):
    """A house order (bound) or an owner order: (row, expected stable id, active venue id)."""
    await node._submit_order(NS(order=order(), params={}))
    node.commission_for_fill = lambda data: Money.from_str("0 USD")
    venue = node.gateway.sdk
    first = next(iter(venue.rows.values()))
    if state == "bound":
        return first, ROOT, first["id"]
    fields = ("symbol", "qty", "side", "type", "time_in_force", "limit_price", "extended_hours")
    owner = venue.post(
        "/orders", {k: first[k] for k in fields} | {"client_order_id": "owner-entry"}
    )
    return owner, ClientOrderId("owner-entry"), first["id"]


async def _deliver(node, state, event, row, stable, active, *, held=False):
    """Deliver one event; check identity, forwarding and that the binding never moves back."""
    node._handle_report.reset_mock()
    node.generate_order_updated.reset_mock()
    node.cache.order.reset_mock()
    requests = len(node.gateway.sdk.requests)
    if held or event in UNCLASSIFIED:  # Lineage or event unknown: incomplete coverage.
        with pytest.raises(client_module().IncompleteCoverage):
            await node._on_trade_update(_update(event, row))
        node._handle_report.assert_not_called()
        node.generate_order_updated.assert_not_called()
        assert node._active["fixture-entry"] == active
        if event in UNCLASSIFIED:
            assert len(node.gateway.sdk.requests) == requests  # Refused before identity work.
        return
    await node._on_trade_update(_update(event, row))
    assert node._active["fixture-entry"] == active  # Only ever moves forward.
    reported = [c.args[0] for c in node._handle_report.call_args_list]
    assert all(r.client_order_id == stable for r in reported)
    if event in REFUSALS:
        assert node.cache.order.call_args.args[0] == stable
    elif event in FILLS:
        assert len(reported) == 1  # Executions are real even on an older order.
    elif state.startswith("stale"):
        assert reported == []  # Stale status never moves the engine backwards.
    elif state == "confirmed_unbound":
        node.generate_order_updated.assert_called_once()  # Bound before forwarding.


EVENTS = STATUS + FILLS + REFUSALS + UNCLASSIFIED


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("state", ["bound", "owner"])
def test_every_event_on_a_house_or_owner_order_resolves_identity_once(state, event):
    async def run():
        node = client()
        row, stable, active = await _state(node, state)
        await _deliver(node, state, event, row, stable, active)

    asyncio.run(run())


async def _successor_state(node, state):
    """Successor lineage states after one house modify of the bound order."""
    first, _, _ = await _state(node, "bound")
    venue = node.gateway.sdk
    await node._modify_order(_cmd("modify"))
    if state == "unconfirmed":
        return deepcopy(venue.pending[1]), None, first["id"]
    successor = venue.confirm()
    if state == "confirmed_unbound":
        return successor, ROOT, first["id"]  # Bound by this event's own resolution.
    stale = deepcopy(first) | {"status": "new", "replaced_by": None}
    if state == "stale_cold":  # A restarted client: no binding in memory.
        node._active.clear()
        node._roots.clear()
        node._active_n.clear()
    await node._report(venue.rows[successor["id"]])
    if state == "successor_bound":
        return deepcopy(venue.rows[successor["id"]]), ROOT, successor["id"]
    return stale, ROOT, successor["id"]  # The original order's late snapshot.


# A successor row that says pending_new does not confirm its lineage (the venue has not
# finished the replace): held as incomplete coverage. A confirmed one in pending_replace is
# being replaced again and stays confirmed (PR 588 P1-a).
UNCONFIRMING = {"pending_new"}


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize(
    "state", ["confirmed_unbound", "successor_bound", "unconfirmed", "stale_warm", "stale_cold"]
)
def test_every_event_on_a_successor_lineage_resolves_identity_once(state, event):
    async def run():
        node = client()
        row, stable, active = await _successor_state(node, state)
        held = state == "unconfirmed" or (
            state in ("confirmed_unbound", "successor_bound") and event in UNCONFIRMING
        )
        if state == "confirmed_unbound" and not held and event not in UNCLASSIFIED:
            active = row["id"]  # The one resolution binds the venue-confirmed successor.
        await _deliver(node, state, event, row, stable, active, held=held)

    asyncio.run(run())
