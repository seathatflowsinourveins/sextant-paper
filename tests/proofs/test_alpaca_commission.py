"""Paper US-equity fill commission comes only from sourced registry rows.

Sources read 2026-11-11: Alpaca Brokerage Fee Schedule (BrokFeeSched.pdf, revised
2026-10-12), alpaca.markets/algotrading, docs.alpaca.markets paper-trading and
regulatory-fees pages; alpaca-py@cc4cb3b7 alpaca/trading/models.py:633-646 (a trade
update carries no fee field). Fill payloads follow the raw ``trade_updates`` shape the
execution client hands its ``commission_for_fill`` hook.
"""

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
from nautilus_trader.model import Currency, Money

from sextant.alpaca.commission import (
    COMMISSION_ROW,
    REGULATORY_ROW,
    commission_for_fill,
    paper_commission_resolver,
)

ROOT = Path(__file__).resolve().parents[2]
PARAMS = ROOT / "lanes/paper/shared-node.params.json"


def rows() -> list[dict]:
    return json.loads(PARAMS.read_text())


def fee_rows() -> list[dict]:
    return [row for row in rows() if row["name"] in (COMMISSION_ROW, REGULATORY_ROW)]


def fill(asset_class: object = "us_equity") -> dict:
    order: dict[str, object] = {"id": "venue-1", "symbol": "TEST", "side": "sell", "qty": "10"}
    if asset_class is not None:
        order["asset_class"] = asset_class
    return {"event": "fill", "price": "10.00", "qty": "10", "order": order}


def changed(name: str, **fields: object) -> list[dict]:
    edited = copy.deepcopy(fee_rows())
    for row in edited:
        if row["name"] == name:
            row.update(fields)
    return edited


def test_the_rows_file_validates_and_states_paper_scope_with_quoted_sources():
    assert {row["name"] for row in rows()} >= {COMMISSION_ROW, REGULATORY_ROW}
    paper_commission_resolver(rows())
    for row in fee_rows():
        assert row["account_scope"] == "paper"
        assert row["asset_class"] == "us_equity"
        assert row["calibration_state"] == "UNCALIBRATED"
        assert "read 2026-11-11" in row["source"]
        assert "live account" in row["source"] and "NOT covered" in row["source"]
    commission = next(row for row in rows() if row["name"] == COMMISSION_ROW)
    assert "files.alpaca.markets/disclosures/library/BrokFeeSched.pdf" in commission["source"]
    assert "does not charge commissions" in commission["source"]
    regulatory = next(row for row in rows() if row["name"] == REGULATORY_ROW)
    assert "docs.alpaca.markets/us/docs/paper-trading" in regulatory["source"]
    assert "Regulatory fees" in regulatory["source"]


def test_a_paper_equity_fill_costs_exactly_the_rows_values_in_usd():
    money = commission_for_fill(rows(), fill())
    assert isinstance(money, Money)
    assert money.currency == Currency.from_str("USD")
    expected = sum(Decimal(row["value"]) for row in fee_rows())
    assert money.as_decimal() == expected == Decimal("0")


def test_the_value_is_the_rows_exact_decimal_not_a_default():
    edited = changed(COMMISSION_ROW, value="0.01")
    assert commission_for_fill(edited, fill()).as_decimal() == Decimal("0.01")


@pytest.mark.parametrize("missing", [COMMISSION_ROW, REGULATORY_ROW])
def test_a_missing_row_refuses(missing):
    with pytest.raises(ValueError, match="missing"):
        paper_commission_resolver([row for row in fee_rows() if row["name"] != missing])


def test_a_duplicate_row_refuses():
    with pytest.raises(ValueError, match="duplicate"):
        paper_commission_resolver([*fee_rows(), fee_rows()[0]])


@pytest.mark.parametrize("source", [None, "", "   ", "a note with no url"])
def test_a_row_without_a_source_refuses(source):
    with pytest.raises(ValueError, match="source"):
        paper_commission_resolver(changed(COMMISSION_ROW, source=source))


def test_a_row_without_a_source_field_refuses():
    edited = changed(REGULATORY_ROW)
    for row in edited:
        if row["name"] == REGULATORY_ROW:
            del row["source"]
    with pytest.raises(ValueError, match="source"):
        paper_commission_resolver(edited)


@pytest.mark.parametrize("scope", ["live", "", None, "PAPER"])
def test_a_row_not_scoped_to_paper_refuses(scope):
    with pytest.raises(ValueError, match="scope"):
        paper_commission_resolver(changed(COMMISSION_ROW, account_scope=scope))


@pytest.mark.parametrize("asset_class", ["crypto", "us_option", None])
def test_a_row_not_scoped_to_us_equity_refuses(asset_class):
    with pytest.raises(ValueError, match="scope"):
        paper_commission_resolver(changed(REGULATORY_ROW, asset_class=asset_class))


@pytest.mark.parametrize("value", [0.0, 0, True, None, Decimal("0")])
def test_a_value_that_is_not_a_decimal_string_refuses(value):
    with pytest.raises(TypeError, match="string"):
        paper_commission_resolver(changed(COMMISSION_ROW, value=value))


@pytest.mark.parametrize("value", ["-0.01", "NaN", "Infinity", "zero", "0.001", ""])
def test_an_invalid_decimal_value_refuses(value):
    with pytest.raises(ValueError, match="value"):
        paper_commission_resolver(changed(COMMISSION_ROW, value=value))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unit", "USD"),
        ("currency", "EUR"),
        ("calibration_state", "UNAVAILABLE"),
        ("calibration_state", "CALIBRATED"),
        ("last_change", "yesterday"),
        ("last_change", None),
    ],
)
def test_an_invalid_unit_currency_state_or_date_refuses(field, value):
    with pytest.raises(ValueError):
        paper_commission_resolver(changed(REGULATORY_ROW, **{field: value}))


@pytest.mark.parametrize("asset_class", ["crypto", "us_option", None])
def test_a_fill_that_is_not_a_us_equity_refuses(asset_class):
    resolver = paper_commission_resolver(rows())
    with pytest.raises(ValueError, match="us_equity"):
        resolver(fill(asset_class))


def test_a_fill_without_an_order_refuses():
    resolver = paper_commission_resolver(rows())
    with pytest.raises(ValueError, match="us_equity"):
        resolver({"event": "fill", "price": "10.00", "qty": "10"})


def test_the_resolver_keeps_the_rows_it_validated():
    source = rows()
    resolver = paper_commission_resolver(source)
    for row in source:
        if row["name"] == COMMISSION_ROW:
            row["value"] = "5"
    assert resolver(fill()).as_decimal() == Decimal("0")
