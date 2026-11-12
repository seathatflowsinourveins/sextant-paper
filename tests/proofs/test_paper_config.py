"""Account and calendar fences at the shared paper-node boundary (#84)."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from nautilus_trader.model import InstrumentId

from sextant.paper.config import PaperAccount, load_budget
from sextant.paper.sessions import Session, session_at

ROOT = Path(__file__).parents[2]
ET = ZoneInfo("America/New_York")


def ns(local: str) -> int:
    return int(datetime.fromisoformat(local).replace(tzinfo=ET).timestamp()) * 1_000_000_000


def account(lane="alpaca-paper-a", **kwargs):
    return PaperAccount(
        lane=lane,
        account_id="paper-fixture",
        client_id="ALPACA-A",
        namespace="A001",
        sessions=(Session.RTH,),
        **kwargs,
    )


@pytest.mark.parametrize(
    "lane,rate,window,cap,reserve",
    [
        ("alpaca-paper-a", 180, 60, 200, 0),
        ("alpaca-paper-b", 180, 60, 200, 0),
        ("alpaca-paper-c", 150, 60, 200, 50),
        ("ibkr-paper", 30, 1, 50, 0),
    ],
)
def test_budget_consumes_the_shared_limit_sheet(lane, rate, window, cap, reserve):
    budget = load_budget(ROOT / "lanes/limits.yaml", lane)
    assert (budget.requests, budget.window_s, budget.broker_cap, budget.external_reserve) == (
        rate,
        window,
        cap,
        reserve,
    )


def test_ib_40_per_second_needs_rotation_paced_receipt():
    assert (
        load_budget(ROOT / "lanes/limits.yaml", "ibkr-paper", ib_rotation_paced=True).requests == 40
    )


def test_live_or_unknown_lane_never_builds_a_paper_config():
    for lane in ("alpaca-live", "anything"):
        with pytest.raises(ValueError):
            account(lane)


@pytest.mark.parametrize("client_id", [" ", "alpaca-\u00e9"])
def test_a_client_id_rc7_refuses_fails_at_load_not_before_every_send(client_id):
    """#163 author review: an all-space or non-ASCII ID would halt every order before its hook."""
    with pytest.raises(ValueError):
        PaperAccount("alpaca-paper-b", "paper-fixture", client_id, "B001", (Session.RTH,))


def test_only_b_can_claim_external_instruments():
    iid = InstrumentId.from_str("AAPL.XNAS")
    with pytest.raises(ValueError):
        replace(account(), external_claims=(iid,))
    assert replace(account("alpaca-paper-b"), external_claims=(iid,)).external_claims == (iid,)


def test_external_claims_cannot_be_mutated_after_account_validation():
    with pytest.raises(ValueError):
        replace(account(), external_claims=[])


@pytest.mark.parametrize(
    "account_id,port",
    [
        ("U123", 4002),
        ("DU123", 4001),
        ("", 7497),
        ("DU0000000-U1234567", 4002),
        ("D-U1234567", 4002),
        ("DUMMY-F7654321", 7497),
        ("DU0000000 ", 4002),
        ("DU0000000\n", 4002),
        (" DU0000000", 4002),
        ("DUMMY", 4002),
        ("D", 4002),
        ("DU", 4002),
    ],
)
def test_ib_paper_requires_d_account_and_paper_port(account_id, port):
    with pytest.raises(ValueError):
        PaperAccount("ibkr-paper", account_id, "IB-A", "IB001", (Session.RTH,), ib_port=port)


@pytest.mark.parametrize(
    "local,want",
    [
        ("2026-11-06T03:59:59", Session.OVERNIGHT),
        ("2026-11-06T04:00:00", Session.PRE),
        ("2026-11-06T09:30:00", Session.RTH),
        ("2026-11-06T16:00:00", Session.POST),
        ("2026-11-06T20:00:00", Session.CLOSED),
        ("2026-11-08T20:00:00", Session.OVERNIGHT),
        ("2026-11-26T09:30:00", Session.CLOSED),
        ("2026-11-27T12:59:59", Session.RTH),
        ("2026-11-27T13:00:00", Session.POST),
        ("2026-11-27T17:00:00", Session.CLOSED),
        ("2026-10-30T09:30:00", Session.RTH),
        ("2026-11-02T09:30:00", Session.RTH),
    ],
)
def test_xnys_24x5_early_close_holiday_weekend_and_dst(local, want):
    assert session_at(ns(local)) is want


def test_half_open_opening_boundary_uses_integer_nanoseconds():
    opening = ns("2026-11-06T09:30:00")
    assert session_at(opening - 1) is Session.PRE
    assert session_at(opening) is Session.RTH
