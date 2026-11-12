"""Alpaca equity market data for the NautilusTrader rc8 Python adapter.

NautilusTrader d53849b: examples/live/_template/data.py, the Python adapter
guide and live/__init__.pyi. alpaca-py cc4cb3b: data/live/{stock,websocket}.py,
data/historical/stock.py and data/requests.py. Receipt and bar clocks follow
Sextant ADR-0013 and the former alpaca_feed.py thread boundary (1252eb9f).
Thread hand-off uses Python 3.13 asyncio's call_soon_threadsafe
(https://docs.python.org/3.13/library/asyncio-eventloop.html#asyncio.loop.call_soon_threadsafe,
read 2026-11-10).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from functools import partial
from threading import Thread
from typing import Protocol, cast
from weakref import ReferenceType, ref
from zoneinfo import ZoneInfo

from alpaca.common.enums import Sort
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.live.stock import StockDataStream
from alpaca.data.models import Bar as StockBar
from alpaca.data.models import BarSet, Quote, QuoteSet, Trade, TradeSet
from alpaca.data.models.trades import TradeCancel, TradeCorrection, TradingStatus
from alpaca.data.requests import StockBarsRequest, StockQuotesRequest, StockTradesRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from nautilus_trader.common import Clock, Logger
from nautilus_trader.config import DataClientConfig
from nautilus_trader.core import dt_to_unix_nanos
from nautilus_trader.live import (
    BarsResponse,
    ClientCache,
    InstrumentProvider,
    QuotesResponse,
    RequestBars,
    RequestQuotes,
    RequestTrades,
    SubscribeBars,
    SubscribeInstrumentStatus,
    SubscribeQuotes,
    SubscribeTrades,
    TradesResponse,
    UnsubscribeBars,
    UnsubscribeInstrumentStatus,
    UnsubscribeQuotes,
    UnsubscribeTrades,
)
from nautilus_trader.live.clients import MarketDataClient
from nautilus_trader.model import (
    AggressorSide,
    Bar,
    BarAggregation,
    BarType,
    CustomData,
    Equity,
    InstrumentId,
    InstrumentStatus,
    MarketStatusAction,
    Price,
    PriceType,
    Quantity,
    QuoteTick,
    TradeId,
    TradeTick,
    Venue,
)

from sextant.alpaca.stream_facts import (
    AlpacaStreamGap,
    facts_type,
    gap_type,
    trade_facts,
    vendor_trade_id,
)
from sextant.alpaca.trade_updates import _trade_update

_NY = ZoneInfo("America/New_York")
_STREAM_STOP_TIMEOUT_S = 5.0  # E4 dsh build 48cb220, shutdown bound.
_ROUND_LOT_SHARES = 100  # Alpaca's quote sizes and the incumbent SIP normalization.
type _Tick = QuoteTick | TradeTick | Bar | InstrumentStatus | CustomData
type _StreamOwner = tuple[asyncio.AbstractEventLoop, asyncio.Task[None]]
type _StockHandler = Callable[
    [Quote | Trade | StockBar | TradeCorrection | TradeCancel | dict], Awaitable[None]
]


class _DataSettings(Protocol):
    """The E2 data config fields consumed here."""

    api_key_env: str
    api_secret_env: str
    feed: str


class AlpacaStockDataStream(StockDataStream):
    """Run alpaca-py's handler mutations on the loop that sends subscriptions.

    The pinned synchronous helpers use run_coroutine_threadsafe().result().
    Here the mutations and the SDK's initial/reconnect send share a lock so a
    change cannot land between the send snapshot and the SDK setting _running.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._subscription_lock = asyncio.Lock()
        self.connects = 0
        self.on_reconnect: Callable[[int], None] | None = None

    async def _start_ws(self) -> None:
        # alpaca-py websocket.py:452-456 is the one call site, before the subscribe send and
        # before any consume; it reconnects with only a log line (:445-492). Every connect
        # after the first is a stream gap the node must see before any later row (N1e).
        await super()._start_ws()
        self.connects += 1
        if self.connects > 1:
            notice = self.on_reconnect
            if notice is None:
                raise RuntimeError("Alpaca stream reconnected with no gap notice wired")
            notice(self.connects)  # a raise here fails this connect; the SDK retries

    async def _dispatch(self, msg: dict) -> None:
        # alpaca-py cc4cb3b websocket.py:294-317,324-372 permits raw rows,
        # but its typed status cast happens before the handler's error boundary.
        # Keep status and amendment parsing inside our per-row boundary.
        # Correction/cancel handlers are SDK-local wildcards, never wire subscriptions.
        msg_type = msg.get("T")
        channel = (
            {"s": "statuses", "c": "corrections", "x": "cancelErrors"}.get(msg_type)
            if isinstance(msg_type, str)
            else None
        )
        if channel is not None:
            handlers = self._handlers[channel]
            symbol = msg.get("S")
            handler = handlers.get(symbol, handlers.get("*")) if isinstance(symbol, str) else None
            if handler is None and (not isinstance(symbol, str) or not symbol) and handlers:
                # No instrument can be named: a subscribed handler counts and
                # logs this malformed row, without publishing instrument data.
                handler = next(iter(handlers.values()))
            if handler is not None:
                await handler(msg)
            return
        await super()._dispatch(msg)

    async def _send_subscribe_msg(self) -> None:
        async with self._subscription_lock:
            await super()._send_subscribe_msg()

    async def register(
        self,
        channel: str,
        symbols: tuple[str, ...],
        handler: _StockHandler,
        updates: tuple[_StockHandler, _StockHandler] | None = None,
    ) -> None:
        async with self._subscription_lock:
            if updates is not None:
                # alpaca-py cc4cb3b stock.py:135-157; the SDK excludes these
                # callback registries from subscribe frames (websocket.py:389-401).
                self.register_trade_corrections(updates[0])
                self.register_trade_cancels(updates[1])
            self._handlers[channel].update(dict.fromkeys(symbols, handler))
            if self._running:
                await super()._send_subscribe_msg()

    async def unregister(self, channel: str, symbols: tuple[str, ...]) -> None:
        async with self._subscription_lock:
            if self._running:
                await self._send_unsubscribe_msg(channel, list(symbols))
            for symbol in symbols:
                del self._handlers[channel][symbol]


