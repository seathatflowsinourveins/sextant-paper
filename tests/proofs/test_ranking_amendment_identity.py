"""N1f fix round (Codex xhigh r1 at 44b860fe, P1): an amendment names its print in full.

The first proof is the reviewer's witness (review-gpt-gates/n1frows-44b860f-codex-r1,
test_delta_witnesses.py:37-54) unchanged. The lead's ruling (04:2x): the identity is exchange,
original id, original price and original size; an id match with another price or size is not
the stored print, withdraws nothing and is counted as its own receipt class.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
import test_ranking_amendments as amendments
from nautilus_trader.model import CustomData


@pytest.mark.parametrize(
    ("field", "value"),
    [("original_price", Decimal("9.99")), ("original_size", Decimal("999"))],
)
def test_an_amendment_must_match_the_original_price_and_size(tmp_path, monkeypatch, field, value):
    make_amendment = amendments.amendment

    def mismatched(kind, symbol, original_id, now):
        event = make_amendment(kind, symbol, original_id, now)
        return CustomData(event.data_type, replace(event.data, **{field: value}))

    monkeypatch.setattr(amendments, "amendment", mismatched)
    result = amendments.amended_run(tmp_path, (365, "correction", "PENNY", 60))
    actual_rank = amendments.rank(result, 420)
    assert actual_rank is not None, (
        f"D-60 was price=0.50, size=100; nonmatching {field}={value} "
        f"incorrectly withdrew its rank: {actual_rank}"
    )


def test_a_mismatched_amendment_is_its_own_receipt_class(tmp_path, monkeypatch) -> None:
    make_amendment = amendments.amendment

    def mismatched(kind, symbol, original_id, now):
        event = make_amendment(kind, symbol, original_id, now)
        return CustomData(event.data_type, replace(event.data, original_price=Decimal("9.99")))

    monkeypatch.setattr(amendments, "amendment", mismatched)
    amendments.amended_run(tmp_path, (365, "correction", "PENNY", 60))
    receipt = amendments.receipt(tmp_path)
    assert receipt["amendments"] == {"inside": 0, "outside": 0, "unprimed": 0}
    assert receipt["amendments_mismatched"] == {"count": 1, "notional": "999.00"}


# Reviews at bf7ac594 and ed19739e: the data client builds each tick at instrument precision 4
# (alpaca/data.py ``_Stock.price``, alpaca/parsing.py), so the vendor's raw price travels in the
# facts record beside it and the identity is compared raw to raw. These run the data client's
# own tick and facts builders; the backtest replays ticks only, so the facts are keyed here.
SUB = Decimal("0.000056")  # a sub-dollar print with more than four decimals


def _vendor_run(tmp_path, monkeypatch, *, original: Decimal, plan, missing=()):
    from datetime import UTC, datetime

    import test_ranking_actor as actor
    from alpaca.data.models import Trade
    from nautilus_trader.model import Currency, Equity, Price, Quantity, Symbol
    from test_ranking import NS

    from sextant.alpaca.data import _Stock, _trade_tick
    from sextant.alpaca.stream_facts import StreamFacts, trade_facts

    records = {}

    class Vendor(StreamFacts):
        def __call__(self, tick):
            return True, ("@",)

        def vendor(self, tick):
            facts = records.get(tick.trade_id.value)
            return None if facts is None else (facts.price, facts.size)

    def equity(iid):  # the production precision (alpaca/parsing.py _PRICE_PRECISION)
        return Equity(
            instrument_id=iid,
            raw_symbol=Symbol(str(iid.symbol)),
            currency=Currency.from_str("USD"),
            price_precision=4,
            price_increment=Price.from_str("0.0001"),
            lot_size=Quantity.from_str("1"),
            ts_event=0,
            ts_init=0,
        )

    def vendor(tick):
        name = str(tick.instrument_id.symbol)
        exchange, number = tick.trade_id.value.split("-")
        raw = tick.price.as_decimal() + (SUB if name == "PENNY" else 0)
        at = datetime.fromtimestamp(tick.ts_event // NS, UTC)
        fields = {"t": at, "p": float(raw), "s": 100, "x": exchange, "i": int(number)}
        row = Trade(name, fields | {"c": ["@"], "z": "C"})
        built = _trade_tick(row, _Stock(tick.instrument_id, name, 4, 0), tick.ts_init)
        if built.trade_id.value not in missing:
            records[built.trade_id.value] = trade_facts(row, built)
        return built

    make_trades, make_amendment = amendments.trades, amendments.amendment

    def named(kind, symbol, original_id, now):
        event = make_amendment(kind, symbol, original_id, now)
        return CustomData(event.data_type, replace(event.data, original_price=original))

    monkeypatch.setattr(actor, "equity", equity)
    monkeypatch.setattr(amendments, "trades", lambda: [vendor(t) for t in make_trades()])
    monkeypatch.setattr(amendments, "amendment", named)
    run = amendments.session_run
    monkeypatch.setattr(
        amendments, "session_run", lambda *a, **k: run(*a, facts=Vendor("sip"), **k)
    )
    return amendments.amended_run(tmp_path, *plan)


def test_a_genuine_cancel_of_a_sub_dollar_print_withdraws_its_rank(tmp_path, monkeypatch):
    raw = Decimal("0.50") + SUB  # the price the vendor sent for D-60, as its cancel names it
    out = _vendor_run(tmp_path, monkeypatch, original=raw, plan=[(365, "cancel", "PENNY", 60)])
    assert amendments.rank(out, 420) is None, "the cancelled print still feeds the rank"
    assert amendments.receipt(tmp_path)["amendments"]["inside"] == 1
    assert amendments.rank(out, 365) is None  # withdrawn at once, unthrottled
    assert amendments.rank(out, 540) is not None  # the windows have rolled past it


def test_a_sub_dollar_cancel_naming_another_price_is_still_a_mismatch(tmp_path, monkeypatch):
    other = Decimal("0.51") + SUB
    out = _vendor_run(tmp_path, monkeypatch, original=other, plan=[(365, "cancel", "PENNY", 60)])
    assert amendments.rank(out, 420) is not None
    assert amendments.receipt(tmp_path)["amendments_mismatched"]["count"] == 1


def test_a_cancel_differing_only_beyond_four_decimals_is_a_mismatch(tmp_path, monkeypatch):
    # The gate's review at ed19739e: 0.500056 stored, 0.500057 named; both are 0.5001 as ticks.
    near = Decimal("0.500057")
    out = _vendor_run(tmp_path, monkeypatch, original=near, plan=[(365, "cancel", "PENNY", 60)])
    body = amendments.receipt(tmp_path)
    assert body["amendments"]["inside"] == 0, "a different vendor price matched by rounding"
    assert body["amendments_mismatched"]["count"] == 1
    assert amendments.rank(out, 420) is not None


def test_a_print_without_a_raw_vendor_record_is_unprimed_never_matched(tmp_path, monkeypatch):
    raw = Decimal("0.50") + SUB
    plan = [(365, "cancel", "PENNY", 60)]
    out = _vendor_run(tmp_path, monkeypatch, original=raw, plan=plan, missing={"D-60"})
    assert amendments.receipt(tmp_path)["amendments"] == {"inside": 0, "outside": 0, "unprimed": 1}
    assert amendments.rank(out, 420) is not None
