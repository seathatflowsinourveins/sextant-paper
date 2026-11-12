"""Commission for an Alpaca paper account's US-equity fills, read only from sourced rows.

No Alpaca fill carries a fee: alpaca-py@cc4cb3b7 ``alpaca/trading/models.py:633-646``
(``TradeUpdate``) and ``:168-251`` (``Order``) have no commission or fee field, and the
``trade_updates`` fill fields are ``timestamp``, ``price``, ``qty`` and ``position_qty``
(docs.alpaca.markets/us/docs/websocket-streaming, read 2026-11-11). The amount therefore
comes from two rows in ``lanes/paper/shared-node.params.json``, each quoting Alpaca's own
pages: the commission row and the paper regulatory-fee row. Both are scoped to paper
accounts and US equities; the live account's fees are not covered. There is no default:
a missing, duplicated, unsourced, mis-scoped or non-string row refuses (ADR-0018).
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from nautilus_trader.model import Currency, Money

COMMISSION_ROW = "paper_us_equity_commission_per_fill"
REGULATORY_ROW = "paper_us_equity_regulatory_fees_per_fill"
NAMES = (COMMISSION_ROW, REGULATORY_ROW)
UNIT = "USD per fill"
SCOPE = {"account_scope": "paper", "asset_class": "us_equity", "currency": "USD"}


def _iso_date(text: object) -> bool:
    try:
        return isinstance(text, str) and bool(date.fromisoformat(text))
    except ValueError:
        return False


def _row_value(row: Mapping[str, object]) -> Decimal:
    name = row["name"]
    source = row.get("source")
    if not isinstance(source, str) or "https://" not in source:
        raise ValueError(f"{name}: source with an official URL required")
    if any(row.get(field) != wanted for field, wanted in SCOPE.items()):
        raise ValueError(f"{name}: scope must be paper, us_equity, USD")
    if row.get("unit") != UNIT or row.get("calibration_state") != "UNCALIBRATED":
        raise ValueError(f"{name}: unit {UNIT!r} and UNCALIBRATED state required")
    if not _iso_date(row.get("last_change")):
        raise ValueError(f"{name}: last_change must be an ISO date")
    raw = row.get("value")
    if type(raw) is not str:
        raise TypeError(f"{name}: value must be a decimal string, never float or number")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{name}: value is not a decimal") from None
    precision = Currency.from_str("USD").precision
    if not value.is_finite() or value < 0 or value != round(value, precision):
        raise ValueError(f"{name}: value must be finite, non-negative and exact in cents")
    return value


def paper_commission_resolver(
    rows: Sequence[Mapping[str, object]],
) -> Callable[[Mapping[str, Any]], Money]:
    """Validate the fee rows once; return the execution client's ``commission_for_fill``."""
    found = [row for row in rows if row.get("name") in NAMES]
    names = [row["name"] for row in found]
    if len(set(names)) != len(names):
        raise ValueError("duplicate paper commission rows")
    if set(names) != set(NAMES):
        raise ValueError(f"missing paper commission rows: {sorted(set(NAMES) - set(names))}")
    total = sum((_row_value(row) for row in found), Decimal(0))
    # The stub types Money's amount as float; the string form keeps the Decimal exact.
    money = Money.from_str(f"{total:f} USD")

    def commission_for_fill(data: Mapping[str, Any]) -> Money:
        order = data.get("order")
        if not isinstance(order, Mapping) or order.get("asset_class") != "us_equity":
            raise ValueError("paper commission rows cover us_equity fills only")
        return money

    return commission_for_fill


def commission_for_fill(rows: Sequence[Mapping[str, object]], data: Mapping[str, Any]) -> Money:
    """One fill's commission plus paper regulatory fees, exactly as the rows state."""
    return paper_commission_resolver(rows)(data)
