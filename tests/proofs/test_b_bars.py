"""B's bars and latest-quote reads for the per-symbol trail width (D-30, LEAD-1021 item 6).

One batched SIP bars page for the held symbols, this session's regular hours from the open to
now, and one batched latest-quote read under the poll's validity rules. The fixtures are
invented prices; nothing here is a calibrated value.
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sextant.paper.b_feed import (
    BarRow,
    BarsOutsideRth,
    BHistoricalTape,
    BMarketDataFeed,
    TapeShapeError,
)
from sextant.paper.b_protector import load_b_parameters
from sextant.paper.sessions import ET

SEC = 1_000_000_000


class RawBars:
    """alpaca-py's RESTClient.get, one call per request (common/rest.py:212)."""

    def __init__(self, response) -> None:
        self.response = response
        self.requests: list[tuple[str, dict]] = []

    def get(self, path, data):
        self.requests.append((path, dict(data)))
        return self.response


RTH_NOW = int(datetime(2026, 11, 9, 10, 12, tzinfo=ET).timestamp()) * SEC
RTH_OPEN = int(datetime(2026, 11, 9, 9, 30, tzinfo=ET).timestamp()) * SEC


def raw_bar(h: Any = 200.06, l: Any = 199.94, c: Any = 200, t="2026-11-09T15:05:00Z"):  # noqa: E741
    return {"t": t, "o": 1, "h": h, "l": l, "c": c, "v": 9, "n": 3, "vw": 200}


def page(rows, token=None) -> RawBars:
    return RawBars({"bars": rows, "next_page_token": token})


def test_one_bars_page_is_one_request_for_all_held_symbols_newest_first_on_sip():
    raw = page({"AAPL": [raw_bar(), raw_bar(200.1, 199.9, t="2026-11-09T15:00:00Z")]}, "more")
    rows, token, _ = BHistoricalTape(raw, 10_000, 10_000).bar_page(["AAPL", "MSFT"], 5, RTH_NOW)

    assert len(raw.requests) == 1
    path, fields = raw.requests[0]
    assert path == "/stocks/bars"
    assert fields["symbols"] == "AAPL,MSFT"
    assert str(fields["timeframe"]) == "5Min"
    assert (str(fields["feed"].value), str(fields["sort"].value), fields["limit"]) == (
        "sip",
        "desc",
        10_000,
    )
    assert rows["MSFT"] == ()  # a later symbol of a partial page can come back short
    assert rows["AAPL"][0] == BarRow(
        ts_ns=int(datetime(2026, 11, 9, 10, 5, tzinfo=ET).timestamp()) * SEC,
        high=Decimal("200.06"),
        low=Decimal("199.94"),
        close=Decimal("200"),
    )
    assert len(rows["AAPL"]) == 2
    assert token == "more"  # noqa: S105 - a page token, not a secret: the history is partial


def test_bars_are_read_from_the_rth_open_to_now_and_never_outside_rth():
    raw = page({})
    tape = BHistoricalTape(raw, 10_000, 10_000)
    tape.bar_page(["AAPL"], 5, RTH_NOW, page_token="more")  # noqa: S106 - a page token
    fields = raw.requests[0][1]
    assert (fields["start"], fields["end"]) == (
        "2026-11-09T14:30:00+00:00",
        "2026-11-09T15:12:00+00:00",
    )
    assert fields["page_token"] == "more"  # noqa: S105 - a truncated page continues
    pre = int(datetime(2026, 11, 9, 9, 0, tzinfo=ET).timestamp()) * SEC
    with pytest.raises(BarsOutsideRth):
        tape.bar_page(["AAPL"], 5, pre)
    assert tape.bar_page([], 5, RTH_NOW)[:2] == ({}, None)
    assert len(raw.requests) == 1  # neither a pre-market nor an empty read sends a request


