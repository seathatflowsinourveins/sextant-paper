"""P-R4 and P-R4b (MODELED): B's dynamic width and tighten-only modify on a real BacktestNode.

The simulated venue ignores ``ModifyOrder.params``: only the trigger moves in replay. The
``trailing_offset`` value is asserted on the command the Strategy issues; the venue half (PATCH
``trail``) is proved later on the adapter's fake venue and on paper (LEAD-2201 B1, G4, G8).
"""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

from nautilus_trader.model import (
    AggressorSide,
    Bar,
    BarType,
    Price,
    Quantity,
    TradeId,
    TradeTick,
)
from test_b_lifecycle import NS, PENNY, quote
from test_b_lifecycle_entry import QUOTES, TAPE
from test_b_lifecycle_protect import OPEN, run, trails

from sextant.entry.indicators import DecimalATR

ROWS = {  # test inputs shaped like lanes/paper/b-parameters.json's width rows
    "alpaca_min_trail_fraction": D("0.001"),
    "trail_atr_period": D(3),
    "trail_atr_bar_minutes": D(1),
    "trail_atr_multiple": D(2),
}
BAR = BarType.from_str(f"{PENNY}-1-MINUTE-LAST-EXTERNAL")
MINUTE = 60 * NS
VOLUME = Quantity.from_int(1000)
# (open, high, low, close) per completed minute after 09:30 ET; the ranges narrow, then widen.
SHAPES = [
    ("0.5300", "0.5340", "0.5280", "0.5320"),
    ("0.5320", "0.5360", "0.5300", "0.5350"),
    ("0.5350", "0.5380", "0.5330", "0.5370"),
    ("0.5370", "0.5385", "0.5365", "0.5380"),
    ("0.5380", "0.5390", "0.5375", "0.5385"),
    ("0.5385", "0.5600", "0.5300", "0.5390"),
]


def bars(shapes=SHAPES) -> list[Bar]:
    out = []
    for i, (open_, high, low, close) in enumerate(shapes):
        prices = [Price.from_str(value) for value in (open_, high, low, close)]
        at = OPEN + (i + 1) * MINUTE
        out.append(Bar(BAR, prices[0], prices[1], prices[2], prices[3], VOLUME, at, at))
    return out


def rth_tape(shapes=SHAPES) -> list[TradeTick]:
    return [
        TradeTick(
            PENNY,
            Price.from_str(shape[3]),
            Quantity.from_int(100),
            AggressorSide.BUY,
            TradeId(f"rth-{i}"),
            OPEN + (i + 1) * MINUTE - NS,
            OPEN + (i + 1) * MINUTE - NS,
        )
        for i, shape in enumerate(shapes)
    ]


def rth_quotes(shapes=SHAPES) -> list:
    out = [quote(OPEN + NS // 2, "0.5290", "0.5300")]
    for i, shape in enumerate(shapes):
        close = D(shape[3])
        out.append(quote(OPEN + (i + 1) * MINUTE - NS // 2, f"{close - D('0.0005')}", shape[3]))
    return out


def run_width(tmp_path, *, shapes=SHAPES, **kwargs: Any):
    kwargs.setdefault("width_rows", ROWS)
    return run(
        tmp_path,
        quotes=[*QUOTES, *rth_quotes(shapes)],
        bars=bars(shapes),
        tape=TAPE,
        extra_trades=rth_tape(shapes),
        **kwargs,
    )


def widths(journal) -> list[dict[str, Any]]:
    return [row for row in journal if row["kind"] == "width"]


def test_the_width_comes_from_completed_bars_only_and_the_oracle_agrees(tmp_path) -> None:
    journal, _, _ = run_width(tmp_path)
    rows = widths(journal)
    assert rows, "a width needs trail_atr_period completed RTH bars"
    for row in rows:
        completed = [b for b in bars() if b.ts_init <= row["ts"]]  # no later bar is read
        atr = DecimalATR(3)
        for bar in completed:
            atr.handle_bar(bar)
        assert atr.value is not None and D(row["atr"]) == atr.value
        assert abs(D(repr(row["oracle"])) - atr.value) <= atr.value * D("1e-9")
    assert rows[0]["ts"] == bars()[2].ts_init  # the first bar that completes the period


def test_a_new_trail_without_a_width_arms_at_the_native_row_and_pages(tmp_path) -> None:
    journal, orders, _ = run_width(tmp_path)
    first = trails(orders)[0]
    assert first.trailing_offset == D("250.00")  # native 0.025, before any RTH bar
    assert any(row.get("reason") == "width_unavailable" for row in journal)
