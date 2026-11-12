"""Proofs for the Alpaca adapter's provider, configs and factories.

upstream checked: nautilus_trader@d53849b ``examples/live/_template/``;
alpaca-py@cc4cb3b7 ``alpaca/trading/client.py``, ``alpaca/common/rest.py``;
sources per module docstring. No network and no real credential.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Any, cast

import alpaca
import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.trading import client as alpaca_client_module
from alpaca.trading.enums import AssetClass
from alpaca.trading.models import Asset
from alpaca.trading.requests import GetAssetsRequest
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model import (
    AccountId,
    AccountType,
    Currency,
    InstrumentId,
    OmsType,
    Price,
    Symbol,
    TraderId,
    list_tick_schemes,
)

import sextant.alpaca
from sextant.alpaca import constants, factories, providers
from sextant.alpaca.config import AlpacaDataClientConfig, AlpacaExecutionClientConfig
from sextant.alpaca.data import AlpacaDataClient
from sextant.alpaca.execution import AlpacaExecutionClient
from sextant.alpaca.parsing import parse_equity


def _asset(
    symbol: str = "AAPL",
    *,
    increment: float | None = 0.01,
    tradable: bool = True,
    shortable: bool = True,
    easy_to_borrow: bool = True,
    marginable: bool = True,
    fractionable: bool = True,
) -> Asset:
    return cast(
        Asset,
        SimpleNamespace(
            symbol=symbol,
            price_increment=increment,
            tradable=tradable,
            shortable=shortable,
            easy_to_borrow=easy_to_borrow,
            marginable=marginable,
            fractionable=fractionable,
        ),
    )


class FakeTradingClient:
    """A recorded stand-in for ``alpaca.trading.client.TradingClient``."""

    def __init__(self, assets: dict[str, Any], *, raw_data: bool = False) -> None:
        self.assets = assets
        self.raw_data = raw_data
        self.calls: list[str] = []
        self.bulk_requests: list[GetAssetsRequest] = []

    def get_asset(self, symbol: str) -> Any:
        self.calls.append(f"get_asset:{symbol}")
        return self.assets[symbol]

    def get_all_assets(self, request: GetAssetsRequest) -> Any:
        self.calls.append("get_all_assets")
        self.bulk_requests.append(request)
        return list(self.assets.values())


def _raw_row(symbol: str, exchange: str = "NASDAQ") -> dict[str, Any]:
    """One raw assets row as the vendor serves it (``RawData`` is a dict)."""
    return {
        "symbol": symbol,
        "exchange": exchange,
        "tradable": True,
        "marginable": True,
        "shortable": True,
        "easy_to_borrow": True,
        "fractionable": True,
    }


def _provider(
    fake: Any,
    config: InstrumentProviderConfig,
) -> providers.AlpacaInstrumentProvider:
    return providers.AlpacaInstrumentProvider(
        config,
        client_factory=cast("Any", lambda: fake),
    )


_KEY_ENV = "ALPACA_API_KEY"
_SECRET_ENV = "ALPACA_SECRET_KEY"  # noqa: S105 - a variable name, not a secret


def _data_config(**kwargs: Any) -> AlpacaDataClientConfig:
    kwargs.setdefault("api_key_env", _KEY_ENV)
    kwargs.setdefault("api_secret_env", _SECRET_ENV)
    return AlpacaDataClientConfig(**kwargs)


def _exec_config(**kwargs: Any) -> AlpacaExecutionClientConfig:
    kwargs.setdefault("api_key_env", _KEY_ENV)
    kwargs.setdefault("api_secret_env", _SECRET_ENV)
    kwargs.setdefault("account_id", "ALPACA-002")
    return AlpacaExecutionClientConfig(**kwargs)


def test_the_adapter_package_does_not_shadow_alpaca_py() -> None:
    assert sextant.alpaca is not alpaca
    assert sys.modules["alpaca"] is alpaca
    assert factories.TradingClient is alpaca_client_module.TradingClient


def test_the_data_config_holds_environment_names_not_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "dummy-key-value")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "dummy-secret-value")
    config = _data_config()
    assert (config.api_key_env, config.api_secret_env) == (
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
    )
    assert config.feed == "sip"
    for feed in DataFeed:
        assert _data_config(feed=feed.value).feed == feed.value
    with pytest.raises(ValueError, match="feed"):
        _data_config(feed="tape")
    body = config.dict()
    assert body["api_key_env"] == "ALPACA_API_KEY"
    assert "dummy-key-value" not in str(body)
    assert "dummy-secret-value" not in str(body)


def test_an_asset_becomes_an_alpaca_equity_on_the_tiered_tick() -> None:
    equity = parse_equity(_asset("AAPL", increment=0.01), ts_init=11)
    assert equity.id == InstrumentId.from_str("AAPL.ALPACA")
    assert equity.raw_symbol == Symbol("AAPL")
    assert equity.price_precision == 4
    assert equity.price_increment == Price.from_str("0.0001")
    assert equity.ts_event == 11
    assert equity.ts_init == 11
    assert equity.quote_currency == Currency.from_str("USD")
    assert equity.tick_scheme == "ALPACA_US_EQUITY"
    assert "ALPACA_US_EQUITY" in list_tick_schemes()


def test_the_data_factory_builds_a_lazy_alpaca_client_with_our_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, Any]] = []

    class RecordingTradingClient:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

        def get_asset(self, symbol: str) -> Asset:
            return _asset(symbol)

    monkeypatch.setattr(factories, "TradingClient", RecordingTradingClient)
    monkeypatch.setenv("SEXTANT_TEST_API_KEY", "dummy-key")
    monkeypatch.setenv("SEXTANT_TEST_SECRET", "dummy-secret")
    config = AlpacaDataClientConfig(
        api_key_env="SEXTANT_TEST_API_KEY",
        api_secret_env="SEXTANT_TEST_SECRET",  # noqa: S106 - a variable name, not a secret
        instrument_provider=InstrumentProviderConfig(load_ids=["AAPL.ALPACA"]),
    )
    client = factories.AlpacaDataClientFactory.create(
        name="ALPACA",
        config=config,
        cache=cast("Any", None),
        clock=cast("Any", None),
    )
    assert isinstance(client, AlpacaDataClient)
    assert client.venue == constants.VENUE
    provider = client.instrument_provider
    assert isinstance(provider, providers.AlpacaInstrumentProvider)
    assert created == []
    asyncio.run(provider.initialize())
    assert created == [
        {
            "api_key": "dummy-key",
            "secret_key": "dummy-secret",
            "paper": True,
            "raw_data": True,
        },
    ]
    assert [str(instrument.id) for instrument in provider.list_all()] == ["AAPL.ALPACA"]


def test_the_data_factory_names_the_missing_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEXTANT_TEST_MISSING_API_KEY", raising=False)
    config = AlpacaDataClientConfig(
        api_key_env="SEXTANT_TEST_MISSING_API_KEY",
        api_secret_env="SEXTANT_TEST_MISSING_SECRET",  # noqa: S106 - a variable name, not a secret
        instrument_provider=InstrumentProviderConfig(load_ids=["AAPL.ALPACA"]),
    )
    client = factories.AlpacaDataClientFactory.create(
        name="ALPACA",
        config=config,
        cache=cast("Any", None),
        clock=cast("Any", None),
    )
    provider = client.instrument_provider
    assert provider is not None
    with pytest.raises(ValueError, match="SEXTANT_TEST_MISSING_API_KEY"):
        asyncio.run(provider.initialize())


def test_the_execution_factory_builds_a_margin_netting_alpaca_client() -> None:
    config = _exec_config()
    client = factories.AlpacaExecutionClientFactory.create(
        name="ALPACA",
        config=config,
        cache=cast("Any", None),
        clock=cast("Any", None),
        trader_id=TraderId("ALPACA-001"),
    )
    assert isinstance(client, AlpacaExecutionClient)
    assert client.venue == constants.VENUE
    assert client.account_id == AccountId.from_str("ALPACA-002")
    assert client.account_type == AccountType.MARGIN
    assert client.oms_type == OmsType.NETTING
    assert client.instrument_provider is None
    assert client.config is config


def test_the_execution_client_connect_refuses_without_gateway_authority() -> None:
    client = factories.AlpacaExecutionClientFactory.create(
        name="ALPACA",
        config=_exec_config(),
        cache=cast("Any", None),
        clock=cast("Any", None),
        trader_id=TraderId("ALPACA-001"),
    )
    with pytest.raises(ValueError, match="namespace"):
        asyncio.run(client._connect())


def test_the_data_client_connect_initializes_the_provider_and_publishes_instruments() -> None:
    fake = FakeTradingClient({"AAPL": _asset("AAPL")})
    provider_config = InstrumentProviderConfig(load_ids=["AAPL.ALPACA"])
    provider = _provider(fake, provider_config)
    seen: list[Any] = []

    class RecordingClient(AlpacaDataClient):
        def _handle_instrument(self, instrument: object) -> None:
            seen.append(instrument)

    client = RecordingClient(
        name="ALPACA",
        config=_data_config(instrument_provider=provider_config),
        cache=cast("Any", None),
        clock=cast("Any", None),
        venue=constants.VENUE,
        instrument_provider=provider,
    )
    asyncio.run(client._connect())
    assert [str(instrument.id) for instrument in seen] == ["AAPL.ALPACA"]


def test_an_unknown_adapter_field_is_refused() -> None:
    with pytest.raises(TypeError):
        _data_config(self_invented_field=1)


def test_the_live_endpoint_switch_cannot_be_built() -> None:
    """Paper is this adapter's only home; a live switch is not a config value."""
    with pytest.raises(TypeError, match="paper"):
        AlpacaDataClientConfig(
            api_key_env="ALPACA_API_KEY",
            api_secret_env="ALPACA_SECRET_KEY",  # noqa: S106 - a variable name, not a secret
            paper=False,
        )
    with pytest.raises(TypeError, match="paper"):
        AlpacaExecutionClientConfig(
            api_key_env="ALPACA_API_KEY",
            api_secret_env="ALPACA_SECRET_KEY",  # noqa: S106 - a variable name, not a secret
            account_id="ALPACA-002",
            paper=False,
        )


