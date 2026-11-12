"""The paper commission rows reach the execution client through its factory (E5 u8).

sextant.alpaca.commission.paper_commission_resolver (merged as #478) validates the two
sourced rows in lanes/paper/shared-node.params.json and returns the client's
``commission_for_fill`` hook. NT d53849b examples/live/_template/factories.py: the
factory opens no resource; the adapter guide's fail-startup contract: connect refuses.
"""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from nautilus_trader.model import Money, TraderId

from sextant.alpaca import execution, factories
from sextant.alpaca.commission import COMMISSION_ROW, REGULATORY_ROW
from sextant.alpaca.config import AlpacaExecutionClientConfig

from .conftest import controls

PARAMS = Path(__file__).resolve().parents[3] / "lanes/paper/shared-node.params.json"


def rows():
    return json.loads(PARAMS.read_text())


def changed(name, **fields):
    edited = copy.deepcopy(rows())
    next(row for row in edited if row["name"] == name).update(fields)
    return edited


def build(commission_rows, monkeypatch):
    fixture_controls = controls()
    monkeypatch.setattr(execution.Controls, "from_rows", lambda rows: fixture_controls)
    config = AlpacaExecutionClientConfig(
        api_key_env="FIXTURE_KEY",
        api_secret_env="FIXTURE_SECRET",  # noqa: S106 - environment variable name, not a key.
        account_id="ALPACA-002",
        house_prefix="fixture-",
        pacing_rows=[],
        commission_rows=commission_rows,
    )
    clock = Mock()
    clock.timestamp_ns.return_value = 0
    return factories.AlpacaExecutionClientFactory.create(
        name="ALPACA",
        config=config,
        cache=cast("Any", Mock()),
        clock=clock,
        trader_id=TraderId("ALPACA-001"),
    )


def test_factory_passes_the_sourced_paper_commission_hook_and_connect_proceeds(monkeypatch):
    sdk, stream = Mock(), Mock()
    monkeypatch.setattr(execution, "TradingClient", sdk)
    monkeypatch.setattr(execution, "TradingStream", stream)
    monkeypatch.setenv("FIXTURE_KEY", "dummy-key")
    monkeypatch.setenv("FIXTURE_SECRET", "dummy-secret")
    client = build(rows(), monkeypatch)
    hook = client.commission_for_fill
    assert callable(hook)
    assert hook({"order": {"asset_class": "us_equity"}}) == Money.from_str("0 USD")
    client._do_query_account = AsyncMock()
    setattr(client, "create_task", lambda coroutine, name: coroutine.close())  # noqa: B010
    asyncio.run(client._connect())
    sdk.assert_called_once()


@pytest.mark.parametrize(
    "commission_rows",
    [
        pytest.param(None, id="no-rows"),
        pytest.param([r for r in rows() if r["name"] != REGULATORY_ROW], id="missing-row"),
        pytest.param(changed(COMMISSION_ROW, value=0.0), id="float-value"),
        pytest.param(changed(COMMISSION_ROW, source="no official page"), id="unsourced"),
        pytest.param(changed(COMMISSION_ROW, account_scope="live"), id="live-scope"),
        pytest.param(changed(REGULATORY_ROW, account_scope="live"), id="live-fees"),
    ],
)
def test_missing_invalid_or_live_rows_refuse_connect_before_credentials(
    commission_rows, monkeypatch
):
    sdk = Mock()
    monkeypatch.setattr(execution, "TradingClient", sdk)
    monkeypatch.delenv("FIXTURE_KEY", raising=False)
    monkeypatch.delenv("FIXTURE_SECRET", raising=False)
    client = build(commission_rows, monkeypatch)  # The factory itself never raises.
    assert client.commission_for_fill is None
    with pytest.raises(ValueError, match="commission"):
        asyncio.run(client._connect())
    sdk.assert_not_called()


def test_live_scope_is_refused_by_the_resolver_itself():
    from sextant.alpaca.commission import paper_commission_resolver

    with pytest.raises(ValueError, match="paper"):
        paper_commission_resolver(changed(COMMISSION_ROW, account_scope="live"))


def test_commission_rows_survive_config_serialization():
    config = AlpacaExecutionClientConfig(
        api_key_env="FIXTURE_KEY",
        api_secret_env="FIXTURE_SECRET",  # noqa: S106 - environment variable name, not a key.
        account_id="ALPACA-002",
        commission_rows=rows(),
    )
    assert NS(**config.dict()).commission_rows == rows()
