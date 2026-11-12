"""Alpaca instrument provider: assets by id, or every US equity.

Follows nautilus_trader@d53849b ``examples/live/_template/providers.py``: network
work only in the async hooks, each SDK call via ``asyncio.to_thread``. Raw rows
(``raw_data=True``) parse one by one, since a typed list fails whole
(alpaca-py ``alpaca/trading/client.py:394-397``); a bad row is skipped and
counted. An id answered 404 (``alpaca/common/rest.py:198-207``) stays unloaded,
as in the engine's ``live/providers.py:97-108``; other SDK errors fail the load.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, cast, override

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass
from alpaca.trading.requests import GetAssetsRequest
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.live.providers import InstrumentProvider
from nautilus_trader.model import InstrumentId

from .parsing import parse_equity


class AlpacaInstrumentProvider(InstrumentProvider):
    """Load Alpaca equities; ``client_factory`` defers the client to first load."""

    def __init__(
        self,
        config: InstrumentProviderConfig | None = None,
        *,
        client_factory: Callable[[], TradingClient],
    ) -> None:
        super().__init__(config)
        self._client_factory = client_factory
        self._client: TradingClient | None = None

    def _trading_client(self) -> TradingClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    @override
    async def load_all_async(self, filters: dict | None = None) -> None:
        """Load every US-equity asset, tradable or not, from Alpaca."""
        client = self._trading_client()
        request = GetAssetsRequest(asset_class=AssetClass.US_EQUITY)
        rows = cast(
            "list[Any]",
            await asyncio.to_thread(client.get_all_assets, request),
        )
        ts_init = time.time_ns()
        skipped = 0
        for row in rows:
            try:
                self.add(parse_equity(row, ts_init=ts_init))
            except (AttributeError, KeyError, TypeError, ValueError):
                skipped += 1
        if skipped:
            self._log.warning(
                f"Skipped {skipped} of {len(rows)} Alpaca asset row(s) that "
                "could not be parsed (missing or invalid symbol/fields)",
            )

    @override
    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        """Load the named instruments through one asset lookup each."""
        client = self._trading_client()
        ts_init = time.time_ns()
        skipped = 0
        for instrument_id in instrument_ids:
            try:
                row = await asyncio.to_thread(
                    client.get_asset,
                    instrument_id.symbol.value,
                )
                self.add(parse_equity(row, ts_init=ts_init))
            except APIError as error:
                if error.status_code != 404:
                    raise
                skipped += 1
            except (AttributeError, KeyError, TypeError, ValueError):
                skipped += 1
        if skipped:
            self._log.warning(
                f"Skipped {skipped} of {len(instrument_ids)} Alpaca asset "
                "lookup(s) not listed (404) or not parsed",
            )

    @override
    async def load_async(
        self,
        instrument_id: InstrumentId,
        filters: dict | None = None,
    ) -> None:
        """Load one id via the list loader (engine ``providers.py:110``), or raise."""
        if self.find(instrument_id) is not None:
            return
        await self.load_ids_async([instrument_id], filters)
        if self.find(instrument_id) is None:
            raise ValueError(f"Alpaca does not list an equity for {instrument_id}")