def test_the_key_names_and_the_account_identity_are_required() -> None:
    with pytest.raises(TypeError):
        AlpacaDataClientConfig()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        AlpacaDataClientConfig(api_key_env="ALPACA_API_KEY")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        AlpacaExecutionClientConfig(
            api_key_env="ALPACA_API_KEY",
            api_secret_env="ALPACA_SECRET_KEY",  # noqa: S106 - a variable name, not a secret
        )  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="ALPACA"):
        AlpacaExecutionClientConfig(
            api_key_env="ALPACA_API_KEY",
            api_secret_env="ALPACA_SECRET_KEY",  # noqa: S106 - a variable name, not a secret
            account_id="XNAS-002",
        )


def test_the_provider_loads_every_us_equity_including_untradable_ones() -> None:
    fake = FakeTradingClient(
        {"AAPL": _asset("AAPL"), "DEAD": _asset("DEAD", tradable=False)},
    )
    provider = _provider(fake, InstrumentProviderConfig(load_all=True))
    asyncio.run(provider.initialize())
    assert fake.calls == ["get_all_assets"]
    request = fake.bulk_requests[0]
    assert request.status is None
    assert request.asset_class is AssetClass.US_EQUITY
    assert [str(instrument.id) for instrument in provider.list_all()] == [
        "AAPL.ALPACA",
        "DEAD.ALPACA",
    ]


