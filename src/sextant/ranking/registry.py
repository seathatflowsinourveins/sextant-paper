"""ADR-0018 explicit registry, using upstream registry.py's checks and SHA identity.

Source: northstar-lanes@00203275944a22655b3e14b221cfde9a265d549a:
registry.py, contracts.py:358-398. No implicit starting values or operating registry.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import TypedDict


@dataclass(frozen=True, slots=True)
class RegistryRow:
    name: str
    unit: str
    value: Decimal | int
    source: str
    calibration_state: str
    last_change: date


ROWS = {
    "mover_min_move": (Decimal, "fraction"),
    "mover_min_trades_accel": (Decimal, "ratio"),
    "mover_min_dvol_ratio": (Decimal, "ratio"),
    "move_1m_window_s": (int, "seconds"),
    "move_5m_window_s": (int, "seconds"),
    "trades_accel_window_s": (int, "seconds"),
    "trades_accel_baseline_min": (int, "minutes"),
    "publish_min_interval_s": (int, "seconds"),
    "generation_max_age_s": (int, "seconds"),
    "heartbeat_max_age_s": (int, "seconds"),
    "input_max_age_s": (int, "seconds"),
    "top_n": (int, "symbols"),
}

# Trading/entry deployment only (ADR-0003 amendment): kept out of ROWS so the
# display registry and its params_hash stay untouched. No values live here.
ENTRY_ROWS = {
    "entry_heartbeat_max_age_s": (int, "seconds"),
    "entry_generation_max_age_s": (int, "seconds"),
    "entry_input_max_age_s": (int, "seconds"),
}


# Paper B: LEAD-1501 requires recorded-session measurement before a reviewed value.
# No deployment defaults. Tests inject rows in memory, never through the command.
B_ENTRY_AGE_ROWS: tuple[RegistryRow, ...] = ()

# The gate is an entry setting outside the generation registry and its hash.
RANK_GATE_ENABLED = RegistryRow(
    name="rank_gate_enabled",
    unit="boolean",
    value=False,
    source=(
        "LEAD-1759 item (b), 2026-11-10: default OFF; Release Toggles "
        "(https://martinfowler.com/articles/feature-toggles.html#ReleaseToggles), read 2026-11-10; "
        "enabled policy follows ADR-0003:24,42 and ADR-0024:33-34"
    ),
    calibration_state="UNCALIBRATED",
    last_change=date(2026, 11, 10),
)


def _require_source(row: RegistryRow) -> None:
    """A row's source is non-empty text and its last change a date, never coerced."""
    source: object = row.source
    if not isinstance(source, str) or not source.strip() or type(row.last_change) is not date:
        raise ValueError("registry source and last change required")


@dataclass(frozen=True, slots=True)
class Registry:
    rows: tuple[RegistryRow, ...]
    params_hash: str = field(init=False)

    def __post_init__(self) -> None:
        names = {row.name for row in self.rows}
        if len(names) != len(self.rows) or names != ROWS.keys():
            raise ValueError("missing, duplicate or unknown registry rows")
        for row in self.rows:
            kind, unit = ROWS[row.name]
            if type(row.value) is not kind:
                raise TypeError(f"{row.name} must be {kind.__name__}, never float or bool")
            if not Decimal(row.value).is_finite() or row.value <= 0 or row.unit != unit:
                raise ValueError(f"invalid registry value/unit: {row.name}")
            _require_source(row)
            if row.calibration_state != "UNCALIBRATED":
                raise ValueError("this carry has no calibration evidence")
        ordered = tuple(sorted(self.rows, key=lambda r: r.name))
        object.__setattr__(self, "rows", ordered)
        payload = json.dumps(
            [asdict(row) for row in ordered], default=str, sort_keys=True, separators=(",", ":")
        )
        object.__setattr__(self, "params_hash", hashlib.sha256(payload.encode()).hexdigest())

    def __getattr__(self, name: str) -> Decimal | int:
        for row in self.rows:
            if row.name == name:
                return row.value
        raise AttributeError(name)

    def seconds(self, name: str) -> int:
        return int(getattr(self, name)) * 1_000_000_000


class EntryAges(TypedDict):
    entry_heartbeat_max_age_s: int
    entry_generation_max_age_s: int
    entry_input_max_age_s: int


def entry_ages(rows: tuple[RegistryRow, ...]) -> EntryAges:
    """Validated ``RankingConsumer`` entry ages (``consumer.py:44-46``) from rows."""
    names = {row.name for row in rows}
    if len(names) != len(rows) or names != ENTRY_ROWS.keys():
        raise ValueError("missing, duplicate or unknown entry registry rows")
    ages: dict[str, int] = {}
    for row in rows:
        kind, unit = ENTRY_ROWS[row.name]
        if type(row.value) is not kind:
            raise TypeError(f"{row.name} must be {kind.__name__}, never float or bool")
        if not Decimal(row.value).is_finite() or row.value <= 0 or row.unit != unit:
            raise ValueError(f"invalid registry value/unit: {row.name}")
        _require_source(row)
        if row.calibration_state != "UNCALIBRATED":
            raise ValueError("this carry has no calibration evidence")
        ages[row.name] = int(row.value)
    return {
        "entry_heartbeat_max_age_s": ages["entry_heartbeat_max_age_s"],
        "entry_generation_max_age_s": ages["entry_generation_max_age_s"],
        "entry_input_max_age_s": ages["entry_input_max_age_s"],
    }
