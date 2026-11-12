"""Explicit synthetic inputs, never registry authority or market evidence."""

import json
from decimal import Decimal as D
from pathlib import Path

import pytest
from nautilus_trader.model import (
    AggressorSide,
    Bar,
    BarType,
    Currency,
    Equity,
    InstrumentId,
    Price,
    Quantity,
    QuoteTick,
    Symbol,
    TradeId,
    TradeTick,
)

from sextant.entry.parameters import EntryParameters

ROOT = Path(__file__).parents[3]
SEC = 1_000_000_000
START = 1_799_000_000 * SEC
IID = InstrumentId.from_str("AAA.SIM")
VALUES = {
    "first_leg_window_s": "120",
    "first_leg_trades_count": 3,
    "first_leg_span_s": "1",
    "first_leg_max_move": "0.10",
    "entry_max_vwap_extension_atr": "2",
    "entry_chase_bps": "10",
    "entry_ttl_s": "5",
    "reclaim_lookback_s": "30",
    "reclaim_trades_count": 3,
    "reclaim_trades_span_s": "1",
    "reclaim_hold_s": "2",
    "reclaim_max_above_buffers": "1",
    "atr_period": 3,
    "atr_bar_minutes": 1,
    "max_data_age_s": "5",
    "max_armed_symbols": 5,
    "bp_target_fraction": "1",
    "stop_ref_fraction": "0.01",
    "portfolio_slots": 5,
    "stopout_size_decay": "0.5",
    "stopout_size_floor": "0.25",
    "confidence_size_floor": "0.5",
    "chase_cap_fraction": "0.01",
    "flow_share_cap": "0.05",
    "flow_share_window_s": "60",
    "covered_flow_window_s": "2",
    "covered_flow_baseline_min": "1",
    "covered_flow_ratio": "1",
}


def rows() -> list[dict]:
    return [
        {
            "name": name,
            "unit": "count" if type(value) is int else "fixture unit",
            "value": value,
            "source": "ADR-0004; TEST ONLY, no deployment authority",
            "calibration": "UNCALIBRATED",
            "last_change": "2026-11-06",
            "scope": "FIXTURE_ONLY",
        }
        for name, value in VALUES.items()
    ]


def params(**overrides) -> EntryParameters:
    fixture_rows = rows()
    for row in fixture_rows:
        if row["name"] in overrides:
            row["value"] = overrides[row["name"]]
    return EntryParameters.from_rows(fixture_rows, fixture=True)


@pytest.fixture
def instrument() -> Equity:
    return Equity(
        IID,
        Symbol("AAA"),
        Currency.from_str("USD"),
        2,
        Price.from_str("0.01"),
        0,
        0,
        lot_size=Quantity.from_int(1),
    )


def trade(seconds: str | int, price: str, *, iid=IID, size=100, lag=0, identity=None) -> TradeTick:
    ts = START + int(D(seconds) * SEC)
    return TradeTick(
        iid,
        Price.from_str(price),
        Quantity.from_int(size),
        AggressorSide.NO_AGGRESSOR,
        TradeId(identity or str(ts)),
        ts,
        ts + lag,
    )


def quote(seconds: str | int, bid: str, ask: str) -> QuoteTick:
    ts = START + int(D(seconds) * SEC)
    return QuoteTick(
        IID,
        Price.from_str(bid),
        Price.from_str(ask),
        Quantity.from_int(100),
        Quantity.from_int(100),
        ts,
        ts,
    )


def bar(seconds: int, *, iid=IID, minutes=1) -> Bar:
    ts = START + seconds * SEC
    return Bar(
        BarType.from_str(f"{iid}-{minutes}-MINUTE-LAST-EXTERNAL"),
        Price.from_str("10.00"),
        Price.from_str("10.10"),
        Price.from_str("9.90"),
        Price.from_str("10.00"),
        Quantity.from_int(100),
        ts,
        ts,
    )


def registry_rows() -> list[dict]:
    return json.loads((ROOT / "lanes/sim/entry-parameters.json").read_text())