def test_the_equity_carries_the_vendor_asset_flags() -> None:
    equity = parse_equity(
        _asset(
            "AAPL",
            tradable=True,
            shortable=False,
            easy_to_borrow=False,
            marginable=True,
            fractionable=False,
        ),
        ts_init=11,
    )
    info = dict(equity.info)
    assert info["tradable"] is True
    assert info["shortable"] is False
    assert info["easy_to_borrow"] is False
    assert info["marginable"] is True
    assert info["fractionable"] is False


def test_the_tiered_tick_scheme_keeps_sub_penny_below_one_dollar() -> None:
    equity = parse_equity(_asset("PENNY", increment=0.0001), ts_init=11)
    assert equity.next_ask_price(0.5, 1) == Price.from_str("0.5001")
    assert equity.next_ask_price(1.23, 1) == Price.from_str("1.2400")
    assert equity.next_bid_price(1.23, 1) == Price.from_str("1.2200")
    assert equity.next_ask_price(250, 1) == Price.from_str("250.0100")


def test_one_unknown_exchange_does_not_fail_the_instrument_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK validates a typed asset list as a whole; raw rows parse one by one."""
    monkeypatch.setenv("SEXTANT_TEST_API_KEY", "dummy-key")
    monkeypatch.setenv("SEXTANT_TEST_SECRET", "dummy-secret")

    class RawTradingClient:
        def __init__(self, **kwargs: Any) -> None:
            self.raw_data = bool(kwargs.get("raw_data"))

        def get_all_assets(self, request: GetAssetsRequest) -> list[dict[str, Any]]:
            rows = [_raw_row("AAPL"), _raw_row("ODD", exchange="VENDOR_NEW_EXCHANGE")]
            if not self.raw_data:
                raise ValueError("VENDOR_NEW_EXCHANGE is not in the vendor exchange enum")
            return rows

    monkeypatch.setattr(factories, "TradingClient", RawTradingClient)
    config = AlpacaDataClientConfig(
        api_key_env="SEXTANT_TEST_API_KEY",
        api_secret_env="SEXTANT_TEST_SECRET",  # noqa: S106 - a variable name, not a secret
        instrument_provider=InstrumentProviderConfig(load_all=True),
    )
    client = factories.AlpacaDataClientFactory.create(
        name="ALPACA",
        config=config,
        cache=cast("Any", None),
        clock=cast("Any", None),
    )
    provider = client.instrument_provider
    assert isinstance(provider, providers.AlpacaInstrumentProvider)
    asyncio.run(provider.initialize())
    assert [str(instrument.id) for instrument in provider.list_all()] == [
        "AAPL.ALPACA",
        "ODD.ALPACA",
    ]


def test_one_unparseable_asset_row_does_not_fail_the_bulk_load() -> None:
    fake = FakeTradingClient(
        {"AAPL": _raw_row("AAPL"), "BROKEN": {"symbol": ""}},
        raw_data=True,
    )
    provider = _provider(fake, InstrumentProviderConfig(load_all=True))
    warnings: list[str] = []
    provider._log = cast("Any", SimpleNamespace(warning=warnings.append))
    asyncio.run(provider.initialize())
    assert [str(instrument.id) for instrument in provider.list_all()] == ["AAPL.ALPACA"]
    assert len(warnings) == 1
    assert "Skipped 1" in warnings[0]


def test_one_unparseable_asset_lookup_does_not_fail_the_rest() -> None:
    fake = FakeTradingClient(
        {"AAPL": _raw_row("AAPL"), "BROKEN": {"symbol": ""}},
        raw_data=True,
    )
    provider = _provider(
        fake,
        InstrumentProviderConfig(load_ids=["AAPL.ALPACA", "BROKEN.ALPACA"]),
    )
    warnings: list[str] = []
    provider._log = cast("Any", SimpleNamespace(warning=warnings.append))
    asyncio.run(provider.initialize())
    assert [str(instrument.id) for instrument in provider.list_all()] == ["AAPL.ALPACA"]
    assert len(warnings) == 1
    assert "Skipped 1" in warnings[0]


def test_a_cache_miss_loads_the_requested_equity_through_the_id_list_loader() -> None:
    """_stock() outside the startup set asks the single-instrument hook for it."""
    fake = FakeTradingClient({"AAPL": _asset("AAPL"), "MSFT": _asset("MSFT")})
    config = _data_config(
        instrument_provider=InstrumentProviderConfig(load_ids=["AAPL.ALPACA"]),
    )
    provider = _provider(fake, config.instrument_provider)
    seen: list[Any] = []

    class RecordingClient(AlpacaDataClient):
        def _handle_instrument(self, instrument: object) -> None:
            seen.append(instrument)

    client = RecordingClient(
        name="ALPACA",
        config=config,
        cache=cast("Any", None),
        clock=cast("Any", None),
        instrument_provider=provider,
        venue=constants.VENUE,
    )
    asyncio.run(provider.initialize())
    assert [str(instrument.id) for instrument in provider.list_all()] == ["AAPL.ALPACA"]
    stock = asyncio.run(client._stock(InstrumentId.from_str("MSFT.ALPACA")))
    assert stock.symbol == "MSFT"
    assert [str(instrument.id) for instrument in provider.list_all()] == [
        "AAPL.ALPACA",
        "MSFT.ALPACA",
    ]
    assert [str(instrument.id) for instrument in seen] == ["MSFT.ALPACA"]


def test_an_unlisted_equity_is_refused_by_the_single_instrument_loader() -> None:
    fake = FakeTradingClient({"AAPL": _asset("AAPL")})
    provider = _provider(fake, InstrumentProviderConfig(load_ids=["AAPL.ALPACA"]))
    asyncio.run(provider.initialize())
    with pytest.raises(ValueError, match="does not list an equity"):
        asyncio.run(provider.load_async(InstrumentId.from_str("MSFT.ALPACA")))
    assert provider.find(InstrumentId.from_str("MSFT.ALPACA")) is None


def _sdk(status: int) -> alpaca_client_module.TradingClient:
    """The real SDK client; only its HTTP session is faked. GONE gets ``status``."""
    sdk = alpaca_client_module.TradingClient("k", "s", paper=True, raw_data=True)

    def request(method: str, url: str, **opts: Any) -> requests.Response:
        symbol = url.rsplit("/", 1)[-1]
        if symbol == "GONE" and not status:
            raise requests.Timeout
        response = requests.Response()
        response.status_code = status if symbol == "GONE" else 200
        response._content = json.dumps(_raw_row(symbol)).encode()
        return response

    sdk._session.request = request  # type: ignore[method-assign]
    return sdk


def test_an_unlisted_id_fails_closed_and_the_rest_of_the_batch_loads() -> None:
    ids = ["GONE.ALPACA", "AAPL.ALPACA"]
    provider = _provider(_sdk(404), InstrumentProviderConfig(load_ids=ids))
    warnings: list[str] = []
    provider._log = cast("Any", SimpleNamespace(warning=warnings.append))
    asyncio.run(provider.initialize())
    assert [str(instrument.id) for instrument in provider.list_all()] == ["AAPL.ALPACA"]
    assert "Skipped 1 of 2" in warnings[0]
    with pytest.raises(ValueError, match="does not list an equity"):
        asyncio.run(provider.load_async(InstrumentId.from_str(ids[0])))


@pytest.mark.parametrize("status", [0, 401, 403, 500])
def test_an_sdk_failure_other_than_not_found_fails_the_load(status: int) -> None:
    ids = ["AAPL.ALPACA", "GONE.ALPACA"]
    provider = _provider(_sdk(status), InstrumentProviderConfig(load_ids=ids))
    with pytest.raises(requests.Timeout if not status else APIError):
        asyncio.run(provider.initialize())