@dataclass(frozen=True)
class _Stock:
    id: InstrumentId
    symbol: str
    price_precision: int
    size_precision: int

    def price(self, value: object) -> Price:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        return Price.from_decimal_dp(number, self.price_precision)

    def quantity(self, value: object, multiplier: int = 1) -> Quantity:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number != number.to_integral_value():
            raise ValueError("Alpaca market data size must be a nonnegative whole count")
        return Quantity.from_decimal_dp(number * multiplier, self.size_precision)


@dataclass
class _StreamState:
    closing: bool = False
    failure: Exception | None = None


def _publish_on_owner(
    client_ref: ReferenceType[AlpacaDataClient],
    channel: str,
    symbol: str,
    key: InstrumentId | BarType | None,
    tick: _Tick | None,
    conversion_error: Exception | None,
    facts: CustomData | None = None,
) -> None:
    if (client := client_ref()) is not None:
        client._publish(channel, symbol, key, tick, conversion_error, facts)


def _notice_gap(
    owner: asyncio.AbstractEventLoop, client_ref: ReferenceType[AlpacaDataClient], connects: int
) -> None:
    # Called on the stream thread; FIFO with every later row's hand-off (call_soon_threadsafe).
    owner.call_soon_threadsafe(_gap_on_owner, client_ref, connects, time.time_ns())


def _gap_on_owner(client_ref: ReferenceType[AlpacaDataClient], connects: int, receipt: int) -> None:
    if (client := client_ref()) is not None:
        client._publish_gap(connects, receipt)


