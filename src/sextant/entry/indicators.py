"""Decimal rc8 ATR/RMA carry; source pins and the exact-boundary exception are in ADR-0004."""

from decimal import Decimal

from nautilus_trader.model import Bar


class DecimalATR:
    def __init__(self, period: int):
        self.period, self.count = period, 0
        self.previous_close: Decimal | None = None
        self.seed_sum = Decimal(0)
        self.value: Decimal | None = None

    def handle_bar(self, bar: Bar) -> None:
        high, low, close = bar.high.as_decimal(), bar.low.as_decimal(), bar.close.as_decimal()
        true_range = (
            high - low
            if self.previous_close is None
            else max(self.previous_close, high) - min(low, self.previous_close)
        )
        self.previous_close = close
        self.count += 1
        if self.count <= self.period:
            self.seed_sum += true_range
            if self.count == self.period:
                self.value = self.seed_sum / self.period
        else:
            assert self.value is not None
            self.value = (self.value * (self.period - 1) + true_range) / self.period
