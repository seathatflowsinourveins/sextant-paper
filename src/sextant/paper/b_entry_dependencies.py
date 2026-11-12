"""B's sourced entry dependencies (LEAD-1452 part 4); no order or balance implementation.

BUILD ON Sextant@7252b0f5 exits/policy.py:69-115, alpaca/commission.py:67-94 and
entry/sizing.py:17-50. KEEP OURS for B's row/path admission: searched those packages and
nautilus_trader@d53849b2 / alpaca-py@cc4cb3b7; neither ships B's parameters. The native
sizer takes a Decimal rate (rc8 python/nautilus_trader/risk/__init__.pyi:18-28;
crates/risk/src/sizing.rs:132-153). USD/fill is not that rate; only a sourced zero model
is representable as one constant zero rate, independent of price, quantity and fill count.
"""

import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import fields
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sextant.alpaca.commission import NAMES as FEE_NAMES
from sextant.alpaca.commission import paper_commission_resolver
from sextant.entry.parameters import number
from sextant.exits.policy import CHASE, ExitParams

SCOPE = "alpaca-paper-b"
EXIT_FIELDS = {f.name for f in fields(ExitParams)} - set(CHASE)
EXIT_ROWS = EXIT_FIELDS | {"chase_min_interval_s", "round_trip_cost"}


def _indexed(rows: Sequence[Mapping[str, Any]], expected: set[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for row in rows:
        name = row.get("name")
        if name not in expected or name in found:
            raise ValueError(f"{name}: unknown or duplicate row")
        found[name] = row
    if missing := expected - found.keys():
        raise ValueError(f"missing rows: {sorted(missing)}")
    return found


def _authority(row: Mapping[str, Any]) -> None:
    name = row["name"]
    if row.get("scope") != SCOPE:
        raise ValueError(f"{name}: scope must be {SCOPE}")
    if any(
        not isinstance(row.get(k), str) or not row[k].strip()
        for k in ("source", "unit", "last_change")
    ):
        raise ValueError(f"{name}: source/unit/last_change required")
    try:
        date.fromisoformat(row["last_change"])
    except ValueError:
        raise ValueError(f"{name}: last_change must be an ISO date") from None


def load_exit_params(rows: Sequence[Mapping[str, Any]]) -> ExitParams:
    """B's file only, with unavailable cost/interval and no entry-to-exit chase fallback."""
    found = _indexed(rows, EXIT_ROWS)
    values: dict[str, Any] = {name: None for name in CHASE}
    for name, row in found.items():
        _authority(row)
        value, state = row.get("value"), row.get("calibration_state")
        if name in {"round_trip_cost", "chase_min_interval_s"}:
            if value is not None or state != "UNAVAILABLE":
                raise ValueError(f"{name}: must remain UNAVAILABLE for B")
            continue
        if value is None or state not in {"UNCALIBRATED", "CALIBRATED"}:
            raise ValueError(f"{name}: no sourced value")
        if name == "breach_persist_count":
            if type(value) is not int or value < 1:
                raise ValueError(f"{name}: positive integer required")
            values[name] = value
        else:
            values[name] = number(value, name, zero=True)
    return ExitParams(**values)


def sizing_commission_rate(rows: Sequence[Mapping[str, Any]]) -> Decimal:
    """Use the existing paper fee model's scope/source/unit validation; no live/option rate."""
    amount = paper_commission_resolver(rows)({"order": {"asset_class": "us_equity"}})
    if amount.as_decimal() != 0:
        raise ValueError(f"{', '.join(FEE_NAMES)}: nonzero USD per fill is not a constant rate")
    return amount.as_decimal()  # zero USD / any positive notional = zero; no fabricated notional


def load_basis_path(parameters: Path) -> Path:
    """Explicit approved existing home and file parent; persistence stays in SegmentBasis.

    pathlib resolution and tempfile's OS write probe follow Python 3.13 pathlib/tempfile
    docs (read 2026-11-11); no account/key-derived location or balance, no mkdir/reset.
    """
    rows = json.loads(parameters.read_text(encoding="utf-8"), parse_float=Decimal)
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError("basis_path: state parameters must be rows")
    bound = "basis_receipt_max_bytes"
    found = _indexed(rows, {"basis_state_home", "basis_path", bound})
    row = found[bound]
    _authority(row)
    limit = row.get("value")
    if row.get("unit") != "bytes":
        raise ValueError(f"{bound}: bytes unit required")
    if row.get("calibration_state") not in {"UNCALIBRATED", "CALIBRATED"} or limit is None:
        raise ValueError(f"{bound}: unavailable")
    if type(limit) is not int or limit <= 0:
        raise ValueError(f"{bound}: positive integer required")
    paths = {}
    for name in ("basis_state_home", "basis_path"):
        row = found[name]
        _authority(row)
        value = row.get("value")
        if row.get("unit") != "path" or not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name}: explicit path required")
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f"{name}: absolute path required")
        paths[name] = path.resolve()
    home, path = paths["basis_state_home"], paths["basis_path"]
    if not home.is_dir():
        raise ValueError("basis_state_home: approved home must already exist")
    if not path.is_relative_to(home) or path == home:
        raise ValueError("basis_path: must be a file under basis_state_home")
    if not path.parent.is_dir() or (path.exists() and not path.is_file()):
        raise ValueError("basis_path: existing parent and a state-file location required")
    # Python's JSON docs recommend bounding input before parsing (read 2026-11-11).
    # The registry supplies the ceiling; no numeric default is authorized here.
    if path.exists() and path.stat().st_size > limit:
        raise ValueError(f"basis_path: receipt exceeds {bound}")
    try:
        with tempfile.TemporaryFile(dir=path.parent):
            pass
    except OSError as error:
        raise ValueError(f"basis_path: parent is not writable: {error}") from error
    return path
