"""ADR-0024 surge30 witnesses: synthetic receipts, not a market cohort or strategy trial."""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.model import (
    AggressorSide,
    InstrumentId,
    Price,
    Quantity,
    QuoteTick,
    TradeId,
    TradeTick,
)

from sextant.entry.parameters import EntryParameters
from sextant.entry.signals import Surge
from sextant.entry.surge import (
    DEFAULT_CONFIG,
    SurgeConfig,
    SurgeState,
    active_flag,
    advance_surge,
    is_closing_print,
    two_sided,
)
from sextant.ranking.baseline import BaselineSample, BaselineTable
from sextant.ranking.engine import Segment
from sextant.ranking.window import NS

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 10, 27)
START = 1_793_088_000 * NS  # 2026-10-27 04:00 ET, explicit fixture boundary.
RTH = Segment("RTH", DAY, START, START + 600 * NS, "fixture XNYS boundary")
RTH40 = Segment("RTH", DAY, START, START + 40 * NS, "fixture XNYS boundary")
PRE = Segment("PRE", DAY, START - 600 * NS, START, "fixture XNYS boundary")
POST = Segment("POST", DAY, START + 40 * NS, START + 600 * NS, "fixture XNYS boundary")
IID = InstrumentId.from_str("SURGE.NASDAQ")


def entry_params(**overrides: Any) -> EntryParameters:
    rows = json.loads((ROOT / "lanes/sim/entry-parameters.json").read_text())
    for row in rows:
        if row["name"] in overrides:
            row["value"] = overrides[row["name"]]
    return EntryParameters.from_rows(rows, first_leg_only=True)


PARAMS = entry_params()


def at(second: int | Decimal) -> int:
    return START + int(D(second) * NS)


def baseline(
    *,
    known_at: int | None = None,
    session: str = "2026-10-26",
    feed: str = "sip",
) -> BaselineTable:
    # A flat complete curve: 600 s of prior-session RTH flow gives a 30 s
    # normal of $30 and 3 trades.
    return BaselineTable(
        (
            BaselineSample("SURGE", "RTH", session, 0, D(0), D(0)),
            BaselineSample("SURGE", "RTH", session, 600, D(600), D(60)),
        ),
        "fixture explicit complete curve; linear within minute",
        START - NS if known_at is None else known_at,
        feed,
    )


def tick(
    second: int | Decimal,
    price: str = "1.00",
    size: str = "100",
    *,
    name: str = "SURGE",
) -> TradeTick:
    stamp = at(second)
    return TradeTick(
        InstrumentId.from_str(name + ".NASDAQ"),
        Price.from_str(price),
        Quantity.from_str(size),
        AggressorSide.BUY,
        TradeId(f"{name}-{stamp}"),
        stamp,
        stamp,  # same-timestamp receipt: the fixture's known_at equals its event time.
    )


def quote(
    second: int | Decimal,
    bid: str = "1.04",
    ask: str = "1.06",
    bid_size: str = "500",
) -> QuoteTick:
    stamp = at(second)
    return QuoteTick(
        IID,
        Price.from_str(bid),
        Price.from_str(ask),
        Quantity.from_str(bid_size),
        Quantity.from_str("500"),
        stamp,
        stamp,
    )


def warm(
    quote_tick: QuoteTick | None = None,
    *,
    segment: Segment = RTH,
    feed: str = "sip",
    baseline_table: BaselineTable | None = None,
    params: EntryParameters = PARAMS,
    config: SurgeConfig = DEFAULT_CONFIG,
    final_known_at: int | None = None,
) -> tuple[SurgeState, Surge | None]:
    """One quiet print, then a rising, unusually large run inside the 30 s window."""
    table = baseline() if baseline_table is None else baseline_table
    state: SurgeState = SurgeState()
    flag: Surge | None = None
    for second, price, size in (
        (0, "1.00", "100"),
        (25, "1.01", "100"),
        (26, "1.01", "100"),
        (30, "1.00", "100"),
        (31, "1.03", "500"),
        (32, "1.04", "500"),
    ):
        state, flag = advance_surge(
            state,
            tick(second, price, size),
            table,
            segment=segment,
            feed=feed,
            params=params,
            config=config,
        )
    state, flag = advance_surge(
        state,
        tick(33, "1.05", "500"),
        table,
        segment=segment,
        feed=feed,
        params=params,
        config=config,
        quote=quote_tick or quote(32),
        known_at=final_known_at,
    )
    return state, flag


def test_qualifying_print_emits_the_first_leg_surge() -> None:
    state, flag = warm()
    assert flag is not None
    assert isinstance(flag, Surge)
    assert flag.instrument_id == IID
    assert flag.segment == RTH.name
    assert flag.first_print_ts == START + 31 * NS  # first print at/above quiet * 1.02
    assert flag.flag_ts == START + 33 * NS
    assert flag.known_at == flag.flag_ts
    assert flag.expires_ts == flag.flag_ts + 120 * NS
    assert flag.quiet_price == D("1.00")
    assert flag.source == "surge30"
    assert state.flag == flag
    assert state.segment_key == (RTH.name, RTH)
    assert state.metrics["dollar_volume"] == D(1862)
    assert state.metrics["trade_count"] == 6
    assert state.metrics["baseline_dollar_volume"] == D(30)
    assert state.metrics["move"] == D("0.05")
    assert state.metrics["rising"] is True
    assert state.metrics["latency_ns"] == 2 * NS
    assert state.metrics["window_start_ts"] == START + 3 * NS
    assert state.metrics["quiet_ts"] == START
    assert state.metrics["quote_ts"] == START + 32 * NS
    assert state.metrics["uncalibrated"] is True


