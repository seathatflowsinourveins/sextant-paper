"""B's trail width from each symbol's own measured noise (D-30, Entries 77 and 80, LEAD-1021).

trail_fraction(s) = min(native, max(minimum, noise_floor(s) / basis(s),
                                    multiple x ATR(s) / basis(s)))
with noise_floor = max(one_tick, spread) (ADR-0005 X2) and ATR the rc8 Wilder ATR on completed
bars. The fixtures are invented prices; nothing here is a calibrated value.
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.indicators import AverageTrueRange, MovingAverageType

from sextant.paper.b_protector import load_b_parameters
from sextant.paper.b_width import (
    BarRow,
    MarketView,
    completed_atr,
    trail_fraction,
    trail_percent,
    view_from_quote,
)
from sextant.paper.sessions import ET

NATIVE = Decimal("0.025")
MINIMUM = Decimal("0.001")
MULTIPLE = Decimal("3")
MINUTE_NS = 60 * 1_000_000_000
START_NS = 1_762_786_800 * 1_000_000_000  # an arbitrary bar boundary


def width(atr, view):
    return trail_fraction(native=NATIVE, minimum=MINIMUM, multiple=MULTIPLE, atr=atr, view=view)


def bars(ranges, *, minutes=5, close=Decimal("100")):
    return [
        BarRow(
            ts_ns=START_NS + index * minutes * MINUTE_NS,
            high=close + spread / 2,
            low=close - spread / 2,
            close=close,
        )
        for index, spread in enumerate(ranges)
    ]


def test_a_quiet_symbol_goes_below_a_quarter_percent_down_to_its_measured_noise():
    quiet = MarketView(bid=Decimal("200"), spread=Decimal("0.01"), one_tick=Decimal("0.01"))
    fraction = width(Decimal("0.10"), quiet)  # 3 x 0.10 / 200 = 0.0015
    assert fraction is not None and fraction == Decimal("0.0015")
    assert fraction < Decimal("0.0025")
    assert isinstance(fraction, Decimal)


def test_the_spread_floor_wins_when_it_is_wider_than_the_atr_term():
    wide_spread = MarketView(bid=Decimal("10"), spread=Decimal("0.05"), one_tick=Decimal("0.01"))
    assert width(Decimal("0.01"), wide_spread) == Decimal("0.005")  # 0.05 / 10


def test_a_noisy_symbol_stays_wide_and_never_above_the_native_cap():
    noisy = MarketView(bid=Decimal("4"), spread=Decimal("0.02"), one_tick=Decimal("0.01"))
    assert width(Decimal("0.20"), noisy) == NATIVE  # 3 x 0.20 / 4 = 0.15, capped


def test_a_very_quiet_symbol_never_goes_below_the_minimum():
    still = MarketView(bid=Decimal("500"), spread=Decimal("0.01"), one_tick=Decimal("0.01"))
    assert width(Decimal("0.01"), still) == MINIMUM


def test_without_an_atr_or_a_basis_there_is_no_width():
    view = MarketView(bid=Decimal("100"), spread=Decimal("0.01"), one_tick=Decimal("0.01"))
    assert width(None, view) is None
    assert width(Decimal("0.1"), None) is None
    with pytest.raises(TypeError):
        width(0.1, view)  # floats never enter the width


def test_the_quote_view_uses_the_outside_hours_tick_rule():
    assert view_from_quote(Decimal("0.50"), Decimal("0.51")).one_tick == Decimal("0.0001")
    view = view_from_quote(Decimal("100.00"), Decimal("100.03"))
    assert (view.bid, view.spread, view.one_tick) == (
        Decimal("100.00"),
        Decimal("0.03"),
        Decimal("0.01"),
    )


def test_the_percent_sent_is_rounded_up_to_two_decimals_and_stays_decimal():
    assert trail_percent(Decimal("0.025")) == Decimal("2.50")
    assert trail_percent(Decimal("0.0018")) == Decimal("0.18")
    assert trail_percent(Decimal("0.00181")) == Decimal("0.19")  # wider, never tighter
    assert trail_percent(Decimal("0.001")) == Decimal("0.10")
    assert isinstance(trail_percent(Decimal("0.0015")), Decimal)


def test_atr_counts_only_completed_bars():
    rows = bars([Decimal("0.5")] * 3 + [Decimal("5")])
    last_open_ns = rows[-1].ts_ns
    # The fourth bar is still forming: its large range must not enter the ATR.
    assert completed_atr(rows, 3, 5, last_open_ns + MINUTE_NS, last_open_ns + MINUTE_NS) == Decimal(
        "0.5"
    )
    assert completed_atr(
        rows, 3, 5, last_open_ns + 5 * MINUTE_NS, last_open_ns + 5 * MINUTE_NS
    ) == Decimal("2")


def test_atr_is_none_until_the_period_has_completed_bars():
    rows = bars([Decimal("0.5")] * 2)
    assert (
        completed_atr(rows, 3, 5, rows[-1].ts_ns + 5 * MINUTE_NS, rows[-1].ts_ns + 5 * MINUTE_NS)
        is None
    )
    assert completed_atr([], 3, 5, START_NS, START_NS) is None


def test_atr_ignores_the_order_bars_arrive_in():
    rows = bars([Decimal("0.4"), Decimal("0.6"), Decimal("0.5"), Decimal("0.9")])
    now = rows[-1].ts_ns + 5 * MINUTE_NS
    assert completed_atr(list(reversed(rows)), 3, 5, now, now) == completed_atr(
        rows, 3, 5, now, now
    )


def test_atr_matches_nautilus_rc8_wilder_average_true_range():
    closes = [Decimal(v) for v in ("100", "100.4", "99.8", "100.9", "101.3", "100.7", "101.8")]
    rows = [
        BarRow(
            ts_ns=START_NS + index * 5 * MINUTE_NS,
            high=close + Decimal("0.35") + Decimal(index) / 20,
            low=close - Decimal("0.25"),
            close=close,
        )
        for index, close in enumerate(closes)
    ]
    native = AverageTrueRange(3, MovingAverageType.Wilder)
    for row in rows:
        native.update_raw(float(row.high), float(row.low), float(row.close))
    ours = completed_atr(rows, 3, 5, rows[-1].ts_ns + 5 * MINUTE_NS, rows[-1].ts_ns + 5 * MINUTE_NS)
    assert ours is not None and native.initialized
    assert abs(float(ours) - native.value) < 1e-9


SEC = 1_000_000_000


def test_atr_reads_only_this_sessions_rth_bars_and_needs_a_recent_newest_bar():
    open_ns = START_NS - 30 * MINUTE_NS  # START_NS is 10:00 ET
    quiet = Decimal("0.01")
    pre = [
        BarRow(
            open_ns - (i + 1) * 5 * MINUTE_NS,
            Decimal(100) + quiet,
            Decimal(100) - quiet,
            Decimal(100),
        )
        for i in range(66)
    ]  # probe 6: quiet pre-market bars seeded the ATR 3.3x tighter
    rth = bars([Decimal("1")] * 3)
    now = rth[-1].ts_ns + 5 * MINUTE_NS
    assert completed_atr(pre + rth, 3, 5, now, now) == Decimal("1")
    assert completed_atr(rth, 3, 5, now + 5 * MINUTE_NS - 1, now + 5 * MINUTE_NS - 1) == Decimal(
        "1"
    )
    assert (
        completed_atr(rth, 3, 5, now + 5 * MINUTE_NS, now + 5 * MINUTE_NS) is None
    )  # no bar of the interval closed
    evening = int(datetime(2025, 11, 10, 18, 0, tzinfo=ET).timestamp()) * SEC
    assert (
        completed_atr(rth, 3, 5, evening, evening) is None
    )  # outside RTH there are no RTH bars now


def test_the_atr_is_usable_only_when_the_newest_bar_is_the_interval_that_just_closed():
    def at(hour: int, minute: int, second: int = 0) -> int:
        return int(datetime(2026, 11, 9, hour, minute, second, tzinfo=ET).timestamp()) * SEC

    def ending(hour: int, minute: int) -> list[BarRow]:  # three 5-minute bars, newest ends here
        last = at(hour, minute) - 5 * MINUTE_NS
        one, mid = Decimal("1"), Decimal("100")
        return [
            BarRow(last - i * 5 * MINUTE_NS, mid + one / 2, mid - one / 2, mid) for i in range(3)
        ]

    # The Codex max witness (LEAD-1021, per completed bar interval): bars ending 11:20 give no
    # new width from 11:25:00 on; the last good width is kept and the reason paged (part 2).
    assert completed_atr(ending(11, 20), 3, 5, at(11, 24, 59), at(11, 24, 59)) == Decimal("1")
    assert completed_atr(ending(11, 20), 3, 5, at(11, 25), at(11, 25)) is None
    assert completed_atr(ending(11, 20), 3, 5, at(11, 29, 59), at(11, 29, 59)) is None
    assert completed_atr(ending(11, 25), 3, 5, at(11, 25), at(11, 25)) == Decimal("1")


def test_the_width_refuses_a_cap_above_the_native_max_a_non_positive_minimum_and_floats():
    view = MarketView(bid=Decimal("100"), spread=Decimal("0.01"), one_tick=Decimal("0.01"))
    one = Decimal("1")
    with pytest.raises(ValueError):  # probe 14: 0.03 gave trail_percent 3.00, above 2.50
        trail_fraction(
            native=Decimal("0.03"), minimum=MINIMUM, multiple=MULTIPLE, atr=one, view=view
        )
    for minimum in (Decimal(0), Decimal("-0.001")):
        with pytest.raises(ValueError):
            trail_fraction(native=NATIVE, minimum=minimum, multiple=MULTIPLE, atr=one, view=view)
    floats: list[dict[str, Any]] = [{"native": 0.025}, {"minimum": 0.001}, {"multiple": 3.0}]
    for bad in floats:  # probe 15: a float bound leaked out as the width
        with pytest.raises(TypeError):
            trail_fraction(
                **({"native": NATIVE, "minimum": MINIMUM, "multiple": MULTIPLE} | bad),
                atr=one,
                view=view,
            )
    for field in ("bid", "spread", "one_tick"):
        fields: dict[str, Any] = {"bid": view.bid, "spread": view.spread, "one_tick": view.one_tick}
        fields[field] = 0.01
        floaty = MarketView(**fields)
        with pytest.raises(TypeError):
            width(one, floaty)


def test_the_percent_and_the_quote_view_refuse_impossible_values():
    for fraction in (Decimal(0), Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):  # probe 16: 0.00, -1.00 and NaN were formatted
            trail_percent(fraction)
    for bid, ask in (("10", "9.9"), ("0", "0.01"), ("-1", "0.01"), ("NaN", "1")):
        with pytest.raises(ValueError):  # probe 17: a crossed quote gave a negative spread
            view_from_quote(Decimal(bid), Decimal(ask))


ROOT = Path(__file__).parents[2]
WIDTH_ROWS = {
    "alpaca_min_trail_fraction": "0.001",
    "trail_atr_multiple": "3",
    "trail_atr_period": "22",
    "trail_atr_bar_minutes": "5",
}


def test_each_width_tunable_has_one_uncalibrated_registry_row_with_its_source():
    path = ROOT / "lanes/paper/b-parameters.json"
    rows = {row["name"]: row for row in json.loads(path.read_text())}
    params = load_b_parameters(path)
    for name, value in WIDTH_ROWS.items():  # probes 24 and 25: no row for these values
        assert rows[name]["value"] == value and rows[name]["calibration_state"] == "UNCALIBRATED"
        assert "UNCALIBRATED" in rows[name]["source"]
        assert getattr(params, name) == Decimal(value)


# --- The lead's review of part 1 (LEAD-REVIEW-bw1-1ec614b) ------------------------------------


def et(hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(2026, 11, 9, hour, minute, second, tzinfo=ET).timestamp()) * SEC


def test_a_bar_counts_as_complete_only_if_it_ended_by_the_time_the_page_was_fetched():
    one, mid = Decimal("1"), Decimal("100")
    done = [
        BarRow(et(11, 15) - i * 5 * MINUTE_NS, mid + one / 2, mid - one / 2, mid) for i in range(3)
    ]
    forming = BarRow(et(11, 20), mid + Decimal("0.005"), mid - Decimal("0.005"), mid)  # 0.01 so far
    rows = [*done, forming]
    # Fetched at 11:24:59, the 11:20 bar was still forming: no width at 11:25:00 (review item 2).
    assert completed_atr(rows, 3, 5, et(11, 25), et(11, 24, 59)) is None
    assert completed_atr(rows, 3, 5, et(11, 25), et(11, 25)) is not None  # fetched once closed


ZERO, NEG = Decimal(0), Decimal("-1")
VIEW = MarketView(bid=Decimal("100"), spread=Decimal("0.01"), one_tick=Decimal("0.01"))


@pytest.mark.parametrize(
    ("overrides", "view"),
    [
        ({"multiple": ZERO}, VIEW),  # review item 3: each drops a term of the max()
        ({"multiple": Decimal("-3")}, VIEW),
        ({"atr": NEG}, VIEW),
        ({}, MarketView(bid=Decimal("100"), spread=Decimal("-5"), one_tick=Decimal("0.01"))),
        ({}, MarketView(bid=Decimal("100"), spread=Decimal("0.01"), one_tick=ZERO)),
        ({}, MarketView(bid=Decimal("100"), spread=Decimal("0.01"), one_tick=NEG)),
        ({"native": Decimal("0.0005")}, VIEW),  # item 4: under the 0.001 floor
        ({"atr": Decimal("NaN")}, VIEW),  # item 6: never InvalidOperation
        ({"atr": Decimal("Infinity")}, VIEW),
        ({"native": Decimal("NaN")}, VIEW),
        ({"minimum": Decimal("NaN")}, VIEW),
        ({}, MarketView(bid=Decimal("100"), spread=Decimal("NaN"), one_tick=Decimal("0.01"))),
        ({}, MarketView(bid=Decimal("NaN"), spread=Decimal("0.01"), one_tick=Decimal("0.01"))),
        ({}, MarketView(bid=ZERO, spread=Decimal("0.01"), one_tick=Decimal("0.01"))),
        ({}, MarketView(bid=NEG, spread=Decimal("0.01"), one_tick=Decimal("0.01"))),
    ],
)
def test_the_width_refuses_every_input_that_would_drop_a_term_or_is_not_a_number(overrides, view):
    args: dict[str, Any] = {"native": NATIVE, "minimum": MINIMUM, "multiple": MULTIPLE}
    args |= {"atr": Decimal("1")} | overrides
    with pytest.raises(ValueError):
        trail_fraction(**args, view=view)


@pytest.mark.parametrize(
    "overrides",
    [
        {"native_trail_fraction": Decimal("0.0005")},  # item 4: native under the minimum
        {"trail_atr_period": Decimal("2.5")},  # item 8: an AssertionError in the tick
        {"trail_atr_period": Decimal("0")},
        {"trail_atr_bar_minutes": Decimal("2.5")},
        {"tape_max_bars": Decimal("0.5")},  # int() would make it 0
    ],
)
def test_the_registry_refuses_a_native_under_the_minimum_and_a_fractional_count(overrides):
    from sextant.paper.b_protector import BParameters

    facts: dict[str, Any] = {
        "native_trail_fraction": NATIVE,
        "reconcile_interval_s": Decimal("10"),
        "feed_poll_interval_s": Decimal("1"),
        "feed_silence_s": Decimal("5"),
        "tape_max_bars": Decimal("10000"),
        "alpaca_min_trail_fraction": MINIMUM,
        "trail_atr_period": Decimal("22"),
        "trail_atr_bar_minutes": Decimal("5"),
        "trail_atr_multiple": MULTIPLE,
    }
    BParameters(**facts)  # the registry's own values load
    with pytest.raises(ValueError):
        BParameters(**(facts | overrides))


# --- Codex max on 1b at 37bb20e: the two follow-ups closed before the push ---------------------


def honest_and_quiet() -> tuple[list[BarRow], list[BarRow]]:
    """22 bars, 19 of range 2 then 3 of 0.10: the honest ATR22 is about 1.74 (the probe's)."""

    spreads = [Decimal("2")] * 19 + [Decimal("0.10")] * 3
    honest = [
        BarRow(
            et(9, 30) + i * 5 * MINUTE_NS, Decimal(100) + s / 2, Decimal(100) - s / 2, Decimal(100)
        )
        for i, s in enumerate(spreads)
    ]
    return honest, honest[-3:]


def test_bars_merged_from_valid_pages_that_repeat_an_interval_are_refused():
    from test_b_bars import page

    from sextant.paper.b_feed import BHistoricalTape

    honest, quiet = honest_and_quiet()
    honest_atr = completed_atr(honest, 22, 5, et(11, 20), et(11, 20))
    assert honest_atr is not None and honest_atr.quantize(Decimal("0.01")) == Decimal("1.74")
    text = [datetime.fromtimestamp(r.ts_ns // SEC, tz=ET).isoformat() for r in reversed(quiet)]
    rows = [
        {"t": t, "o": 1, "h": 100.05, "l": 99.95, "c": 100, "v": 9, "n": 3, "vw": 100} for t in text
    ]
    merged: list[BarRow] = []
    for _ in range(8):  # eight valid pages, each the same three quiet bars (Codex probe)
        merged += BHistoricalTape(page({"AAPL": rows}), 10_000, 10_000).bar_page(
            ["AAPL"], 5, et(11, 20)
        )[0]["AAPL"]
    with pytest.raises(ValueError):  # it gave 0.10 from three distinct intervals
        completed_atr(merged, 22, 5, et(11, 20), et(11, 20))


@pytest.mark.parametrize("period", [0, Decimal("2.5"), True, -1])
def test_the_atr_refuses_a_period_that_is_not_a_positive_integer(period):
    honest, _ = honest_and_quiet()
    with pytest.raises(ValueError):  # never the AssertionError in DecimalATR.handle_bar
        completed_atr(honest, period, 5, et(11, 20), et(11, 20))
