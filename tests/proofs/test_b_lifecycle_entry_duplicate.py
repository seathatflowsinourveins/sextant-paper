"""R3 rejected-print regression; synthetic inputs, native offline BacktestNode only."""

from nautilus_trader.model import OrderSide, TradeTick
from test_b_lifecycle import quote
from test_b_lifecycle_entry import QUOTES, TAPE, at, house, run_entry, ticks
from test_b_lifecycle_protect import NATIVE


def test_a_rejected_duplicate_cannot_be_the_print_that_submits_an_entry(tmp_path):
    """Rank arrives after a valid run; replaying its last print cannot enter."""
    early = [*TAPE[:-1], ("-0.5", TAPE[-1][1], TAPE[-1][2])]
    last = ticks(early)[-1]
    duplicate = TradeTick(
        last.instrument_id,
        last.price,
        last.size,
        last.aggressor_side,
        last.trade_id,
        last.ts_event,
        at("0.5"),
    )
    journal, orders, strategy = run_entry(
        tmp_path,
        tape=early,
        quotes=[*QUOTES, quote(at("0.4"), "0.5050", "0.5100")],
        extra_trades=(duplicate,),
        native_trail_fraction=NATIVE,
    )
    book = next(iter(strategy.books.values()))
    assert book.entry.last.ts_init == at("-0.5")  # the duplicate was actually rejected
    assert sum(ts == last.ts_event for ts, _ in book.trades) == 1
    assert not [row for row in journal if row["kind"] == "submit"]
    assert not [order for order in house(orders) if order.side is OrderSide.BUY]
