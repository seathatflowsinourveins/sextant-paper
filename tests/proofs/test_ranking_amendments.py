"""N1f (LEAD-2357 1b): a corrected or cancelled print inside a name's live windows withdraws
that name's rank until the windows no longer contain it.

Two traded fixture names print on a real BacktestNode; corrections and cancels with the
original trade's identity reach the RankingActor's ``on_data`` at fixture times.
"""

from __future__ import annotations

import json
from decimal import Decimal as D
from typing import Any

from nautilus_trader.model import (
    AggressorSide,
    CustomData,
    DataType,
    InstrumentId,
    Price,
    Quantity,
    TradeId,
    TradeTick,
)
from test_ranking import NS, START
from test_ranking_actor import Logged
from test_ranking_node_session import session_run

from sextant.alpaca.trade_updates import AlpacaTradeCancel, AlpacaTradeCorrection

SECONDS = (0, 60, 120, 180, 240, 300, 310, 320, 330, 340, 350, 360, 420, 480, 540, 600)


def price(second: int) -> str:
    return "0.50" if second < 310 else f"{D('0.55') + D('0.01') * max(0, (second - 360) // 60):.2f}"


def trades() -> list[TradeTick]:
    out = []
    for name, venue_code, offset in (("PENNY", "D", 0), ("LARGE", "Q", NS // 2)):
        iid = InstrumentId.from_str(f"{name}.ALPACA")
        for second in SECONDS:
            at = START + second * NS + offset
            out.append(
                TradeTick(
                    iid,
                    Price.from_str(price(second)),
                    Quantity.from_str("100"),
                    AggressorSide.NO_AGGRESSOR,
                    TradeId(f"{venue_code}-{second}"),
                    at,
                    at,
                )
            )
    return sorted(out, key=lambda tick: tick.ts_event)


def amendment(kind: str, symbol: str, original_id: int, now: int) -> CustomData:
    common: dict[str, Any] = dict(
        instrument_id=InstrumentId.from_str(f"{symbol}.ALPACA"),
        symbol=symbol,
        exchange="D",
        original_id=original_id,
        original_price=D("0.50"),
        original_size=D("100"),
        tape="C",
        ts_event=now,
        ts_init=now,
    )
    if kind == "cancel":
        event: Any = AlpacaTradeCancel(**common, action="C")
    else:
        event = AlpacaTradeCorrection(
            **common,
            corrected_id=original_id + 1,
            corrected_price=D("0.51"),
            corrected_size=D("100"),
            original_conditions=("@",),
            corrected_conditions=("@",),
        )
    name = "AlpacaTradeCancel" if kind == "cancel" else "AlpacaTradeCorrection"
    return CustomData(DataType(name, metadata={"feed": "sip"}), event)


def amended_run(tmp_path, *plan: tuple[int, str, str, int]):
    class Amending(Logged):
        def watch_statuses(self) -> None:
            super().watch_statuses()
            for at_s, kind, symbol, original in plan:

                def deliver(_event, kind=kind, symbol=symbol, original=original) -> None:
                    now = self.clock.timestamp_ns()
                    self.on_data(amendment(kind, symbol, original, now))

                self.clock.set_time_alert_ns(f"amend-{at_s}-{original}", START + at_s * NS, deliver)

    return session_run(tmp_path, actor_cls=Amending, trades=trades())


def rank(out: dict[str, Any], second: int, name: str = "PENNY") -> int | None:
    at = START + second * NS
    (generation,) = [g for g in out["receiver"].received if g.known_at == at]
    return next(row.rank for row in generation.rows if row.symbol == name)


def receipt(tmp_path) -> dict[str, Any]:
    return json.loads((tmp_path / "receipts" / "ranking-2026-10-27-PRE.json").read_text())


def test_baseline_both_names_rank_without_amendments(tmp_path) -> None:
    out = amended_run(tmp_path)
    assert all(rank(out, s) is not None for s in (360, 420, 480, 540, 600))


def test_a_correction_inside_the_window_withdraws_until_the_window_rolls(tmp_path) -> None:
    out = amended_run(tmp_path, (365, "correction", "PENNY", 60))
    assert rank(out, 365) is None  # withdrawn at once, unthrottled
    assert rank(out, 420) is None and rank(out, 480) is None  # the print is still contained
    assert rank(out, 540) is not None  # the windows and their anchor have rolled past it
    assert rank(out, 420, "LARGE") is not None  # no other name is affected
    assert receipt(tmp_path)["amendments"] == {"inside": 1, "outside": 0, "unprimed": 0}


def test_a_correction_outside_every_window_changes_nothing_but_is_counted(tmp_path) -> None:
    out = amended_run(tmp_path, (545, "cancel", "PENNY", 60), (365, "correction", "PENNY", 999))
    assert all(rank(out, s) is not None for s in (420, 480, 540, 600))
    assert receipt(tmp_path)["amendments"] == {"inside": 0, "outside": 2, "unprimed": 0}


def test_a_name_outside_the_universe_is_ignored_and_counted(tmp_path) -> None:
    out = amended_run(tmp_path, (365, "correction", "OTHER", 60))
    assert rank(out, 420) is not None and out["actor"].ignored_amendments == 1


def test_two_overlapping_amendments_withdraw_until_both_prints_leave(tmp_path) -> None:
    out = amended_run(tmp_path, (365, "correction", "PENNY", 60), (370, "cancel", "PENNY", 120))
    assert rank(out, 540) is None  # the 120 s print is still the window's anchor
    assert rank(out, 600) is not None
    assert receipt(tmp_path)["amendments"]["inside"] == 2


def test_the_receipt_sums_the_amended_notional_inside_and_outside(tmp_path) -> None:
    # Lead ruling 03:35: the cumulative dollar volume keeps a corrected print's notional (a
    # known limit); the receipt measures it from the records' own fields, no behaviour change.
    amended_run(
        tmp_path,
        (365, "correction", "PENNY", 60),
        (370, "cancel", "PENNY", 999),
        (375, "correction", "LARGE", 999),
    )
    body = receipt(tmp_path)
    assert body["amendment_notional"] == {"inside": "50.00", "outside": "100.00"}
    assert body["amended_names"] == 2
