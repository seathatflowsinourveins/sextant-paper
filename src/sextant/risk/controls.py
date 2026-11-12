"""Explicit row-8 controls: no fixture or missing reserve becomes runtime authority.

Registry pattern: northstar-lanes contracts.Params, ADR-0018. Numeric pacing and
kill starts: northstar-trading@8256c16c31dc pacer/guard/actor; see the manifest.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any

SEC = 1_000_000_000
ENTRY_RESERVE_EXCEEDS_BURST = "entry-reserve-exceeds-burst"
COUNTS = frozenset(
    {
        "request_budget",
        "broker_limit",
        "observation_max_edges",
        "max_cancel_retries",
        "reserve_per_open_position",
        "reserve_per_exiting_position",
    }
)
OPTIONAL = frozenset({"reserve_per_open_position", "reserve_per_exiting_position"})
NAMES = COUNTS | frozenset(
    {
        "window_s",
        "burst_fraction",
        "cancel_timeout_s",
        "cancel_retry_s",
        "flatten_tick_s",
        "backoff_base_s",
        "backoff_max_s",
        "observation_reminder_s",
        "observation_money_step_fraction",
        "observation_leverage_step",
    }
)


@dataclass(frozen=True)
class Controls:
    values: Mapping[str, Any]
    params_hash: str
    fixture: bool

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        fixture=False,
        slots: int | None = None,
    ):
        names = [r["name"] for r in rows]
        if set(names) != NAMES or len(names) != len(NAMES):
            raise ValueError("missing, duplicate or unknown custody control")
        values = {}
        for row in rows:
            name, value = row["name"], row.get("value")
            if not all(
                row.get(key) for key in ("source", "unit", "last_change", "calibration", "scope")
            ):
                raise ValueError(f"{name}: missing registry metadata")
            if row.get("scope") == "FIXTURE_ONLY" and not fixture:
                raise ValueError(f"{name}: fixture is not deployment authority")
            if value is None and name in OPTIONAL:
                if row["scope"] not in {"FIXTURE_ONLY", "REVIEWED", "UNAVAILABLE"}:
                    raise ValueError(f"{name}: unavailable authority")
                values[name] = None
                continue
            if row.get("scope") not in {"FIXTURE_ONLY", "REVIEWED"} or value is None:
                raise ValueError(f"{name}: unavailable authority")
            if row.get("calibration") not in {"UNCALIBRATED", "CALIBRATED"}:
                raise ValueError(f"{name}: unknown calibration")
            if name in COUNTS:
                if (
                    type(value) is not int
                    or value < 0
                    or (
                        name in {"request_budget", "broker_limit", "observation_max_edges"}
                        and value < 2
                    )
                ):
                    raise ValueError(f"{name}: integer domain")
            else:
                if type(value) not in (str, int, Decimal):
                    raise ValueError(f"{name}: Decimal required")
                value = Decimal(value)
                if not value.is_finite() or value <= 0:
                    raise ValueError(f"{name}: positive Decimal required")
                if name.endswith("_s") and value * SEC != (value * SEC).to_integral_value():
                    raise ValueError(f"{name}: nanosecond precision")
            values[name] = value
        if values["request_budget"] > values["broker_limit"]:
            raise ValueError("request budget exceeds broker limit")
        burst = int(values["request_budget"] * values["burst_fraction"])
        if not 1 <= burst < values["request_budget"]:
            raise ValueError("burst outside budget")
        if slots is not None:
            if type(slots) is not int or slots <= 0:
                raise ValueError("slots: positive integer required")
            if burst < 2 * slots + 1:
                raise ValueError(
                    f"{ENTRY_RESERVE_EXCEEDS_BURST}: {slots} slots need burst >= "
                    f"{2 * slots + 1}, got {burst}"
                )
        payload = json.dumps(
            sorted((dict(r) for r in rows), key=lambda r: r["name"]), sort_keys=True, default=str
        )
        return cls(MappingProxyType(values), hashlib.sha256(payload.encode()).hexdigest(), fixture)

    def __getitem__(self, name: str) -> Any:
        return self.values[name]

    def ns(self, name: str) -> int:
        return int(self[name] * SEC)

    @property
    def burst(self) -> int:
        return int(self["request_budget"] * self["burst_fraction"])
