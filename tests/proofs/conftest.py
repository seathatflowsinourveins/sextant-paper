"""Owner-thread native cleanup and offline TradingView account authority."""

import gc
import json
from datetime import UTC, datetime, timedelta

import pytest
from nautilus_trader.common import LogLevel, init_logging
from nautilus_trader.core import UUID4
from nautilus_trader.model import TraderId


@pytest.fixture(scope="session", autouse=True)
def proof_logging():
    """NT d53849b python/tests/conftest.py:58-74; log assertions need INFO enabled."""
    return init_logging(
        trader_id=TraderId("TESTER-000"),
        instance_id=UUID4(),
        level_stdout=LogLevel.INFO,
        is_bypassed=False,
        print_config=False,
    )


@pytest.fixture(autouse=True)
def owner_thread_gc():
    """NT d53849b python/tests/conftest.py:148-154, before SDK workers start too."""
    gc.collect()
    yield gc.collect
    gc.collect()


@pytest.fixture(autouse=True)
def offline_tv_account(request, tmp_path, monkeypatch):
    if request.node.path.name not in {
        "test_tradingview.py",
        "test_tradingview_account.py",
        "test_tradingview_stop_record.py",
        "test_tradingview_get_stream.py",
        "test_tradingview_record_failure.py",
        "test_tradingview_empty_response.py",
        "test_tradingview_symbols.py",
        "test_tradingview_bridge.py",
        "test_tradingview_loop.py",
    }:
        return
    from sextant.tradingview import account

    template = json.loads(account.DEFAULT_PARAMETERS.read_text())
    for row in template["rows"]:
        row["value"] = {
            "account_state_path": str(tmp_path / "state.json"),
            "account_calls_per_utc_day": 10000,
            "account_request_min_interval_s": 2,
            "account_request_timeout_s": 60,
        }[row["name"]]
        row["calibration_state"] = (
            "FIXED" if row["name"] == "account_state_path" else "UNCALIBRATED"
        )
        row["source"] = "offline fixture only; no operating authority"
    params = tmp_path / "account.params.json"
    params.write_text(json.dumps(template))
    monkeypatch.setattr(account, "DEFAULT_PARAMETERS", params)
    at = [datetime(2026, 11, 11, 15, tzinfo=UTC)]
    monkeypatch.setattr(account, "_utcnow", lambda: at[0])

    async def sleep(seconds):
        at[0] += timedelta(seconds=seconds)

    monkeypatch.setattr(account, "_sleep", sleep)
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "schema": "tv-account.state/2",
                "day": "2026-11-11",
                "calls": 0,
                "stopped": False,
                "quota_stop": False,
                "last_call": None,
                "cache": {},
                "audit": [],
            }
        )
    )