@pytest.mark.parametrize(
    "quote_tick",
    [
        quote(30),  # 3 s old, beyond the 1 s RTH start
        quote(32, "1.06", "1.06"),  # locked, not two-sided
        quote(32, "1.04", "1.06", bid_size="0"),  # one-sided size
    ],
)
def test_stale_or_one_sided_quote_emits_no_flag(quote_tick: QuoteTick) -> None:
    state, flag = warm(quote_tick)
    assert flag is None
    assert state.flag is None
    assert two_sided(quote_tick, START + 33 * NS, RTH, DEFAULT_CONFIG) is False


@pytest.mark.parametrize("session", ["2026-10-27", "2026-10-28"])
def test_current_or_future_baseline_session_refuses(session: str) -> None:
    # A baseline curve dated the trade's own session (or later) is lookahead.
    with pytest.raises(ValueError, match="baseline session"):
        warm(baseline_table=baseline(session=session))


@pytest.mark.parametrize(("feed", "table_feed"), [("boats", "sip"), ("iex", "sip")])
def test_baseline_feed_mismatch_refuses(feed: str, table_feed: str) -> None:
    with pytest.raises(ValueError, match="baseline feed mismatch"):
        advance_surge(
            SurgeState(),
            tick(0),
            baseline(feed=table_feed),
            segment=RTH,
            feed=feed,
            params=PARAMS,
        )


def test_future_baseline_known_at_refuses() -> None:
    table = baseline(known_at=START + 33 * NS)
    with pytest.raises(ValueError, match="future baseline"):
        advance_surge(
            SurgeState(),
            tick(0),
            table,
            segment=RTH,
            feed="sip",
            params=PARAMS,
        )


def test_newest_quote_is_kept_when_an_older_one_arrives() -> None:
    table = baseline()
    state = SurgeState()
    for second, price, size in (
        (0, "1.00", "100"),
        (25, "1.01", "100"),
        (26, "1.01", "100"),
        (30, "1.00", "100"),
        (31, "1.03", "500"),
        (32, "1.04", "500"),
    ):
        state, _ = advance_surge(
            state,
            tick(second, price, size),
            table,
            segment=RTH,
            feed="sip",
            params=PARAMS,
        )

    crossed = quote(32, "1.06", "1.06")  # newest, but locked
    state, flag = advance_surge(
        state,
        tick(D("32.5"), "1.04", "500"),
        table,
        segment=RTH,
        feed="sip",
        params=PARAMS,
        quote=crossed,
    )
    assert flag is None
    assert state.quote == crossed

    older_valid = quote(D("31.9"), "1.04", "1.06")
    state, flag = advance_surge(
        state,
        tick(D("32.8"), "1.05", "500"),
        table,
        segment=RTH,
        feed="sip",
        params=PARAMS,
        quote=older_valid,
    )
    assert flag is None
    assert state.flag is None
    assert state.quote == crossed  # the older book never replaces the newer watermark


def test_expiry_is_anchored_to_the_flag_print() -> None:
    state, flag = warm(quote(33), final_known_at=at(34))
    assert flag is not None
    assert flag.flag_ts == at(33)
    assert flag.known_at == at(34)
    assert flag.expires_ts == flag.flag_ts + 120 * NS
    assert active_flag(state, at(152)) == flag
    assert active_flag(state, at(153)) is None  # expiry is flag_ts + 120 s, not known_at + 120 s


def test_quote_freshness_is_measured_at_availability() -> None:
    state, flag = warm(quote(32), final_known_at=at(40))
    assert flag is None
    assert state.flag is None


def test_an_expired_flag_is_not_emitted() -> None:
    # The quote would still be "fresh" under this widened age, so only the
    # expiry guard can refuse a flag known after its first-leg deadline.
    config = replace(DEFAULT_CONFIG, quote_max_age_rth_s=10_000)
    state, flag = warm(quote(33), final_known_at=at(154), config=config)
    assert flag is None
    assert state.flag is None


def test_episode_latch_suppresses_a_second_flag_past_expiry() -> None:
    state, first = warm()
    assert first is not None
    assert state.latched is True
    # Keep the same unusual-volume/move episode alive for 143 s, well past the 120 s expiry.
    for second in range(34, 177):
        price = str(D("1.05") + D(second - 33) / 1000)
        state, flag = advance_surge(
            state,
            tick(second, price, "500"),
            baseline(),
            segment=RTH,
            feed="sip",
            params=PARAMS,
            quote=quote(second - 1),
        )
        assert flag is None
    assert state.flag == first
    assert state.latched is True
    assert active_flag(state, START + 176 * NS) is None  # expired, so the latch is what refused


