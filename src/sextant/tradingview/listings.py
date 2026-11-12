"""Stage NASDAQ Trader's two public symbol directory files for the display producer.

Source: the official directory definitions,
https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs, read 2026-11-11: "The symbol
directory files are updated periodically through-out each day" and "The last row of each
Symbol Directory text file contains a timestamp that reports the File Creation Time." The
download addresses are the manifest row "REST bar-fidelity display host through the ranking
publisher". The headers below are the ones observed on 2026-11-11; a changed header is a
changed schema and is refused until reviewed.

A file is replaced only after both downloads pass: the exact header, a field count per row
equal to the header's, and the File Creation Time trailer as the last line. A truncated
download or an HTML error page fails one of those, and the previous good files stay. No key
is used and nothing here calls TradingView or a broker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import urllib.request
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sextant.execution.state import write_atomic
from sextant.tradingview.bars import OTHER_LISTED_EXCHANGES

BASE = "https://www.nasdaqtrader.com/dynamic/SymDir/"
HEADERS = {
    "nasdaqlisted.txt": (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
        "Round Lot Size|ETF|NextShares"
    ),
    "otherlisted.txt": (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol"
    ),
}
# "File Creation Time followed by mmddyyyyhhmm as the first field, followed by all delimiters
# to round out the row. An example: File Creation Time: 1217200717:03|||||" (symboldirdefs).
TRAILER = re.compile(r"File Creation Time: (\d{10}:\d{2})\|*\Z")
RECEIPT = "listings-receipt.json"
DEFAULT_PARAMETERS = Path(__file__).resolve().parents[3] / "deploy/listings/parameters.json"

Fetch = Callable[[str, float], bytes]


class ListingError(ValueError):
    """A refused download; the previous good files are left in place."""


def fetch(url: str, timeout: float) -> bytes:
    """One plain HTTPS GET; a non-200 or non-text answer is refused."""
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 (fixed https)
        kind = response.headers.get_content_type()
        if response.status != 200 or kind != "text/plain":
            raise ListingError(f"{url} answered {response.status} {kind}")
        return response.read()


def validate(name: str, data: bytes) -> dict[str, Any]:
    """Check one directory file's shape and return its receipt; refuse anything else."""
    header = HEADERS[name]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ListingError(f"{name} is not UTF-8 text") from exc
    lines = text.splitlines()
    if not lines or lines[0] != header:
        raise ListingError(f"{name} does not start with the directory header")
    match = TRAILER.fullmatch(lines[-1])
    if match is None:
        raise ListingError(f"{name} does not end with the File Creation Time row (truncated?)")
    created = match.group(1)
    try:
        datetime.strptime(created, "%m%d%Y%H:%M")
    except ValueError as exc:
        raise ListingError(f"{name} has an invalid File Creation Time") from exc
    width = header.count("|")
    rows = lines[1:-1]
    if not rows or any(row.count("|") != width for row in rows):
        raise ListingError(f"{name} has no rows or a row with the wrong field count")
    unknown: Counter[str] = Counter()
    if "Exchange" in header.split("|"):
        column = header.split("|").index("Exchange")
        unknown.update(
            code
            for code in (row.split("|")[column] for row in rows)
            if code not in OTHER_LISTED_EXCHANGES
        )
    return {
        "file": name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "rows": len(rows),
        "file_creation_time": created,
        # Counted, never mapped: the parser skips these rows (bars.OTHER_LISTED_EXCHANGES).
        "unknown_exchange_rows": sum(unknown.values()),
        "unknown_exchange_codes": dict(sorted(unknown.items())),
    }


def creation_time(text: str) -> str:
    """The trailer's mmddyyyyHH:MM text, or "unknown"; the producer shows it, never refuses."""
    lines = text.splitlines()
    match = TRAILER.fullmatch(lines[-1]) if lines else None
    return match.group(1) if match else "unknown"


def _replace_bytes(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def stage(
    directory: Path,
    *,
    timeout: float,
    get: Fetch = fetch,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Download and validate both files, then replace each atomically and write a receipt."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    fetched: dict[str, bytes] = {}
    receipts = []
    for name in HEADERS:
        try:
            data = get(BASE + name, timeout)
        except ListingError:
            raise
        except Exception as exc:  # network, TLS, HTTP: the previous files stay
            raise ListingError(f"{name} download failed: {type(exc).__name__}") from exc
        receipts.append(validate(name, data))
        fetched[name] = data
    for name, data in fetched.items():
        _replace_bytes(directory / name, data)
    receipt = {
        "schema": "sextant.tv-listings-receipt/1",
        "staged_at": now().isoformat(),
        "source": BASE,
        "files": receipts,
    }
    write_atomic(directory / RECEIPT, receipt)
    return receipt


def load_timeout(path: Path = DEFAULT_PARAMETERS) -> float:
    row = json.loads(Path(path).read_text(encoding="utf-8"))["rows"]["fetch_timeout_s"]
    value = row.get("value")
    if type(value) is not int or not 1 <= value <= 600 or not str(row.get("source", "")).strip():
        raise ListingError("the fetch_timeout_s row has no valid value and source")
    return float(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sextant.tradingview.listings",
        description="Stage nasdaqlisted.txt and otherlisted.txt for the display producer.",
    )
    parser.add_argument("directory", type=Path)
    parser.add_argument("--parameters", type=Path, default=DEFAULT_PARAMETERS)
    args = parser.parse_args(argv)
    try:
        receipt = stage(args.directory, timeout=load_timeout(args.parameters))
    except (ListingError, OSError) as error:
        print(f"Listings not staged; the previous files stay: {error}")
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
