# Carried from seathatflowsinourveins/northstar-trading@8256c16c31dcb8e53fc726e0c1c77dc2c8d0d4fa:
# src/northstar_trading/data/alpaca_history.py, verbatim (ADR-0013).
"""Use Alpaca's pagination/retries; retain the original HTTP response texts."""

import json
import os
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from datetime import time as daytime
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.models import BarSet, TradeSet
from alpaca.data.requests import StockBarsRequest, StockTradesRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from requests import Response

NY = ZoneInfo("America/New_York")
BARS = ("1min", "1day")
RawBar = dict[str, Any]


def symbol_name(symbol: str) -> str:
    """Keep vendor tickers intact, but never interpret a ticker as a path."""
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,31}", symbol):
        raise ValueError(f"Invalid equity symbol: {symbol!r}")
    return symbol


def timestamp(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError("Bar timestamps must carry a timezone")
    return result.astimezone(UTC)


def decimal_value(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def day_start(day: date) -> datetime:
    return datetime.combine(day, daytime.min, NY)


def nanos(value: datetime) -> int:
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def publish_once(path: Path, write: Callable[[Path], object]) -> bool:
    """Publish a completed local file exclusively; a crash cannot land a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return False
    with NamedTemporaryFile(dir=path.parent, prefix=".landing-", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        write(temporary)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def raw_path(root: Path, bar: str, symbol: str, day: date) -> Path:
    if bar not in BARS:
        raise ValueError(f"Unsupported bar interval: {bar}")
    return root / "alpaca_sip" / bar / symbol_name(symbol) / f"{day.isoformat()}.parquet"


def trade_path(root: Path, symbol: str, day: date) -> Path:
    """The raw trade receipt partitions by symbol and New York session, like bars."""
    return root / "alpaca_sip" / "trades" / symbol_name(symbol) / f"{day.isoformat()}.parquet"


def _land_pages(
    path: Path,
    pages: Sequence[str],
    *,
    metadata: dict[str, str],
    validate: Callable[[str], object],
) -> bool:
    if path.exists():
        return False
    if not pages:
        raise ValueError("Cannot land a request without a response")
    for page in pages:
        validate(page)
    table = pa.table({"page": list(range(len(pages))), "response_json": list(pages)})
    table = table.replace_schema_metadata(
        {key.encode(): value.encode() for key, value in metadata.items()}
    )
    return publish_once(path, lambda target: pq.write_table(table, target))


def land_raw(path: Path, pages: Sequence[str], *, symbol: str, day: date, bar: str) -> bool:
    """Parquet is an envelope, not a re-encoding of Alpaca prices or timestamps.

    Each row retains one complete, unchanged HTTP response body, including tokens,
    unknown fields, duplicates and source order. No float-valued response model is
    serialized back to disk. Nautilus schemas are used only in catalog.py.
    """
    metadata = {
        "source": "alpaca_sip",
        "feed": "sip",
        "adjustment": "raw",
        "symbol": symbol_name(symbol),
        "date": day.isoformat(),
        "asof": day.isoformat(),
        "bar": bar,
        "window": "America/New_York calendar day",
    }
    return _land_pages(
        path,
        pages,
        metadata=metadata,
        validate=lambda page: BarSet(json.loads(page)["bars"] or {}),
    )


def _validate_trade_page(page: str) -> None:
    TradeSet(json.loads(page)["trades"] or {})


def land_trades(path: Path, pages: Sequence[str], *, symbol: str, day: date) -> bool:
    """Keep each complete trade page; A7's raw fields are never re-encoded.

    ``corrections`` and ``cancelErrors`` are WebSocket messages, not historical
    pages, so a later revision needs its own receipt rather than an overwrite.
    """
    metadata = {
        "source": "alpaca_sip",
        "feed": "sip",
        "kind": "trades",
        "symbol": symbol_name(symbol),
        "date": day.isoformat(),
        "asof": day.isoformat(),
        "window": "America/New_York calendar day",
    }
    return _land_pages(path, pages, metadata=metadata, validate=_validate_trade_page)


def read_raw(path: Path) -> list[RawBar]:
    table = pq.read_table(path)
    meta = table.schema.metadata or {}
    if meta.get(b"adjustment") != b"raw" or meta.get(b"feed") != b"sip":
        raise ValueError(f"Not unadjusted SIP data: {path}")
    symbol = meta[b"symbol"].decode()
    day = date.fromisoformat(meta[b"date"].decode())
    if meta.get(b"asof") != day.isoformat().encode():
        raise ValueError(f"Raw archive lacks matching session asof; re-ingest separately: {path}")
    rows: list[RawBar] = []
    for text in table["response_json"].to_pylist():
        response = json.loads(text, parse_float=Decimal)
        for row in (response["bars"] or {}).get(symbol, []):
            if timestamp(row["t"]).astimezone(NY).date() != day:
                raise ValueError(f"Response escaped its source-day partition: {path}")
            rows.append(row)
    return rows


def read_trades(path: Path) -> list[dict[str, Any]]:
    """Read the vendor trade records back; prices are Decimal, timestamps stay text."""
    table = pq.read_table(path)
    meta = table.schema.metadata or {}
    if meta.get(b"kind") != b"trades" or meta.get(b"feed") != b"sip":
        raise ValueError(f"Not SIP trades: {path}")
    symbol = meta[b"symbol"].decode()
    day = date.fromisoformat(meta[b"date"].decode())
    if meta.get(b"asof") != day.isoformat().encode():
        raise ValueError(f"Raw archive lacks matching session asof; re-ingest separately: {path}")
    rows: list[dict[str, Any]] = []
    for text in table["response_json"].to_pylist():
        response = json.loads(text, parse_float=Decimal)
        for row in (response["trades"] or {}).get(symbol, []):
            if timestamp(row["t"]).astimezone(NY).date() != day:
                raise ValueError(f"Response escaped its source-day partition: {path}")
            rows.append(row)
    return rows


class Pacer:
    """A conservative minimum interval, applied to *each* HTTP page and retry."""

    def __init__(
        self,
        calls_per_minute: Decimal = Decimal("10000"),
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not calls_per_minute.is_finite() or not 0 < calls_per_minute <= 10000:
            raise ValueError("Data calls per minute must be positive and at most 10000")
        self.interval = float(Decimal(60) / calls_per_minute)
        self.clock = clock
        self.sleep = sleep
        self.last: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self.last is not None:
            delay = self.interval - (now - self.last)
            if delay > 0:
                self.sleep(delay)
        self.last = self.clock()


class AlpacaHistory(StockHistoricalDataClient):
    """Two small transport hooks; upstream owns pagination and 429 retries."""

    def __init__(self, calls_per_minute: Decimal = Decimal("10000")) -> None:
        key, secret = environment_credentials()
        super().__init__(api_key=key, secret_key=secret, raw_data=True)
        self.pacer = Pacer(calls_per_minute)
        self.pages: list[str] = []
        self._session.hooks["response"].append(self._capture)

    def _capture(self, response: Response, **kwargs: Any) -> None:
        if response.status_code == 200:
            self.pages.append(response.text)

    def _one_request(self, method: str, url: str, opts: dict, retry: int) -> dict:
        self.pacer.wait()
        return super()._one_request(method, url, opts, retry)

    def fetch(self, symbol: str, day: date, bar: str) -> tuple[str, ...]:
        if bar not in BARS:
            raise ValueError(f"Unsupported bar interval: {bar}")
        request = StockBarsRequest(
            symbol_or_symbols=symbol_name(symbol),
            timeframe=TimeFrame(1, TimeFrameUnit("Min" if bar == "1min" else "Day")),
            start=day_start(day),
            end=day_start(day + timedelta(days=1)) - timedelta(microseconds=1),
            feed=DataFeed.SIP,
            adjustment=Adjustment.RAW,
            sort=Sort.ASC,
            asof=day.isoformat(),
        )
        self.pages = []
        self.get_stock_bars(request)
        return tuple(self.pages)

    def fetch_trades(self, symbol: str, day: date) -> tuple[str, ...]:
        """Fetch every complete page of one New York session's SIP trades."""
        request = StockTradesRequest(
            symbol_or_symbols=symbol_name(symbol),
            start=day_start(day),
            end=day_start(day + timedelta(days=1)) - timedelta(microseconds=1),
            feed=DataFeed.SIP,
            sort=Sort.ASC,
            asof=day.isoformat(),
        )
        self.pages = []
        self.get_stock_trades(request)
        return tuple(self.pages)

    def close(self) -> None:
        self._session.close()


def environment_credentials() -> tuple[str, str]:
    # alpaca-py 0.44.0 does not actually load env in _validate_credentials.
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise ValueError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY through with-keys.sh")
    return key, secret
