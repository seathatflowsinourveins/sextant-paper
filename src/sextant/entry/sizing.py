"""N5 sizing and durable basis; entry_sizing.py/persist.py pins are in docs/manifest.md."""

import json
from collections import namedtuple
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.model import Equity, Money, Price, Quantity
from nautilus_trader.risk import FixedRiskSizer

from sextant.entry.orders import funding_unit_cost
from sextant.entry.parameters import EntryParameters, number
from sextant.execution.state import write_atomic


class SegmentBasis:
    def __init__(self, path: Path, account: str, params: EntryParameters):
        self.path, self.account, self.params = path, account, params
        self.receipt: dict[str, Any] = {}
        if path.exists():
            self.receipt = json.loads(path.read_text())
            if self.receipt.get("account") != account:
                raise ValueError("basis account mismatch")
            number(self.receipt.get("basis"), "persisted basis", zero=True)
            if not self.receipt.get("segment") or not self.receipt.get("params_hash"):
                raise ValueError("invalid basis receipt")

    def freeze(
        self, segment: str, buying_power: Decimal | None, *, first_read: str
    ) -> Decimal | None:
        if self.receipt.get("segment") == segment:
            if self.receipt["params_hash"] != self.params.params_hash:
                raise ValueError("basis parameters changed inside segment")
            return Decimal(self.receipt["basis"])
        if buying_power is None:
            return None
        if not segment or first_read not in {"segment_start", "node_start"}:
            raise ValueError("basis segment or first-read label")
        basis = number(buying_power, "buying_power", zero=True) * self.params["bp_target_fraction"]
        receipt = dict(
            account=self.account,
            segment=segment,
            basis=str(basis),
            first_read=first_read,
            params_hash=self.params.params_hash,
        )
        write_atomic(self.path, receipt)
        self.receipt = receipt
        return basis


class Sizing(
    namedtuple(
        "Sizing",
        "quantity planned_risk stop_distance confidence_multiplier "
        "binding_term refusal params_hash",
    )
):
    def journal(self) -> dict[str, Any]:
        return {
            name: str(value) if isinstance(value, (Decimal, Quantity)) else value
            for name, value in self._asdict().items()
        }


def size_entry(
    *,
    instrument: Equity,
    params: EntryParameters,
    setup: str,
    entry: Decimal,
    initial_trigger: Decimal,
    equity: Decimal | None,
    basis: Decimal | None,
    buying_power: Decimal | None,
    dollar_volume: Decimal | None,
    confidence: Decimal | None,
    prearm_stopouts: int,
    commission_rate: Decimal | None = None,
) -> Sizing:
    planned = distance = multiplier = binding = refusal = None
    try:
        entry = number(entry, "entry")
        commission_rate = number(commission_rate, "commission_rate", zero=True)
        trigger = number(initial_trigger, "initial_trigger")
        equity, basis = number(equity, "equity"), number(basis, "basis")
        buying_power = number(buying_power, "buying_power")
        dollar_volume = number(dollar_volume, "dollar_volume", zero=True)
        if trigger >= entry:
            raise ValueError("initial_trigger must be below entry")
        if type(prearm_stopouts) is not int or prearm_stopouts < 0:
            raise ValueError("prearm_stopouts")
        if (confidence is None) != (setup in {"FIRST_LEG", "HALT_RESUME"}):
            raise ValueError("confidence authority differs from setup")
        if confidence is not None:
            confidence = number(confidence, "confidence", zero=True)
            if confidence > 1:
                raise ValueError("confidence exceeds one")
            multiplier = (
                params["confidence_size_floor"] + (1 - params["confidence_size_floor"]) * confidence
            )
        stopout = max(params["stopout_size_floor"], params["stopout_size_decay"] ** prearm_stopouts)
        planned = basis * params["stop_ref_fraction"] / params["portfolio_slots"]
        planned *= stopout * (multiplier if multiplier is not None else Decimal(1))
        number(planned, "planned_risk")
        distance = entry - trigger + params["chase_cap_fraction"] * entry
        tick = instrument.price_increment.as_decimal()
        stop = ((entry - distance) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        if not 0 < stop < entry:
            raise ValueError("stop_loss")
        native_entry = Price.from_str(f"{entry:.{instrument.price_precision}f}")
        native_stop = Price.from_str(f"{stop:.{instrument.price_precision}f}")
        if native_entry.as_decimal() != entry or native_stop.as_decimal() != stop:
            raise ValueError("price_increment")
        distance = entry - stop
        candidates = {
            "risk": planned * (1 - 2 * commission_rate) / distance,
            "buying_power": buying_power
            / funding_unit_cost(entry, commission_rate, instrument.quote_currency.precision),
            "flow": params["flow_share_cap"] * dollar_volume / entry,
        }
        hard_limit = min(candidates["buying_power"], candidates["flow"])
        if hard_limit < 1:
            raise ValueError("hard_limit below one share")
        quantity = FixedRiskSizer(instrument).calculate(
            entry=native_entry,
            stop_loss=native_stop,
            equity=Money.from_str(f"{equity} {instrument.quote_currency}"),
            risk=planned / equity,
            commission_rate=commission_rate,
            hard_limit=hard_limit,
        )
        whole = quantity.as_decimal().to_integral_value(rounding=ROUND_FLOOR)
        if whole < 1:
            raise ValueError("quantity below one share")
        quantity = Quantity.from_int(int(whole))
        binding = min(candidates, key=lambda key: candidates[key])
    except (ValueError, ArithmeticError) as error:
        quantity, refusal = Quantity.from_int(0), str(error)
    return Sizing(quantity, planned, distance, multiplier, binding, refusal, params.params_hash)
