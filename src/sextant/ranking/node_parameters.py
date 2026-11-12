"""The ranking node's window rows (review F7): each value from its registry row, never a flag.

``deploy/ranking-node/parameters.json`` follows the deploy parameter pattern (a schema, then rows
with value, unit, status, source and last change). A sourced row is UNCALIBRATED; a window with
no source is UNAVAILABLE with a null value, and the caller decides what the node does without
it. There is no override (lead ruling 04:2x, correcting 03:35): a value with no row, or a row
without its authority, stays unavailable, and a different value is a reviewed change of the row
(AGENTS.md Rule 2). The resolution, with each value's origin, is written as
``parameters-receipt.json`` beside the run's other receipts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sextant.execution.state import write_atomic

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "deploy/ranking-node/parameters.json"
SCHEMA = "sextant.ranking-node.parameters/1"
RECEIPT = "parameters-receipt.json"
FIELDS = ("value", "unit", "status", "source", "last_change")


@dataclass(frozen=True)
class Row:
    value: int | None
    status: str
    source: str
    unit: str
    last_change: str


def _check(name: str, item: object) -> Row:
    """Every field present; a positive int with its source, or null as UNAVAILABLE."""
    if not isinstance(item, dict) or any(field not in item for field in FIELDS):
        raise ValueError(f"the {name} row needs each of {', '.join(FIELDS)}")
    value, status = item["value"], item["status"]
    texts = [item[field] for field in ("source", "unit", "last_change")]
    sourced = status in {"UNCALIBRATED", "CALIBRATED"}
    if not all(isinstance(text, str) and text.strip() for text in texts) or not (
        (sourced and type(value) is int and value > 0)
        or (status == "UNAVAILABLE" and value is None)
    ):
        raise ValueError(
            f"the {name} row needs a positive int with its source, unit and last change, "
            "or null as UNAVAILABLE"
        )
    return Row(value, status, item["source"], item["unit"], item["last_change"])


def load(path: Path = DEFAULT_PATH) -> dict[str, Row]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema") != SCHEMA or not isinstance(raw.get("rows"), dict):
        raise ValueError(f"{path} is not a {SCHEMA} registry")
    return {name: _check(name, item) for name, item in raw["rows"].items()}


def resolve(rows: dict[str, Row], name: str, override: int | None = None) -> tuple[int | None, str]:
    """The row's value: looked up and checked first; an unavailable row stays None. No
    override exists, so any value passed here is refused by the row's name."""
    if name not in rows:
        raise ValueError(f"no {name} row in the registry")
    row = rows[name]
    _check(name, {**row.__dict__})
    if override is not None:
        raise ValueError(f"{name}: no override; a different value is a reviewed change of its row")
    return (None, "unavailable") if row.value is None else (row.value, "registry")


def refuse_retired(argv: list[str] | None, flags: dict[str, str]) -> None:
    """A retired window flag on a command line is refused by its row's name, not ignored."""
    for arg in argv or ():
        flag = arg.split("=", 1)[0]
        if flag in flags:
            raise ValueError(f"{flags[flag]}: {flag} no longer exists; the row alone sets it")


def write_receipt(directory: Path, resolved: dict[str, tuple[int | None, str]]) -> Path:
    path = Path(directory) / RECEIPT
    write_atomic(
        path,
        {name: {"value": value, "origin": origin} for name, (value, origin) in resolved.items()},
    )
    return path
