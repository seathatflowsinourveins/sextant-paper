"""Synthetic custody facts; these do not simulate native external-order claiming."""

from decimal import Decimal as D
from typing import Any

from sextant.execution.custody import (
    AccountScope,
    AccountSnapshot,
    Custody,
    OrderFact,
    PositionAuthority,
)
from sextant.risk.controls import Controls

SEC = 1_000_000_000
SCOPE = AccountScope("ALPACA", "fixture-paper-b")
IID = "AAA.ALPACA"


def control_rows(**changes):
    values = {
        "request_budget": 180,
        "broker_limit": 200,
        "window_s": "60",
        "burst_fraction": "0.1",
        "cancel_timeout_s": "10",
        "cancel_retry_s": "5",
        "flatten_tick_s": "2",
        "backoff_base_s": "1",
        "backoff_max_s": "30",
        "reserve_per_open_position": 1,
        "reserve_per_exiting_position": 1,
        "observation_reminder_s": "300",
        "observation_max_edges": 500,
        "observation_money_step_fraction": "0.01",
        "observation_leverage_step": "0.01",
        "max_cancel_retries": 3,
    } | changes
    rows = [
        dict(
            name=name,
            value=value,
            source="TEST ONLY, ADR-0007/0009/0010",
            unit="fixture",
            calibration="UNCALIBRATED",
            last_change="2026-11-06",
            scope="FIXTURE_ONLY",
        )
        for name, value in values.items()
    ]
    return rows


def controls(*, slots=None, **changes):
    rows = control_rows(**changes)
    if slots is None:
        return Controls.from_rows(rows, fixture=True)
    return Controls.from_rows(rows, fixture=True, slots=slots)


def order(
    cid="house-exit",
    *,
    qty="100",
    filled="0",
    side="SELL",
    status="NEW",
    owner: Any = False,
    venue: str | None = "venue-1",
    iid=IID,
):
    return OrderFact(cid, iid, side, D(qty), D(filled), status, venue, owner)


def snapshot(*orders, net="100", epoch=1, complete=True, known_at=SEC, scope=SCOPE, managed=True):
    authority = {
        IID: PositionAuthority(D(net), "TEST supplied prior custody; no native claim proof")
    }
    return AccountSnapshot(
        scope,
        epoch,
        {IID: D(net)},
        authority if managed else {},
        tuple(orders),
        complete,
        known_at,
        "TEST complete account observation",
    )


def custody(tmp_path):
    return Custody(SCOPE, tmp_path / "custody.json", create=True)