@pytest.mark.parametrize(
    ("h", "l", "c"),
    [
        (101, 100, 95.5),  # close below its own low (probe 1: 22% tighter)
        (100.05, 99.95, 102.95),  # close above its own high (probe 11: 57% tighter)
        (95, 105, 100),  # low above high (probe 4: 3x tighter)
        (None, 99, 100),
        (101, 99, None),
        (0, 99, 100),
        (101, -1, 100),
        ("NaN", 99, 100),
        (101, 99, "Infinity"),
    ],
)
def test_a_bad_bar_is_never_dropped_or_taken_the_page_is_refused(h, l, c):  # noqa: E741
    raw = page({"AAPL": [raw_bar(), raw_bar(h, l, c, t="2026-11-09T15:00:00Z")]})
    with pytest.raises(TapeShapeError):
        BHistoricalTape(raw, 10_000, 10_000).bar_page(["AAPL"], 5, RTH_NOW)


@pytest.mark.parametrize("token", ["", 123, ["x"]])
def test_a_malformed_continuation_token_is_refused_as_quote_page_does(token):
    raw = page({"AAPL": [raw_bar()]}, token)
    with pytest.raises(TapeShapeError):
        BHistoricalTape(raw, 10_000, 10_000).bar_page(["AAPL"], 5, RTH_NOW)


def test_a_bars_page_for_another_symbol_or_without_the_documented_fields_is_malformed():
    with pytest.raises(TapeShapeError):
        BHistoricalTape(page({"TSLA": []}), 10_000, 10_000).bar_page(["AAPL"], 5, RTH_NOW)
    short = {"t": "2026-11-09T15:05:00Z", "o": 1, "h": 2, "l": 1, "c": 1, "v": 9}
    with pytest.raises(TapeShapeError):
        BHistoricalTape(page({"AAPL": [short]}), 10_000, 10_000).bar_page(["AAPL"], 5, RTH_NOW)


class LatestOnly:
    def __init__(self, quotes) -> None:
        self.quotes = quotes
        self.requests: list[Any] = []

    def get_stock_latest_quote(self, request):
        self.requests.append(request)
        return self.quotes

    def get_stock_latest_trade(self, request):
        raise AssertionError("the width reads quotes only")


def test_the_latest_quote_read_is_one_request_with_the_pollers_validity_rules():
    at = datetime(2026, 11, 9, 15, 0, tzinfo=ET)

    class Raw:
        def __init__(self, bid, ask):
            self.bid_price, self.ask_price, self.timestamp = bid, ask, at

    client = LatestOnly({"AAPL": Raw(200.0, 200.01), "MSFT": Raw(301.0, 300.0)})
    seen = BMarketDataFeed(client).latest_quotes(["AAPL", "MSFT", "AMD"], "sip")

    assert len(client.requests) == 1
    assert client.requests[0].symbol_or_symbols == ["AAPL", "MSFT", "AMD"]
    assert [(q.symbol, q.bid, q.ask) for q in seen] == [
        ("AAPL", Decimal("200.0"), Decimal("200.01"))
    ]


ROOT = Path(__file__).parents[2]


def test_the_bars_page_size_comes_from_its_row_and_is_unavailable_without_it():
    raw = page({})
    BHistoricalTape(raw, 10_000, 500).bar_page(["AAPL"], 5, RTH_NOW)
    assert raw.requests[0][1]["limit"] == 500
    with pytest.raises(ValueError):
        BHistoricalTape(raw, 10_000).bar_page(["AAPL"], 5, RTH_NOW)
    assert len(raw.requests) == 1


def test_main_builds_the_tape_with_the_bars_row(tmp_path, monkeypatch):
    from test_b_protector_main import FakeTradingClient as MainClient
    from test_b_protector_main import patch_stream, write_config

    import sextant.paper.b_protector_main as entry

    built: list[tuple] = []

    class Recorder(BHistoricalTape):
        def __init__(self, *args) -> None:
            built.append(args[1:])
            super().__init__(*args)

    monkeypatch.setenv("ALPACA_PAPER_2_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_2_SECRET_KEY", "test-secret")
    monkeypatch.setenv("B_EXPECTED_ACCOUNT_SUFFIX", "ture")
    monkeypatch.setattr(entry, "BHistoricalTape", Recorder)
    patch_stream(monkeypatch)
    config = write_config(tmp_path, "b-slot2.json")
    rows = json.loads((ROOT / "lanes/paper/b-parameters.json").read_text())
    for row in rows:  # distinct values, so a swapped or missing bars row fails (review item 5)
        row["value"] = "500" if row["name"] == "tape_max_bars" else row["value"]
    parameters = tmp_path / "b-parameters.json"
    parameters.write_text(json.dumps(rows))
    argv = ["--config", str(config), "--parameters", str(parameters)]
    assert entry.main(argv, client_factory=lambda **_: MainClient(), on_page=lambda _: None) == 0
    assert built == [(10_000,), (10_000, 500)]  # the start read's tape, then the width's bars


