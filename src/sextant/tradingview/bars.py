"""One-minute bar ingestion for the bar-fidelity display host.

The transport is Alpaca's SIP 1-minute bars through one paced client, which
waits on the shared Pacer before every HTTP page and retry;
the host turns each bar into one volume-bearing tick plus the vendor trade
count, never into one tick per print.  Nothing here orders, sizes or signs a
trade: the unit it feeds is display-only, restricted to paper slot three.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, cast

from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.models import BarSet
from alpaca.data.models.bars import Bar as AlpacaBar
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from sextant.data.alpaca_history import decimal_value, nanos, symbol_name

NS = 1_000_000_000
MINUTE_NS = 60 * NS
# The documented Basic IEX ceiling is 200 historical requests per minute and the
# lead's slot-3 allocation stays at or below it; one page covers one batch.
MAX_BATCH = 200
DIRECTORY_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,11}\Z")
NON_COMMON_NAME = re.compile(
    r"\b(?:warrant|warrants|right|rights|unit|units|preferred|preference|"
    r"depositary shares|notes? due|bond|debenture|%|fund|trust|etf|"
    r"exchange traded|closed[- ]end)\b",
    re.IGNORECASE,
)
NON_COMMON_SUFFIX = re.compile(r"-(?:P[A-Z]?|WT|W|UN|U|R|RT|CL|WS|PR|PA|PB|PC|PD|PE|PF|PG|PH|PI)\Z")
# otherlisted.txt "Exchange" codes as NASDAQ Trader defines them
# (https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs, read 2026-11-11). A row with
# any other code has no documented listing venue, so it is skipped, never guessed.
OTHER_LISTED_EXCHANGES = {
    "A": "NYSE MKT",
    "N": "New York Stock Exchange (NYSE)",
    "P": "NYSE ARCA",
    "Z": "BATS Global Markets (BATS)",
    "V": "Investors' Exchange, LLC (IEXG)",
}


@dataclass(frozen=True, slots=True)
class Bar:
    """One vendor 1-minute bar: prices, volume and the vendor trade count."""

    symbol: str
    start_ns: int
    open: Decimal
    close: Decimal
    volume: int
    trades: int
    vw: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("A bar needs a symbol")
        symbol_name(self.symbol)
        if type(self.start_ns) is not int or self.start_ns < 0 or self.start_ns % MINUTE_NS:
            raise ValueError("A bar starts on a whole minute")
        for name, value in (("open", self.open), ("close", self.close)):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"A bar needs a positive Decimal {name}")
        if type(self.volume) is not int or self.volume < 0:
            raise ValueError("A bar needs a non-negative integer volume")
        if type(self.trades) is not int or self.trades < 0:
            raise ValueError("A bar needs a non-negative integer trade count")
        if self.vw is not None:
            if not isinstance(self.vw, Decimal) or not self.vw.is_finite() or self.vw < 0:
                raise ValueError("A bar vw must be a non-negative Decimal or None")

    @property
    def close_ns(self) -> int:
        return self.start_ns + MINUTE_NS


@dataclass(frozen=True, slots=True)
class BarPoll:
    """One complete poll of the universe: what came back and what failed."""

    bars: tuple[Bar, ...]
    requested: tuple[str, ...]
    failed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.bars, tuple) or any(not isinstance(item, Bar) for item in self.bars):
            raise ValueError("A poll carries Bars")
        for names in (self.requested, self.failed):
            if not isinstance(names, tuple) or any(
                not isinstance(name, str) or not name for name in names
            ):
                raise ValueError("A poll carries symbol names")
        if len(set(self.requested)) != len(self.requested):
            raise ValueError("A poll requests each symbol once")
        if not set(self.failed) <= set(self.requested):
            raise ValueError("A failed symbol must have been requested")


@dataclass(frozen=True, slots=True)
class Listing:
    symbol: str
    name: str
    test_issue: bool
    etf: bool
    exchange: str | None = None


def parse_directory(text: str) -> tuple[Listing, ...]:
    """Parse a NASDAQ Trader directory file into its listing rows."""
    lines = [line for line in text.splitlines() if line.strip()]
    header: list[str] | None = None
    symbol_key = "Symbol"
    rows: list[Listing] = []
    for line in lines:
        fields = [field.strip() for field in line.split("|")]
        if header is None:
            if "ACT Symbol" in fields:
                header, symbol_key = fields, "ACT Symbol"
            elif "Symbol" in fields:
                header, symbol_key = fields, "Symbol"
            continue
        if "File Creation Time" in fields[0]:
            continue
        if len(fields) < len(header):
            raise ValueError("Malformed NASDAQ Trader directory row")
        rows.append(
            Listing(
                symbol=fields[header.index(symbol_key)],
                name=fields[header.index("Security Name")],
                test_issue=fields[header.index("Test Issue")] == "Y",
                etf=fields[header.index("ETF")] == "Y",
                exchange=fields[header.index("Exchange")] if "Exchange" in header else None,
            )
        )
    if header is None:
        raise ValueError("NASDAQ Trader directory has no header")
    return tuple(rows)


def _keeps(listing: Listing) -> bool:
    return (
        not listing.test_issue
        and not listing.etf
        and (listing.exchange is None or listing.exchange in OTHER_LISTED_EXCHANGES)
        and DIRECTORY_SYMBOL.fullmatch(listing.symbol) is not None
        and NON_COMMON_NAME.search(listing.name) is None
        and NON_COMMON_SUFFIX.search(listing.symbol) is None
    )


def build_universe(*directories: str, active: set[str] | None = None) -> tuple[str, ...]:
    """Union the operating-company listings, optionally restricted to ``active``."""
    if not directories:
        raise ValueError("A universe needs at least one NASDAQ Trader directory")
    symbols = {
        listing.symbol
        for text in directories
        for listing in parse_directory(text)
        if _keeps(listing)
    }
    if active is not None:
        symbols &= set(active)
    return tuple(sorted(symbols))


def _bar(symbol: str, row: AlpacaBar) -> Bar:
    trade_count = row.trade_count
    if trade_count is None:
        raise ValueError(f"Alpaca bar lacks the vendor trade count: {symbol}")
    return Bar(
        symbol=symbol,
        start_ns=nanos(row.timestamp),
        open=decimal_value(row.open),
        close=decimal_value(row.close),
        volume=int(row.volume),
        trades=int(trade_count),
        vw=decimal_value(row.vwap) if row.vwap is not None else None,
    )


class Waitable(Protocol):
    """The one pacer method the paced transport needs."""

    def wait(self) -> None: ...


class PacedStockBars(StockHistoricalDataClient):
    """The only stock-bars transport: every HTTP page and retry waits on one Pacer.

    alpaca-py pages inside ``RESTClient._get_marketdata`` (the documented
    ``page_token`` loop) and retries 429/504 inside ``RESTClient._request``;
    both paths call ``_one_request``, so this hook applies the shared allocation
    to every wire request, not once per logical batch (alpaca-py@cc4cb3b7
    ``alpaca/common/rest.py:90-141,360-410``, ``alpaca/data/historical/stock.py:75``).
    """

    def __init__(self, *, pacer: Waitable, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pacer = pacer

    def _one_request(self, method: str, url: str, opts: dict, retry: int) -> dict:
        self._pacer.wait()
        return super()._one_request(method, url, opts, retry)


class AlpacaBarSource:
    """SIP 1-minute bars through the paced transport, in batch-sized requests."""

    def __init__(
        self,
        client: PacedStockBars,
        *,
        batch: int = 150,
        feed: DataFeed = DataFeed.SIP,
    ) -> None:
        if not isinstance(client, PacedStockBars):
            raise ValueError("historical bars need the paced transport, PacedStockBars")
        if type(batch) is not int or not 1 <= batch <= MAX_BATCH:
            raise ValueError(f"Bar batches must cover 1..{MAX_BATCH} symbols")
        self.client = client
        self.batch = batch
        self.feed = feed

    def minute_bars(
        self, symbols: Sequence[str], *, start: datetime, end: datetime
    ) -> tuple[Bar, ...]:
        for name, value in (("start", start), ("end", end)):
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"Bar window {name} needs a timezone")
        if end <= start:
            raise ValueError("Bar window ends before it starts")
        names = tuple(sorted({symbol_name(symbol) for symbol in symbols}))
        if not names:
            return ()
        bars: list[Bar] = []
        for index in range(0, len(names), self.batch):
            chunk = names[index : index + self.batch]
            request = StockBarsRequest(
                symbol_or_symbols=list(chunk),
                timeframe=cast(TimeFrame, TimeFrame.Minute),
                start=start,
                end=end,
                feed=self.feed,
                adjustment=Adjustment.RAW,
                sort=Sort.ASC,
            )
            response = cast(BarSet, self.client.get_stock_bars(request))
            data: Mapping[str, Sequence[AlpacaBar]] | None = response.data
            if data is None:
                raise ValueError("Alpaca bar response has no data")
            for symbol, rows in data.items():
                for row in rows:
                    bars.append(_bar(str(symbol), row))
        return tuple(sorted(bars, key=lambda item: (item.close_ns, item.symbol)))