def test_a_later_anomaly_episode_can_flag_again() -> None:
    state, first = warm()
    assert first is not None

    state, flag = advance_surge(
        state, tick(153, "1.05", "100"), baseline(), segment=RTH, feed="sip", params=PARAMS
    )
    assert flag is None
    assert state.latched is False  # the anomaly episode cleared after expiry
    assert state.flag == first

    # Rebuild a fresh 30 s episode inside the same segment. At 185 s the quiet
    # reference moves from 1.05 to the 154 s print at 1.00, so a 5 % move arms.
    for second in (154, 160, 161, 170, 171):
        state, flag = advance_surge(
            state,
            tick(second, "1.00", "100"),
            baseline(),
            segment=RTH,
            feed="sip",
            params=PARAMS,
        )
        assert flag is None
    for second, price in ((181, "1.03"), (182, "1.04"), (183, "1.04"), (184, "1.04")):
        state, flag = advance_surge(
            state,
            tick(second, price, "500"),
            baseline(),
            segment=RTH,
            feed="sip",
            params=PARAMS,
        )
        assert flag is None

    state, second_flag = advance_surge(
        state,
        tick(185, "1.05", "500"),
        baseline(),
        segment=RTH,
        feed="sip",
        params=PARAMS,
        quote=quote(184),
    )
    assert second_flag is not None
    assert second_flag != first
    assert second_flag.flag_ts == at(185)
    assert second_flag.first_print_ts == at(181)
    assert state.flag == second_flag
    assert state.latched is True


@pytest.mark.parametrize("condition", ["6", "M", "9"])
def test_closing_print_is_ignored(condition: str) -> None:
    state, _ = advance_surge(
        SurgeState(), tick(0), baseline(), segment=RTH, feed="sip", params=PARAMS
    )
    assert is_closing_print((condition,)) is True
    assert is_closing_print(("F",)) is False
    returned, flag = advance_surge(
        state,
        tick(33, "1.05", "500"),
        baseline(),
        segment=RTH,
        feed="sip",
        params=PARAMS,
        conditions=(condition,),
    )
    assert flag is None
    assert returned is state  # neither history nor clocks moved
    assert returned.history == state.history


def test_segment_change_resets_state() -> None:
    state, first = warm()
    assert first is not None
    assert state.latched is True
    new_tick = tick(-1)
    returned, flag = advance_surge(
        state, new_tick, baseline(), segment=PRE, feed="sip", params=PARAMS
    )
    assert flag is None
    assert returned.segment_key == (PRE.name, PRE)
    assert returned.symbol == "SURGE"
    assert len(returned.history.buckets) == 1
    assert returned.history.buckets[0].ticks == (new_tick,)
    assert returned.volume_history.buckets[0].ticks == (new_tick,)
    assert returned.flag is None
    assert returned.latched is False
    assert returned.metrics == {}
    assert returned.known_at == START - NS


def test_segment_transition_resets_before_skipping_an_ineligible_print() -> None:
    state, first = warm(segment=RTH40)
    assert first is not None
    returned, flag = advance_surge(
        state,
        tick(40, "1.05", "500"),
        baseline(),
        segment=POST,
        feed="sip",
        params=PARAMS,
        price_eligible=False,
        volume_eligible=False,
    )
    assert flag is None
    assert returned is not state
    assert returned.segment_key == (POST.name, POST)
    assert returned.flag is None
    assert returned.latched is False
    assert not returned.history.buckets
    assert active_flag(returned, POST.start_ns) is None


@pytest.mark.parametrize("window_s", [90, 150])
def test_first_leg_window_agrees_with_entry_registry(window_s: int) -> None:
    assert PARAMS.ns("first_leg_window_s") == DEFAULT_CONFIG.first_leg_window_s * NS
    with pytest.raises(ValueError, match="first_leg_window_s"):
        advance_surge(
            SurgeState(),
            tick(0),
            baseline(),
            segment=RTH,
            feed="sip",
            params=entry_params(first_leg_window_s=window_s),
        )


def test_params_hash_mismatch_raises() -> None:
    state, _ = advance_surge(
        SurgeState(), tick(0), baseline(), segment=RTH, feed="sip", params=PARAMS
    )
    changed = replace(DEFAULT_CONFIG, surge_move_min=D("0.03"))
    assert changed.params_hash != DEFAULT_CONFIG.params_hash
    with pytest.raises(ValueError, match="parameters changed"):
        advance_surge(
            state,
            tick(1),
            baseline(),
            segment=RTH,
            feed="sip",
            params=PARAMS,
            config=changed,
        )


def test_surge_config_rejects_floats_and_uncalibrated_status() -> None:
    assert DEFAULT_CONFIG.status == "UNCALIBRATED"
    float_move: Any = 0.02
    calibrated: Any = "CALIBRATED"
    with pytest.raises(TypeError, match="never float"):
        SurgeConfig(surge_move_min=float_move)
    with pytest.raises(ValueError, match="UNCALIBRATED"):
        SurgeConfig(status=calibrated)