def test_the_bars_page_size_has_one_uncalibrated_registry_row_with_its_source():
    path = ROOT / "lanes/paper/b-parameters.json"
    row = next(r for r in json.loads(path.read_text()) if r["name"] == "tape_max_bars")
    assert row["value"] == "10000" and row["calibration_state"] == "UNCALIBRATED"
    assert "UNCALIBRATED" in row["source"] and "rest.py" in row["source"]
    assert load_b_parameters(path).tape_max_bars == Decimal("10000")


# --- The lead's review of part 1 (LEAD-REVIEW-bw1-1ec614b) ------------------------------------


def tape(rows, token=None, max_bars=10_000) -> BHistoricalTape:
    return BHistoricalTape(page(rows, token), 10_000, max_bars)


@pytest.mark.parametrize(
    "times",
    [
        [
            "2026-11-09T15:05:00Z",
            "2026-11-09T15:05:00Z",
        ],  # a repeated bar (probe: ATR 0.10, not 1.74)
        ["2026-11-09T15:00:00Z", "2026-11-09T15:05:00Z"],  # newest first is broken
        ["2026-11-09T15:07:00Z"],  # off the 5-minute grid
        ["2026-11-06T15:05:00Z"],  # another day, outside the requested window
        ["2026-11-09T14:25:00Z"],  # before the 09:30 open
        ["2026-11-09T15:15:00Z"],  # after the 10:12 fetch
        [1762700700],  # a number, which pydantic reads as 1970
    ],
)
def test_a_duplicate_out_of_order_off_grid_or_out_of_window_bar_time_refuses_the_page(times):
    rows = {"AAPL": [raw_bar(t=t) for t in times]}
    with pytest.raises(TapeShapeError):
        tape(rows).bar_page(["AAPL"], 5, RTH_NOW)


@pytest.mark.parametrize("rows", [None, 0, "", {}])
def test_a_null_or_non_list_row_set_refuses_as_quote_page_does_and_rows_are_bounded(rows):
    with pytest.raises(TapeShapeError):
        tape({"AAPL": rows}).bar_page(["AAPL"], 5, RTH_NOW)
    times = ["2026-11-09T15:05:00Z", "2026-11-09T15:00:00Z", "2026-11-09T14:55:00Z"]
    with pytest.raises(TapeShapeError):  # three rows against a two-row page
        tape({"AAPL": [raw_bar(t=t) for t in times]}, max_bars=2).bar_page(["AAPL"], 5, RTH_NOW)


def test_outside_rth_is_its_own_signal_and_an_empty_set_returns_before_any_check():
    pre = int(datetime(2026, 11, 9, 9, 0, tzinfo=ET).timestamp()) * SEC
    with pytest.raises(BarsOutsideRth) as refused:
        tape({}).bar_page(["AAPL"], 5, pre)
    assert not isinstance(refused.value, ValueError)  # never mistaken for a refused page
    assert BHistoricalTape(page({}), 10_000).bar_page([], 5, pre)[:2] == ({}, None)


def test_the_page_returns_the_instant_it_was_fetched_at():
    rows, token, fetched_ns = tape({"AAPL": [raw_bar()]}).bar_page(["AAPL"], 5, RTH_NOW)
    assert (len(rows["AAPL"]), token, fetched_ns) == (1, None, RTH_NOW)


def test_the_tape_refuses_a_bars_bound_below_one():
    for bound in (0, -1):
        with pytest.raises(ValueError):
            BHistoricalTape(page({}), 10_000, bound)
