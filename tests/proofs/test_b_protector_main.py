"""Smoke tests for B's entry wiring against mocked alpaca-py models (clean B PR-1)."""

import asyncio
import json
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import requests
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import TradeAccount
from alpaca.trading.requests import TrailingStopOrderRequest
from alpaca.trading.stream import TradingStream

from sextant.execution.custody import AccountScope, CustodyError, WriterLease
from sextant.execution.pacer import AccountPacer
from sextant.paper.b_protector import ProtectorRefusal
from sextant.paper.b_protector_main import (
    BudgetControls,
    PacedTradingClient,
    PacerRefused,
    main,
)
from sextant.paper.config import PaperBudget
from sextant.paper.decision_record import DecisionLog
from sextant.risk.controls import Controls

ROOT = Path(__file__).parents[2]


def api_account(number: str = "paper-fixture") -> TradeAccount:
    return TradeAccount.model_validate(
        {
            "id": str(uuid4()),
            "account_number": number,
            "status": "ACTIVE",
            "currency": "USD",
            "buying_power": "100000",
            "cash": "100000",
            "portfolio_value": "100000",
            "equity": "100000",
        }
    )


class FakeTradingClient:
    """Serves the configured alpaca-py models and records every broker call."""

    def __init__(self, number: str = "paper-fixture") -> None:
        self.account = api_account(number)
        self.calls: list[str] = []
        # alpaca-py RESTClient's session and retry count (common/rest.py:69,72), never used.
        self._session = requests.Session()
        self._retry = 3

    def get_account(self) -> TradeAccount:
        self.calls.append("get_account")
        return self.account

    def get_all_positions(self):
        self.calls.append("get_all_positions")
        return []

    def get_orders(self, filter=None):
        self.calls.append("get_orders")
        return []

    def get_order_by_client_id(self, client_id):
        self.calls.append("get_order_by_client_id")
        raise AssertionError("the smoke path must not resolve an order identity")

    def submit_order(self, order_data) -> None:
        self.calls.append("submit_order")
        raise AssertionError("the smoke path must not submit")

    def cancel_order_by_id(self, order_id) -> None:
        self.calls.append("cancel_order_by_id")
        raise AssertionError("the smoke path must not cancel")


def write_config(tmp_path: Path, name: str, **overrides) -> Path:
    values = {
        "lane": "alpaca-paper-b",
        "account_id": "paper-fixture",
        "client_id": "ALPACA-B",
        "namespace": "B001",
        "sessions": ["rth"],
    }
    values.update(overrides)
    path = tmp_path / name
    path.write_text(json.dumps(values))
    return path


def no_page(page) -> None:
    return None


def patch_stream(monkeypatch) -> tuple[list, list[str]]:
    """Stop TradingStream before the loop and record the subscribed handler."""

    wired = []
    original_subscribe = TradingStream.subscribe_trade_updates

    def record_subscribe(self, handler):
        wired.append(handler)
        return original_subscribe(self, handler)

    monkeypatch.setattr(TradingStream, "subscribe_trade_updates", record_subscribe)
    runs: list[str] = []
    monkeypatch.setattr(TradingStream, "run", lambda self: runs.append("run"))
    return wired, runs


