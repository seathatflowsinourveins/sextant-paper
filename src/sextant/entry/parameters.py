"""Explicit authority checks adapted from the pinned contracts.Params in docs/manifest.md."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any

NAMES = frozenset(
    "first_leg_window_s first_leg_trades_count first_leg_span_s first_leg_max_move "
    "entry_max_vwap_extension_atr entry_chase_bps entry_ttl_s reclaim_lookback_s "
    "reclaim_trades_count reclaim_trades_span_s reclaim_hold_s reclaim_max_above_buffers "
    "atr_period atr_bar_minutes max_data_age_s max_armed_symbols bp_target_fraction "
    "stop_ref_fraction portfolio_slots stopout_size_decay stopout_size_floor confidence_size_floor "
    "chase_cap_fraction flow_share_cap flow_share_window_s covered_flow_window_s "
    "covered_flow_baseline_min covered_flow_ratio".split()
)
FIRST_LEG = frozenset(
    "first_leg_window_s first_leg_trades_count first_leg_span_s first_leg_max_move "
    "entry_chase_bps entry_ttl_s atr_period atr_bar_minutes max_data_age_s "
    "max_armed_symbols".split()
)
# D-38: C's entry-size rows, read with the first-leg rows and nothing else.
SIZING = frozenset(
    "bp_target_fraction stop_ref_fraction portfolio_slots stopout_size_decay "
    "stopout_size_floor confidence_size_floor chase_cap_fraction flow_share_cap "
    "flow_share_window_s".split()
)
COUNTS = frozenset(
    "first_leg_trades_count reclaim_trades_count atr_period atr_bar_minutes "
    "max_armed_symbols portfolio_slots".split()
)
FRACTIONS = frozenset(
    "first_leg_max_move reclaim_max_above_buffers bp_target_fraction stop_ref_fraction "
    "stopout_size_decay stopout_size_floor confidence_size_floor "
    "chase_cap_fraction flow_share_cap".split()
)
ZERO_OK = frozenset(
    {"reclaim_max_above_buffers", "confidence_size_floor", "chase_cap_fraction", "entry_chase_bps"}
)
SEC = 1_000_000_000


def number(value: Any, name: str, *, zero: bool = False) -> Decimal:
    if type(value) not in (str, int, Decimal):
        raise ValueError(f"{name}: finite Decimal required")
    result = Decimal(value)
    if not result.is_finite() or result < 0 or (not zero and result == 0):
        raise ValueError(f"{name}: finite {'nonnegative' if zero else 'positive'} Decimal required")
    return result


@dataclass(frozen=True)
class EntryParameters:
    values: Mapping[str, Any]
    params_hash: str
    fixture: bool
    first_leg_only: bool = False
    sizing_only: bool = False

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        fixture=False,
        first_leg_only=False,
        sizing_only=False,
    ):
        if first_leg_only and sizing_only:
            raise ValueError("first_leg_only and sizing_only are mutually exclusive")
        names = [row["name"] for row in rows]
        if set(names) != NAMES or len(names) != len(NAMES):
            raise ValueError("missing, duplicate or unknown entry registry row")
        loaded = FIRST_LEG | SIZING if sizing_only else FIRST_LEG
        values: dict[str, Any] = {}
        for row in rows:
            name = row["name"]
            if (first_leg_only or sizing_only) and name not in loaded:
                continue  # reclaim, sizing, flow and extension rows stay unread
            scope, value = row.get("scope"), row.get("value")
            if not all(row.get(k) for k in ("source", "unit", "last_change")):
                raise ValueError(f"{name}: missing source/unit/last_change")
            if value is None or scope not in {"FIXTURE_ONLY", "REVIEWED"}:
                raise ValueError(f"{name}: unavailable or unreviewed authority")
            if scope == "FIXTURE_ONLY" and not fixture:
                raise ValueError(f"{name}: fixture is not deployment authority")
            if row.get("calibration") not in {"UNCALIBRATED", "CALIBRATED"}:
                raise ValueError(f"{name}: missing calibration state")
            if name in COUNTS:
                if type(value) is not int or value <= 0:
                    raise ValueError(f"{name}: positive integer required")
            else:
                value = number(value, name, zero=name in ZERO_OK)
                if name in FRACTIONS and value > 1:
                    raise ValueError(f"{name}: fraction exceeds one")
                if name in {"first_leg_max_move", "chase_cap_fraction"} and value >= 1:
                    raise ValueError(f"{name}: fraction must be less than one")
                if name.endswith("_s") and value * SEC != (value * SEC).to_integral_value():
                    raise ValueError(f"{name}: not representable in nanoseconds")
            values[name] = value
        if values["entry_ttl_s"] > values["first_leg_window_s"]:
            raise ValueError("entry_ttl_s exceeds first_leg_window_s")
        payload = json.dumps(
            sorted((dict(row) for row in rows), key=lambda r: r["name"]),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(
            (payload + " first_leg_only" * first_leg_only + " sizing_only" * sizing_only).encode()
        )
        return cls(
            MappingProxyType(values), digest.hexdigest(), fixture, first_leg_only, sizing_only
        )

    def __getitem__(self, name: str) -> Any:
        return self.values[name]

    def has(self, name: str) -> bool:
        return name in self.values

    def ns(self, name: str) -> int:
        return int(self[name] * SEC)