class AlpacaDataClient(MarketDataClient):
    """One SDK stream and historical client for one explicit IEX or SIP feed.

    The E2 factory supplies the provider and names-only config. SDK clients
    can also be supplied at construction for a socket-free sandbox.
    """

    def __init__(
        self,
        *,
        name: str,
        config: DataClientConfig,
        cache: ClientCache,
        clock: Clock,
        instrument_provider: InstrumentProvider | None = None,
        venue: Venue,
        stream: AlpacaStockDataStream | None = None,
        historical: StockHistoricalDataClient | None = None,
        feed: DataFeed | None = None,
    ) -> None:
        self._settings = cast(_DataSettings, config)
        selected = DataFeed(self._settings.feed) if feed is None else feed
        if selected not in (DataFeed.IEX, DataFeed.SIP):
            raise ValueError("alpaca-py StockDataStream supports IEX and SIP")
        super().__init__(
            name=name,
            config=config,
            cache=cache,
            clock=clock,
            venue=venue,
            instrument_provider=instrument_provider,
        )
        self._stream = stream
        self._historical = historical
        self._feed = selected
        self._log = Logger(type(self).__name__)
        self._active: dict[str, dict[str, InstrumentId | BarType]] = {
            "quotes": {},
            "trades": {},
            "bars": {},
            "statuses": {},
        }
        self._stream_ready: asyncio.Future[_StreamOwner] | None = None
        self._stream_task: asyncio.Task[object] | None = None
        self._stream_state = _StreamState()
        self._dropped_counts = dict.fromkeys(self._active, 0)
        self._unknown_status_count = 0
        self._dropped_counts.update(corrections=0, cancelErrors=0)
        self._update_counts: dict[str, int] = dict.fromkeys(("corrections", "cancelErrors"), 0)
        self._gap_count = 0

    @property
    def stream_gap_count(self) -> int:
        """Reconnects after the first connect, each published as ``AlpacaStreamGap``."""
        return self._gap_count

    def _publish_gap(self, connects: int, receipt: int) -> None:
        self._gap_count += 1
        self._log.warning(f"Alpaca stream reconnected (connect {connects}); published a gap")
        if not self._stream_state.closing:
            feed = self._feed.value
            self._handle_data(
                CustomData(gap_type(feed), AlpacaStreamGap(feed, connects, receipt, receipt))
            )

    @property
    def dropped_quote_count(self) -> int:
        """Rejected spreads from streaming and history, counted on the node loop."""
        return self._dropped_counts["quotes"]

    @property
    def dropped_trade_count(self) -> int:
        return self._dropped_counts["trades"]

    @property
    def dropped_bar_count(self) -> int:
        return self._dropped_counts["bars"]

    @property
    def dropped_status_count(self) -> int:
        return self._dropped_counts["statuses"]

    @property
    def unknown_status_count(self) -> int:
        return self._unknown_status_count

    @property
    def trade_correction_count(self) -> int:
        return self._update_counts["corrections"]

    @property
    def trade_cancel_count(self) -> int:
        return self._update_counts["cancelErrors"]

    @property
    def dropped_correction_count(self) -> int:
        return self._dropped_counts["corrections"]

    @property
    def dropped_cancel_count(self) -> int:
        return self._dropped_counts["cancelErrors"]

    async def _connect(self) -> None:
        if self.instrument_provider is None:
            raise RuntimeError("The Alpaca data client requires an instrument provider")
        await self.instrument_provider.initialize()
        for instrument in self.instrument_provider.list_all():
            self._handle_instrument(instrument)

    def _credentials(self) -> tuple[str, str]:
        try:
            return os.environ[self._settings.api_key_env], os.environ[self._settings.api_secret_env]
        except KeyError as error:
            raise ValueError(f"Missing Alpaca credential environment variable {error}") from None

    def _history_client(self) -> StockHistoricalDataClient:
        if self._historical is None:
            self._historical = StockHistoricalDataClient(*self._credentials())
        return self._historical

    async def _disconnect(self) -> None:
        self._stream_state.closing = True
        # The ready future supplies the actual loop before stop is attempted.
        try:
            async with asyncio.timeout(_STREAM_STOP_TIMEOUT_S):
                if self._stream_task is not None and self._stream_ready is not None:
                    if not self._stream_task.done():
                        loop, task = await asyncio.shield(self._stream_ready)
                        try:
                            loop.call_soon_threadsafe(task.cancel)
                        except RuntimeError:
                            if not loop.is_closed():
                                raise
                    await asyncio.shield(self._stream_task)
        finally:
            if self._stream_task is not None and self._stream_task.done():
                # Release exception tracebacks on the node loop after draining;
                # timed-out work stays tracked, as in rc8 live/src/task.rs.
                self._stream_task = None
                self._stream_ready = None
                self._stream_state.failure = None

    @staticmethod
    def _run_stream(
        stream: AlpacaStockDataStream,
        owner: asyncio.AbstractEventLoop,
        ready: asyncio.Future[_StreamOwner],
        state: _StreamState,
    ) -> None:
        async def run() -> None:
            task = asyncio.current_task()
            assert task is not None
            owner.call_soon_threadsafe(ready.set_result, (asyncio.get_running_loop(), task))
            try:
                # SDK run() wraps this coroutine in asyncio.run(). Retain
                # that boundary while making startup and shutdown cancellable.
                if not state.closing:
                    await stream._run_forever()
            except asyncio.CancelledError:
                pass
            except Exception as error:
                if state.failure is None:
                    state.failure = error
            finally:
                try:
                    await stream.close()
                except Exception as error:
                    if state.failure is None:
                        state.failure = error
                finally:
                    # rc8 AX data.rs:362-386 cancels, drains, then clears state.
                    for handlers in stream._handlers.values():
                        handlers.clear()

        try:
            asyncio.run(run())
        except Exception as error:
            if state.failure is None:
                state.failure = error

    async def _stream_worker(
        self,
        stream: AlpacaStockDataStream,
        owner: asyncio.AbstractEventLoop,
        ready: asyncio.Future[_StreamOwner],
    ) -> None:
        # Join before propagating failures: owner-side tracebacks capture self.
        thread = Thread(
            target=AlpacaDataClient._run_stream,
            args=(stream, owner, ready, self._stream_state),
            name="alpaca-stock-stream",
        )
        thread.start()
        await asyncio.to_thread(thread.join)
        if self._stream_state.failure is not None:
            raise self._stream_state.failure
        if not self._stream_state.closing:
            raise RuntimeError("Alpaca stock data stream exited")

    async def _change_subscription(
        self, stream: AlpacaStockDataStream, change: Callable[..., Awaitable[None]], *args: object
    ) -> None:
        if self._stream_task is None:
            owner = asyncio.get_running_loop()
            self._stream_ready = owner.create_future()
            self._stream_task = self.create_task(
                self._stream_worker(stream, owner, self._stream_ready), "alpaca-stock-stream"
            )
        assert self._stream_ready is not None
        loop, _ = await asyncio.shield(self._stream_ready)
        if self._stream_task.done():
            raise RuntimeError("Alpaca stock data stream is unavailable")

        async def apply() -> None:
            await change(*args)

        operation = apply()
        try:
            completion = asyncio.run_coroutine_threadsafe(operation, loop)
        except RuntimeError:
            operation.close()
            raise
        result = asyncio.wrap_future(completion)
        try:
            await asyncio.wait((result, self._stream_task), return_when=asyncio.FIRST_COMPLETED)
            if not result.done():
                raise RuntimeError("Alpaca stock data stream exited during subscription change")
            await result
        finally:
            if not result.done():
                result.cancel()

    async def _stock(self, instrument_id: InstrumentId) -> _Stock:
        provider = self.instrument_provider
        if provider is None or instrument_id.venue != self.venue:
            raise ValueError(f"Alpaca equity unavailable: {instrument_id}")
        instrument = provider.find(instrument_id)
        if instrument is None:
            await provider.load_async(instrument_id)
            instrument = provider.find(instrument_id)
            if isinstance(instrument, Equity):
                self._handle_instrument(instrument)
        if not isinstance(instrument, Equity):
            raise ValueError(f"Alpaca equity unavailable: {instrument_id}")
        symbol = str(instrument.raw_symbol)
        if "*" in symbol:
            raise ValueError("Alpaca data subscriptions and requests require named symbols")
        return _Stock(instrument.id, symbol, instrument.price_precision, instrument.size_precision)

    async def _subscribe_quotes(self, command: SubscribeQuotes) -> None:
        await self._subscribe_channel("quotes", command.instrument_ids)

    async def _subscribe_trades(self, command: SubscribeTrades) -> None:
        await self._subscribe_channel("trades", command.instrument_ids)

    async def _subscribe_instrument_status(self, command: SubscribeInstrumentStatus) -> None:
        # NautilusTrader d53849b live/clients.py:267-268 and live/__init__.pyi:1794-1808.
        await self._subscribe_channel("statuses", command.instrument_ids)

    async def _subscribe_bars(self, command: SubscribeBars) -> None:
        for bar_type in command.bar_types:
            _bar_timeframe(bar_type, streaming=True)
        await self._subscribe_channel("bars", command.bar_types)

    async def _subscribe_channel(
        self, channel: str, keys: list[InstrumentId] | list[BarType]
    ) -> None:
        owner = asyncio.get_running_loop()
        active = self._active[channel]
        stocks: dict[str, tuple[_Stock, InstrumentId | BarType]] = {}
        for key in keys:
            try:
                stock = await self._stock(key.instrument_id if isinstance(key, BarType) else key)
                if stock.symbol in active:
                    if active[stock.symbol] != key:
                        raise ValueError("Alpaca symbol already has a different subscription")
                    continue
                stocks[stock.symbol] = (stock, key)
            except (ValueError, TypeError) as error:
                self._log.error(f"Alpaca {channel} subscription skipped for {key}: {error}")
        if not stocks:
            return

        if self._stream is None:
            self._stream = AlpacaStockDataStream(*self._credentials(), feed=self._feed)
        stream = self._stream
        client_ref: ReferenceType[AlpacaDataClient] = ref(self)
        stream.on_reconnect = partial(_notice_gap, owner, client_ref)
        state = self._stream_state

        async def receive(
            row: Quote | Trade | StockBar | TradeCorrection | TradeCancel | dict,
        ) -> None:
            if state.failure is not None:
                return
            receipt = time.time_ns()
            tick: _Tick | None = None
            conversion_error: Exception | None = None
            facts: CustomData | None = None
            try:
                if channel == "statuses":
                    # SDK raw status dictionaries reach this boundary before
                    # model validation, including a missing symbol or timestamp.
                    try:
                        if not isinstance(row, dict):
                            raise TypeError("Expected raw Alpaca trading status")
                        stock, key = stocks[row["S"]]
                        tick = _instrument_status(row, stock, receipt)
                    except Exception as error:
                        owner.call_soon_threadsafe(
                            _publish_on_owner, client_ref, channel, "", None, None, error
                        )
                        return
                    owner.call_soon_threadsafe(
                        _publish_on_owner, client_ref, channel, stock.symbol, key, tick, None
                    )
                    return
                if isinstance(row, dict):
                    raise TypeError("Unexpected raw Alpaca market data")
                stock, key = stocks[row.symbol]
                if channel == "quotes" and isinstance(row, Quote):
                    try:
                        tick = _quote_tick(row, stock, receipt)
                    except Exception as error:
                        conversion_error = error
                elif channel == "trades" and isinstance(row, Trade):
                    # rc8 Architect AX handles trade and candle parse errors per row.
                    try:
                        tick = _trade_tick(row, stock, receipt)
                    except Exception as error:
                        conversion_error = error
                    if isinstance(tick, TradeTick):  # its conditions, published just before it
                        try:
                            facts = CustomData(facts_type(feed), trade_facts(row, tick))
                        except Exception:
                            facts = None  # the Actor then reads unknown facts: nothing moves
                elif isinstance(key, BarType) and isinstance(row, StockBar):
                    try:
                        tick = _bar(row, stock, key, receipt)
                    except Exception as error:
                        conversion_error = error
                else:
                    raise TypeError("Unexpected Alpaca market data model")
            except Exception as error:
                state.failure = error
                await stream.stop_ws()
                return
            owner.call_soon_threadsafe(
                _publish_on_owner,
                client_ref,
                channel,
                stock.symbol,
                key,
                tick,
                conversion_error,
                facts,
            )

        venue, feed = self.venue, self._feed.value
        assert venue is not None

        async def receive_update(
            update_channel: str,
            row: Quote | Trade | StockBar | TradeCorrection | TradeCancel | dict,
        ) -> None:
            if state.failure is not None:
                return
            try:
                if not isinstance(row, dict):
                    raise TypeError("Expected raw Alpaca trade update")
                envelope = _trade_update(row, venue, feed, time.time_ns())
            except Exception as error:
                owner.call_soon_threadsafe(
                    _publish_on_owner, client_ref, update_channel, "", None, None, error
                )
                return
            owner.call_soon_threadsafe(
                _publish_on_owner,
                client_ref,
                update_channel,
                envelope.data.symbol,
                envelope.data.instrument_id,
                envelope,
                None,
            )

        # alpaca-py installs these handlers before its wire send and retains
        # them for reconnect. Accept the earliest queued tick from each handler.
        active.update({symbol: key for symbol, (_, key) in stocks.items()})
        updates = (
            (partial(receive_update, "corrections"), partial(receive_update, "cancelErrors"))
            if channel == "trades"
            else None
        )
        if updates is None:
            await self._change_subscription(
                stream, stream.register, channel, tuple(stocks), receive
            )
        else:
            await self._change_subscription(
                stream, stream.register, channel, tuple(stocks), receive, updates
            )

    def _publish(
        self,
        channel: str,
        symbol: str,
        key: InstrumentId | BarType | None,
        tick: _Tick | None,
        conversion_error: Exception | None = None,
        facts: CustomData | None = None,
    ) -> None:
        if tick is None:
            self._dropped_counts[channel] += 1
            if channel in ("statuses", "corrections", "cancelErrors"):
                self._log.error(f"Dropped Alpaca {channel} row: {type(conversion_error).__name__}")
            elif conversion_error is not None:
                self._log.error(f"Dropped Alpaca {channel} row for {symbol}: {conversion_error}")
        elif (
            not self._stream_state.closing
            and self._active["trades" if isinstance(tick, CustomData) else channel].get(symbol)
            == key
        ):
            if isinstance(tick, CustomData):
                self._update_counts[channel] += 1
            if isinstance(tick, InstrumentStatus) and tick.action == MarketStatusAction.NONE:
                self._unknown_status_count += 1
                self._log.error("Unknown Alpaca status code; published unknown instrument status")
            if facts is not None:
                self._handle_data(facts)  # before its tick, on the same ordered output
            self._handle_data(tick)

    async def _unsubscribe_quotes(self, command: UnsubscribeQuotes) -> None:
        await self._unsubscribe_channel("quotes", command.instrument_ids)

    async def _unsubscribe_trades(self, command: UnsubscribeTrades) -> None:
        await self._unsubscribe_channel("trades", command.instrument_ids)

    async def _unsubscribe_instrument_status(self, command: UnsubscribeInstrumentStatus) -> None:
        # NautilusTrader d53849b live/clients.py:309-313.
        await self._unsubscribe_channel("statuses", command.instrument_ids)

    async def _unsubscribe_bars(self, command: UnsubscribeBars) -> None:
        await self._unsubscribe_channel("bars", command.bar_types)

    async def _unsubscribe_channel(
        self, channel: str, keys: list[InstrumentId] | list[BarType]
    ) -> None:
        active = self._active[channel]
        symbols = [symbol for symbol, key in active.items() if key in keys]
        if symbols:
            stream = self._stream
            assert stream is not None
            await self._change_subscription(stream, stream.unregister, channel, tuple(symbols))
            for symbol in symbols:
                del active[symbol]

    async def _request_quotes(self, request: RequestQuotes) -> None:
        stocks = [await self._stock(instrument_id) for instrument_id in request.instrument_ids]
        params = StockQuotesRequest(
            symbol_or_symbols=[stock.symbol for stock in stocks],
            start=request.start,
            end=request.end,
            limit=request.limit,
            feed=self._feed,
        )
        result = await asyncio.to_thread(self._history_client().get_stock_quotes, params)
        if not isinstance(result, QuoteSet):
            raise TypeError("Alpaca quote history must return an SDK QuoteSet")
        ticks: list[QuoteTick] = []
        for stock in stocks:
            for row in result.data.get(stock.symbol, ()):
                tick = _quote_tick(row, stock)
                if tick is None:
                    self._dropped_counts["quotes"] += 1
                else:
                    ticks.append(tick)
        self._handle_response(
            QuotesResponse(
                self.client_id,
                request.instrument_ids,
                sorted(ticks, key=lambda tick: tick.ts_event),
                request.request_id,
                self.clock.timestamp_ns(),
                request.start_ns,
                request.end_ns,
                request.params,
            )
        )

    async def _request_trades(self, request: RequestTrades) -> None:
        stocks = [await self._stock(instrument_id) for instrument_id in request.instrument_ids]
        # A limit keeps the window's latest trades, as the official IB adapter does (rc8
        # crates/adapters/interactive_brokers/src/data/core.rs:2056-2061); alpaca-py sorts
        # ascending unless asked (cc4cb3b7 data/requests.py:32,40), so a limit of 1 would be
        # the window's first trade. The response is sorted ascending below.
        params = StockTradesRequest(
            symbol_or_symbols=[stock.symbol for stock in stocks],
            start=request.start,
            end=request.end,
            limit=request.limit,
            feed=self._feed,
            sort=None if request.limit is None else Sort.DESC,
        )
        result = await asyncio.to_thread(self._history_client().get_stock_trades, params)
        if not isinstance(result, TradeSet):
            raise TypeError("Alpaca trade history must return an SDK TradeSet")
        ticks = [
            _trade_tick(row, stock) for stock in stocks for row in result.data.get(stock.symbol, ())
        ]
        self._handle_response(
            TradesResponse(
                self.client_id,
                request.instrument_ids,
                sorted(ticks, key=lambda tick: tick.ts_event),
                request.request_id,
                self.clock.timestamp_ns(),
                request.start_ns,
                request.end_ns,
                request.params,
            )
        )

    async def _request_bars(self, request: RequestBars) -> None:
        # rc8 validates a single historical bar type before dispatch.
        bar_type = request.bar_type
        timeframe = _bar_timeframe(bar_type)
        stock = await self._stock(bar_type.instrument_id)
        start, end = request.start, request.end or self.clock.utc_now()
        # Alpaca filters opening timestamps. The engine window names the
        # closing timestamps we emit under ADR-0013.
        if timeframe.unit == TimeFrameUnit.Day:
            start = start.astimezone(_NY) - timedelta(days=1) if start is not None else None
            end = end.astimezone(_NY) - timedelta(days=1) if end is not None else None
        else:
            start = start - bar_type.spec.timedelta if start is not None else None
            end = end - bar_type.spec.timedelta if end is not None else None
        params = StockBarsRequest(
            symbol_or_symbols=stock.symbol,
            start=start,
            end=end,
            timeframe=timeframe,
            limit=request.limit,
            feed=self._feed,
        )
        result = await asyncio.to_thread(self._history_client().get_stock_bars, params)
        if not isinstance(result, BarSet):
            raise TypeError("Alpaca bar history must return an SDK BarSet")
        bars = [_bar(row, stock, bar_type) for row in result.data.get(stock.symbol, ())]
        self._handle_response(
            BarsResponse(
                self.client_id,
                bar_type,
                bars,
                request.request_id,
                self.clock.timestamp_ns(),
                request.start_ns,
                request.end_ns,
                request.params,
            )
        )