def test_main_wires_the_handler_refuses_a_slot_one_config_and_checks_the_account_first(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALPACA_PAPER_2_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_2_SECRET_KEY", "test-secret")
    monkeypatch.setenv("B_EXPECTED_ACCOUNT_SUFFIX", "ture")
    parameters = ROOT / "lanes/paper/b-parameters.json"
    config = write_config(tmp_path, "b-slot2.json")
    wired, runs = patch_stream(monkeypatch)

    client = FakeTradingClient()
    result = main(
        ["--config", str(config), "--parameters", str(parameters)],
        client_factory=lambda **_: client,
        on_page=no_page,
    )

    assert result == 0
    # The account check, then bind; outside RTH the one start tape read lists positions.
    assert client.calls[:2] == ["get_account", "get_account"]
    assert set(client.calls[2:]) <= {"get_all_positions"}
    assert runs == ["run"]
    assert len(wired) == 1
    assert asyncio.iscoroutinefunction(wired[0])

    slot_one = write_config(tmp_path, "slot-one.json", lane="alpaca-paper-a")
    slot_one_client = FakeTradingClient()
    with pytest.raises(ProtectorRefusal):
        main(
            ["--config", str(slot_one), "--parameters", str(parameters)],
            client_factory=lambda **_: slot_one_client,
            on_page=no_page,
        )
    assert slot_one_client.calls == []

    foreign = write_config(tmp_path, "foreign.json", account_id="foreign-account")
    monkeypatch.setenv("B_EXPECTED_ACCOUNT_SUFFIX", "ture")
    foreign_client = FakeTradingClient()
    with pytest.raises(ProtectorRefusal):
        main(
            ["--config", str(foreign), "--parameters", str(parameters)],
            client_factory=lambda **_: foreign_client,
            on_page=no_page,
        )
    assert foreign_client.calls == ["get_account", "get_account"]


def test_main_refuses_and_pages_when_the_expected_account_suffix_does_not_match(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALPACA_PAPER_2_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_2_SECRET_KEY", "test-secret")
    monkeypatch.setenv("B_EXPECTED_ACCOUNT_SUFFIX", "ZZZZ")
    parameters = ROOT / "lanes/paper/b-parameters.json"
    config = write_config(tmp_path, "b-slot2.json")
    patch_stream(monkeypatch)
    pages = []
    client = FakeTradingClient()

    with pytest.raises(ProtectorRefusal):
        main(
            ["--config", str(config), "--parameters", str(parameters)],
            client_factory=lambda **_: client,
            on_page=pages.append,
        )

    assert [page.reason for page in pages] == ["account_number_mismatch"]
    assert client.calls == ["get_account"]


def test_main_refuses_to_start_without_an_expected_account(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_2_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_2_SECRET_KEY", "test-secret")
    monkeypatch.delenv("B_EXPECTED_ACCOUNT_SUFFIX", raising=False)
    monkeypatch.delenv("B_EXPECTED_ACCOUNT_ID", raising=False)
    parameters = ROOT / "lanes/paper/b-parameters.json"
    config = write_config(tmp_path, "b-slot2.json")
    patch_stream(monkeypatch)
    client = FakeTradingClient()

    with pytest.raises(SystemExit):
        main(
            ["--config", str(config), "--parameters", str(parameters)],
            client_factory=lambda **_: client,
            on_page=no_page,
        )

    assert client.calls == []


def test_main_prefers_a_full_expected_account_id_over_the_suffix(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_2_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_2_SECRET_KEY", "test-secret")
    monkeypatch.setenv("B_EXPECTED_ACCOUNT_SUFFIX", "ZZZZ")
    monkeypatch.setenv("B_EXPECTED_ACCOUNT_ID", "paper-fixture")
    parameters = ROOT / "lanes/paper/b-parameters.json"
    config = write_config(tmp_path, "b-slot2.json")
    _, runs = patch_stream(monkeypatch)
    pages = []
    client = FakeTradingClient()

    result = main(
        ["--config", str(config), "--parameters", str(parameters)],
        client_factory=lambda **_: client,
        on_page=pages.append,
    )

    assert result == 0
    assert pages == []
    assert runs == ["run"]
    # The account check, then bind; outside RTH the one start tape read lists positions.
    assert client.calls[:2] == ["get_account", "get_account"]
    assert set(client.calls[2:]) <= {"get_all_positions"}


def test_a_second_writer_holding_the_account_lease_refuses_before_any_broker_call(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALPACA_PAPER_2_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_2_SECRET_KEY", "test-secret")
    monkeypatch.setenv("B_EXPECTED_ACCOUNT_SUFFIX", "ture")
    parameters = ROOT / "lanes/paper/b-parameters.json"
    config = write_config(tmp_path, "b-slot2.json")
    _, runs = patch_stream(monkeypatch)

    held = WriterLease(AccountScope("ALPACA", "paper-fixture"), tmp_path / "locks", "other-writer")
    held.acquire()
    factory_calls: list[dict] = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return FakeTradingClient()

    try:
        with pytest.raises(CustodyError):
            main(
                ["--config", str(config), "--parameters", str(parameters)],
                client_factory=factory,
                on_page=no_page,
            )
    finally:
        held.close()

    assert factory_calls == []
    assert runs == []


def test_the_lease_is_released_even_when_the_decision_log_close_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_2_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_2_SECRET_KEY", "test-secret")
    monkeypatch.setenv("B_EXPECTED_ACCOUNT_SUFFIX", "ture")
    parameters = ROOT / "lanes/paper/b-parameters.json"
    config = write_config(tmp_path, "b-slot2.json")
    patch_stream(monkeypatch)
    real_close = DecisionLog.close

    def failing_close(self, timeout: float = 5.0) -> None:
        real_close(self, timeout)
        raise RuntimeError("close broke")

    monkeypatch.setattr(DecisionLog, "close", failing_close)

    with pytest.raises(RuntimeError, match="close broke"):
        main(
            ["--config", str(config), "--parameters", str(parameters)],
            client_factory=lambda **_: FakeTradingClient(),
            on_page=no_page,
        )

    second = WriterLease(AccountScope("ALPACA", "paper-fixture"), tmp_path / "locks", "review")
    second.acquire()
    second.close()


def test_a_deferred_submit_is_retained_and_sent_by_the_next_pacer_drain():
    clock = [0]

    class Broker(FakeTradingClient):
        def __init__(self) -> None:
            super().__init__()
            self.submitted: list[object] = []

        def get_account(self) -> TradeAccount:
            self.calls.append("get_account")
            return self.account

        def submit_order(self, order_data) -> None:
            self.calls.append("submit_order")
            self.submitted.append(order_data)

    broker = Broker()
    budget = PaperBudget(requests=4, window_s=60, broker_cap=4, external_reserve=0, source="test")
    pacer = AccountPacer(cast(Controls, BudgetControls(budget)), now_ns=lambda: clock[0])
    paced = PacedTradingClient(broker, pacer)

    paced.get_account()
    request = TrailingStopOrderRequest(
        symbol="AAPL",
        qty=10,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        trail_percent=2.5,
        client_order_id="B001-trail-AAPL-deferred",
    )

    with pytest.raises(PacerRefused):
        paced.submit_order(request)
    assert broker.submitted == []

    clock[0] += 21_000_000_000
    pacer.drain()

    assert broker.submitted == [request]
