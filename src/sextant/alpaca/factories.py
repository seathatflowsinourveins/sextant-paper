"""Build Alpaca clients and the instrument provider from node configuration.

Follows nautilus_trader@d53849b ``examples/live/_template/factories.py``: each
``create`` returns a fresh client and opens no network resource; the data
client's ``TradingClient`` waits for the provider's first load. MARGIN and
NETTING follow the template and ADR-0008 B1. Always ``paper=True`` (alpaca-py
``alpaca/trading/client.py:80-82`` picks live only for ``paper=False``).
"""

from __future__ import annotations

from functools import partial
from typing import cast

from alpaca.trading.client import TradingClient
from nautilus_trader.live.clients import DataClientFactory, ExecutionClientFactory
from nautilus_trader.model import AccountId, AccountType, OmsType

from .commission import paper_commission_resolver
from .config import AlpacaDataClientConfig, AlpacaExecutionClientConfig, resolve_credential
from .constants import VENUE
from .data import AlpacaDataClient
from .execution import AlpacaExecutionClient
from .providers import AlpacaInstrumentProvider


def trading_client(
    config: AlpacaDataClientConfig | AlpacaExecutionClientConfig,
) -> TradingClient:
    """Build a ``TradingClient`` from the config's environment-variable names."""
    return TradingClient(
        api_key=resolve_credential(config.api_key_env),
        secret_key=resolve_credential(config.api_secret_env),
        paper=True,
        raw_data=True,
    )


class AlpacaDataClientFactory(DataClientFactory):
    """Construct the Alpaca market-data client and its instrument provider."""

    @staticmethod
    def create(*, name, config, cache, clock) -> AlpacaDataClient:
        """Construct a fresh data client; no network is opened here."""
        config = cast("AlpacaDataClientConfig", config)
        provider = AlpacaInstrumentProvider(
            config.instrument_provider,
            client_factory=partial(trading_client, config),
        )
        return AlpacaDataClient(
            name=name,
            config=config,
            cache=cache,
            clock=clock,
            venue=VENUE,
            instrument_provider=provider,
        )


class AlpacaExecutionClientFactory(ExecutionClientFactory):
    """Construct the Alpaca execution client from configuration."""

    @staticmethod
    def create(*, name, config, cache, clock, trader_id) -> AlpacaExecutionClient:
        """Construct a fresh execution client; no network is opened here."""
        config = cast("AlpacaExecutionClientConfig", config)
        client = AlpacaExecutionClient(
            name=name,
            config=config,
            cache=cache,
            clock=clock,
            trader_id=trader_id,
            venue=VENUE,
            account_id=AccountId.from_str(config.account_id),
            account_type=AccountType.MARGIN,
            oms_type=OmsType.NETTING,
        )
        # The sourced paper rows (#478) supply the fee hook; a refusal is kept for connect,
        # which fails before credentials or SDK construction (adapter guide, startup).
        try:
            client.commission_for_fill = paper_commission_resolver(config.commission_rows or [])
        except (TypeError, ValueError) as error:
            client.commission_refusal = str(error)
        return client
