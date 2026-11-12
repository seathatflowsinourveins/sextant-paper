"""Parity PR-F1: historical SIP trades with the raw trade fields preserved (A1/A7).

Offline: no key, network, stream or order. The fake HTTP adapter returns fixture pages
from the vendor response shape, and the receipt is re-read from disk.
"""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pyarrow.parquet as pq
import pytest
from alpaca.data.historical import StockHistoricalDataClient
from requests import PreparedRequest, Response
from requests.adapters import HTTPAdapter

from sextant.data.alpaca_history import (
    AlpacaHistory,
    Pacer,
    day_start,
    land_raw,
    land_trades,
    raw_path,
    read_trades,
    trade_path,
)

DAY = date(2024, 11, 29)


def _trade(**overrides: Any) -> dict[str, Any]:
    row = {
        "t": "2024-11-29T14:30:00.123456789Z",
        "p": 123.4567,
        "s": 100,
        "x": "NASDAQ",
        "i": 42,
        "c": ["@", "T"],
        "z": "C",
    }
    row.update(overrides)
    return row


def _payload(*rows: dict[str, Any], cursor: str | None = None) -> str:
    trades = {"AAPL": list(rows)} if rows else None
    return json.dumps({"trades": trades, "next_page_token": cursor})


def _history() -> AlpacaHistory:
    history = AlpacaHistory.__new__(AlpacaHistory)
    StockHistoricalDataClient.__init__(
        history,
        api_key="offline",
        secret_key="offline",  # noqa: S106 - an inert fixture string, as upstream's test
        raw_data=True,
    )
    history.pacer = Pacer()
    history.pages = []
    history._session.hooks["response"].append(history._capture)
    return history


def test_the_trades_request_pins_sip_asof_and_the_new_york_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No adjustment, no bar interval: trades are the raw tape, keyed by the session."""
    bodies = [
        _payload(_trade(), cursor="tok"),
        _payload(_trade(i=43, t="2024-11-29T14:30:01.000000000Z")),
    ]
    queries: list[dict[str, list[str]]] = []

    def send(adapter: HTTPAdapter, request: PreparedRequest, **kwargs: Any) -> Response:
        query = parse_qs(urlsplit(str(request.url)).query)
        queries.append(query)
        response = Response()
        response.status_code = 200
        response.request = request
        response._content = bodies[len(queries) - 1].encode()
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    history = _history()
    try:
        pages = history.fetch_trades("AAPL", DAY)
    finally:
        history.close()

    assert [json.loads(page)["trades"]["AAPL"][0]["i"] for page in pages] == [42, 43]
    first = queries[0]
    assert first["feed"] == ["sip"]
    assert first["asof"] == ["2024-11-29"]
    assert first["sort"] == ["asc"]
    assert first["symbols"] == ["AAPL"]
    assert datetime.fromisoformat(first["start"][0]).astimezone(UTC) == day_start(DAY).astimezone(
        UTC
    )
    assert datetime.fromisoformat(first["end"][0]).astimezone(UTC) == (
        day_start(date(2024, 11, 30)) - timedelta(microseconds=1)
    ).astimezone(UTC)
    assert queries[1]["page_token"] == ["tok"]


def test_the_raw_trade_page_keeps_condition_exchange_tape_and_trade_id(tmp_path: Path) -> None:
    """A7: the vendor record survives the receipt without a typed tick in between."""
    path = trade_path(tmp_path, "AAPL", DAY)
    pages = [
        _payload(_trade(), cursor="tok"),
        _payload(_trade(i=43, t="2024-11-29T14:30:01.000000000Z")),
    ]
    assert land_trades(path, pages, symbol="AAPL", day=DAY) is True

    rows = read_trades(path)
    assert [row["i"] for row in rows] == [42, 43]
    first = rows[0]
    assert first["t"] == "2024-11-29T14:30:00.123456789Z"
    assert first["p"] == Decimal("123.4567")
    assert first["s"] == 100
    assert first["x"] == "NASDAQ"
    assert first["c"] == ["@", "T"]
    assert first["z"] == "C"
    meta = pq.read_table(path).schema.metadata or {}
    assert meta[b"kind"] == b"trades"
    assert meta[b"source"] == b"alpaca_sip"
    assert meta[b"feed"] == b"sip"
    assert meta[b"symbol"] == b"AAPL"
    assert meta[b"asof"] == b"2024-11-29"
    assert meta[b"window"] == b"America/New_York calendar day"


def test_empty_session_lands_as_a_real_receipt(tmp_path: Path) -> None:
    """A session with no prints is evidence of no prints, not a missing file."""
    path = trade_path(tmp_path, "AAPL", DAY)
    assert land_trades(path, [_payload()], symbol="AAPL", day=DAY) is True
    assert read_trades(path) == []


def test_a_later_trade_landing_cannot_change_the_first(tmp_path: Path) -> None:
    path = trade_path(tmp_path, "AAPL", DAY)
    assert land_trades(path, [_payload(_trade())], symbol="AAPL", day=DAY) is True
    assert land_trades(path, [_payload(_trade(i=99, p=1.0))], symbol="AAPL", day=DAY) is False
    assert [row["i"] for row in read_trades(path)] == [42]


def test_a_trade_page_outside_its_session_day_is_refused(tmp_path: Path) -> None:
    """00:00 New York on the next date belongs to the next session."""
    path = trade_path(tmp_path, "AAPL", DAY)
    page = _payload(_trade(t="2024-11-30T05:00:00.000000000Z"))
    assert land_trades(path, [page], symbol="AAPL", day=DAY) is True
    with pytest.raises(ValueError, match="escaped its source-day partition"):
        read_trades(path)


def test_a_trade_landing_without_a_response_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="without a response"):
        land_trades(trade_path(tmp_path, "AAPL", DAY), (), symbol="AAPL", day=DAY)


def test_a_page_without_a_trades_envelope_is_refused(tmp_path: Path) -> None:
    page = json.dumps({"next_page_token": None})
    with pytest.raises(KeyError, match="trades"):
        land_trades(trade_path(tmp_path, "AAPL", DAY), [page], symbol="AAPL", day=DAY)


def test_a_malformed_trade_is_refused_by_the_envelope(tmp_path: Path) -> None:
    page = json.dumps({"trades": {"AAPL": [{"p": 1.0, "s": 100}]}, "next_page_token": None})
    with pytest.raises(ValueError, match="timestamp"):
        land_trades(trade_path(tmp_path, "AAPL", DAY), [page], symbol="AAPL", day=DAY)


def test_read_trades_refuses_a_bars_receipt(tmp_path: Path) -> None:
    path = raw_path(tmp_path, "1min", "AAPL", DAY)
    bars = json.dumps({"bars": {"AAPL": []}, "next_page_token": None})
    assert land_raw(path, [bars], symbol="AAPL", day=DAY, bar="1min") is True
    with pytest.raises(ValueError, match="Not SIP trades"):
        read_trades(path)


def test_a_page_for_another_symbol_is_not_read(tmp_path: Path) -> None:
    path = trade_path(tmp_path, "AAPL", DAY)
    page = json.dumps({"trades": {"MSFT": [_trade()]}, "next_page_token": None})
    assert land_trades(path, [page], symbol="AAPL", day=DAY) is True
    assert read_trades(path) == []


def test_trade_path_refuses_a_symbol_that_is_a_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid equity symbol"):
        trade_path(tmp_path, "../AAPL", DAY)
