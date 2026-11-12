"""Daily NASDAQ Trader directory staging: fixture copies only, no network or key.

The fixtures are excerpts of the two files downloaded 2026-11-11 from the manifest row's
addresses, bytes unchanged (CRLF, header, rows and the File Creation Time trailer).
"""

from __future__ import annotations

import configparser
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sextant.tradingview.bars import build_universe
from sextant.tradingview.listings import (
    BASE,
    HEADERS,
    RECEIPT,
    ListingError,
    creation_time,
    load_timeout,
    stage,
    validate,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/nasdaqtrader"
DEPLOY = ROOT / "deploy/listings"
NOW = datetime(2026, 11, 11, 8, tzinfo=UTC)
HTML = b"<html><head><title>Request Rejected</title></head><body>error</body></html>"


def good(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fetcher(overrides: dict[str, bytes] | None = None, *, fail: str | None = None):
    calls: list[tuple[str, float]] = []

    def get(url: str, timeout: float) -> bytes:
        calls.append((url, timeout))
        name = url.removeprefix(BASE)
        if name == fail:
            raise TimeoutError("fixture timeout")
        return (overrides or {}).get(name, good(name))

    return get, calls


def test_good_files_are_staged_atomically_with_a_receipt(tmp_path: Path) -> None:
    get, calls = fetcher()
    receipt = stage(tmp_path, timeout=60.0, get=get, now=lambda: NOW)
    assert [url for url, _ in calls] == [BASE + name for name in HEADERS]
    assert all(timeout == 60.0 for _, timeout in calls)
    for name in HEADERS:
        assert (tmp_path / name).read_bytes() == good(name)
    rows = {item["file"]: item for item in receipt["files"]}
    assert rows["nasdaqlisted.txt"]["rows"] == 2
    assert rows["nasdaqlisted.txt"]["file_creation_time"] == "1110202621:31"
    assert rows["nasdaqlisted.txt"]["unknown_exchange_rows"] == 0
    assert json.loads((tmp_path / RECEIPT).read_text()) == receipt
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(".")]


@pytest.mark.parametrize(
    "damage",
    ["truncated", "html", "short_row", "not_utf8", "no_rows", "timeout"],
)
def test_a_bad_download_keeps_both_previous_files(tmp_path: Path, damage: str) -> None:
    previous = {name: b"previous good " + name.encode() for name in HEADERS}
    for name, data in previous.items():
        (tmp_path / name).write_bytes(data)
    other = good("otherlisted.txt")
    lines = other.split(b"\r\n")
    bad = {
        "truncated": other[: len(other) // 2],
        "html": HTML,
        "short_row": b"\r\n".join([lines[0], b"A|Agilent|N", *lines[2:]]),
        "not_utf8": other.replace(b"Agilent", b"Agil\xffnt"),
        "no_rows": b"\r\n".join([lines[0], lines[-2], b""]),
    }
    get, _ = fetcher(
        {"otherlisted.txt": bad[damage]} if damage in bad else None,
        fail="otherlisted.txt" if damage == "timeout" else None,
    )
    with pytest.raises(ListingError):
        stage(tmp_path, timeout=60.0, get=get, now=lambda: NOW)
    assert {name: (tmp_path / name).read_bytes() for name in HEADERS} == previous
    assert not (tmp_path / RECEIPT).exists()


def test_html_and_truncation_are_refused_by_name() -> None:
    with pytest.raises(ListingError, match="header"):
        validate("nasdaqlisted.txt", HTML)
    data = good("nasdaqlisted.txt")
    with pytest.raises(ListingError, match="File Creation Time"):
        validate("nasdaqlisted.txt", data[: data.rindex(b"File")])


def test_an_unknown_exchange_code_is_skipped_and_counted_never_guessed() -> None:
    receipt = validate("otherlisted.txt", good("otherlisted.txt"))
    assert receipt["unknown_exchange_rows"] == 2
    assert receipt["unknown_exchange_codes"] == {"F": 1, "M": 1}
    universe = build_universe(good("nasdaqlisted.txt").decode(), good("otherlisted.txt").decode())
    # CECO (code F) is a common stock but has no documented venue; MTEST is a test issue.
    assert universe == ("A", "AAPL", "IBM")


def test_the_producer_sees_each_file_creation_time() -> None:
    assert creation_time(good("otherlisted.txt").decode()) == "1110202621:31"
    assert creation_time("Symbol|Security Name\nFile Creation Time|2026-11-09\n") == "unknown"


def test_the_unit_runs_the_stage_then_restarts_only_a_running_producer() -> None:
    service = configparser.ConfigParser(interpolation=None, strict=False)
    service.read(DEPLOY / "sextant-tradingview-listings.service")
    body = service["Service"]
    assert body["Type"] == "oneshot"
    assert "with-keys" not in body["ExecStart"] and "KEY" not in body["ExecStart"]
    assert body["ExecStart"].endswith(
        "python -m sextant.tradingview.listings %h/sextant-data/tradingview-prestage"
    )
    assert body["ExecStartPost"] == (
        "/usr/bin/systemctl --user try-restart sextant-tradingview-prestage.service"
    )
    timer = configparser.ConfigParser(interpolation=None)
    timer.read(DEPLOY / "sextant-tradingview-listings.timer")
    parameters = json.loads((DEPLOY / "parameters.json").read_text())["rows"]
    assert timer["Timer"]["OnCalendar"] == parameters["on_calendar"]["value"]
    assert load_timeout(DEPLOY / "parameters.json") == float(parameters["fetch_timeout_s"]["value"])
    assert all(row["status"] == "UNCALIBRATED" and row["source"] for row in parameters.values())