def _bar_timeframe(bar_type: BarType, *, streaming: bool = False) -> TimeFrame:
    spec = bar_type.spec
    if (
        bar_type.is_standard()
        and bar_type.is_externally_aggregated()
        and spec.price_type == PriceType.LAST
        and spec.step == 1
    ):
        if spec.aggregation == BarAggregation.MINUTE:
            return cast(TimeFrame, TimeFrame.Minute)
        if not streaming and spec.aggregation == BarAggregation.DAY:
            return cast(TimeFrame, TimeFrame.Day)
    raise NotImplementedError(
        f"Unsupported Alpaca {'streaming' if streaming else 'historical'} bar: {bar_type}"
    )


def _quote_tick(row: Quote, stock: _Stock, receipt: int | None = None) -> QuoteTick | None:
    bid_raw, ask_raw = Decimal(str(row.bid_price)), Decimal(str(row.ask_price))
    if not bid_raw.is_finite() or not ask_raw.is_finite() or not 0 < bid_raw < ask_raw:
        return None
    bid, ask = stock.price(bid_raw), stock.price(ask_raw)
    if not 0 < bid.as_decimal() < ask.as_decimal():
        return None
    event = dt_to_unix_nanos(row.timestamp)
    return QuoteTick(
        stock.id,
        bid,
        ask,
        stock.quantity(row.bid_size, _ROUND_LOT_SHARES),
        stock.quantity(row.ask_size, _ROUND_LOT_SHARES),
        event,
        event if receipt is None else receipt,
    )


def _trade_tick(row: Trade, stock: _Stock, receipt: int | None = None) -> TradeTick:
    if row.id is None:
        raise ValueError("Alpaca trade is missing its vendor ID")
    event = dt_to_unix_nanos(row.timestamp)
    return TradeTick(
        stock.id,
        stock.price(row.price),
        stock.quantity(row.size),
        AggressorSide.NO_AGGRESSOR,
        TradeId(vendor_trade_id(row.exchange, row.id)),
        event,
        event if receipt is None else receipt,
    )


def _bar(row: StockBar, stock: _Stock, bar_type: BarType, receipt: int | None = None) -> Bar:
    if bar_type.spec.aggregation == BarAggregation.DAY:
        local = row.timestamp.astimezone(_NY)
        close = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    else:
        close = row.timestamp + bar_type.spec.timedelta
    event = dt_to_unix_nanos(close)
    return Bar(
        bar_type,
        stock.price(row.open),
        stock.price(row.high),
        stock.price(row.low),
        stock.price(row.close),
        stock.quantity(row.volume),
        event,
        event if receipt is None else receipt,
    )


def _instrument_status(raw: dict, stock: _Stock, receipt: int) -> InstrumentStatus:
    """Alpaca's tape-scoped status codes; never infer trading from free text.

    alpaca-py cc4cb3b models/trades.py:82-117; official real-time stock
    pricing data, Trading Status tables (read 2026-11-10, cached offline).
    NautilusTrader d53849b model/__init__.pyi:2408-2421,8035-8052 and
    docs/concepts/data/instrument_status.md:24-29 preserve unknown booleans.
    """
    # Match SDK websocket.py:294-317's wire timestamp and typed model cast,
    # inside the row boundary and without mutating a vendor dictionary.
    row = TradingStatus(raw["S"], {**raw, "t": raw["t"].to_datetime()})
    if row.timestamp.tzinfo is None or row.timestamp.utcoffset() is None:
        raise ValueError("Trading status timestamp requires a timezone")
    actions = {
        "A": {
            "2": MarketStatusAction.HALT,
            "3": MarketStatusAction.TRADING,
            "5": MarketStatusAction.NEW_PRICE_INDICATION,
            "E": MarketStatusAction.SHORT_SELL_RESTRICTION_CHANGE,
        },
        "C": {
            "H": MarketStatusAction.HALT,
            "T": MarketStatusAction.TRADING,
            "Q": MarketStatusAction.QUOTING,
            "P": MarketStatusAction.PAUSE,
        },
    }
    tape = {"B": "A", "O": "C"}.get(row.tape, row.tape)
    action = actions.get(tape, {}).get(row.status_code, MarketStatusAction.NONE)
    trading = None
    if action == MarketStatusAction.TRADING:
        trading = True
    elif action in (MarketStatusAction.HALT, MarketStatusAction.PAUSE, MarketStatusAction.QUOTING):
        trading = False
    return InstrumentStatus(
        stock.id,
        action,
        dt_to_unix_nanos(row.timestamp),
        receipt,
        reason=f"{row.reason_code}: {row.reason_message}",
        trading_event="unknown" if action == MarketStatusAction.NONE else row.status_message,
        is_trading=trading,
        is_quoting=True if action == MarketStatusAction.QUOTING else None,
    )
