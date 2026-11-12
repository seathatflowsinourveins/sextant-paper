"""The display producer's three bounds come from one sourced registry file, never a literal."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sextant.tradingview import prestage
from sextant.tradingview.bridge import DisplayBounds

PIN = "seathatflowsinourveins/northstar-trading@8256c16c31dcb8e53fc726e0c1c77dc2c8d0d4fa"


def _rows() -> list[dict[str, Any]]:
    raw = json.loads(prestage.DEFAULT_DISPLAY_BOUNDS.read_text(encoding="utf-8"))
    return [dict(row) for row in raw["rows"]]


def _write(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "display-bounds.params.json"
    path.write_text(
        json.dumps({"schema": "tv-display-bounds.registry/1", "rows": rows}), encoding="utf-8"
    )
    return path


def test_the_shipped_bounds_are_the_pinned_display_list_values() -> None:
    bounds = prestage.load_display_bounds()
    assert bounds == DisplayBounds(Decimal("0.05"), Decimal("2"), 600)
    rows = {row["name"]: row for row in _rows()}
    assert set(rows) == {"gap_bound", "rvol_bound", "max_age_s"}
    for name, line in (("gap_bound", 29), ("rvol_bound", 30), ("max_age_s", 31)):
        assert rows[name]["calibration_state"] == "UNCALIBRATED"
        assert (
            f"{PIN}:src/northstar_trading/scanner/display_lists.py:{line}" in rows[name]["source"]
        )


@pytest.mark.parametrize(
    "fault", ("float", "no_source", "missing", "duplicate", "unit", "calibration")
)
def test_a_bounds_row_without_its_authority_or_shape_is_refused(tmp_path: Path, fault: str) -> None:
    rows = _rows()
    if fault == "float":
        rows[0]["value"] = 0.05
    elif fault == "no_source":
        rows[1]["source"] = " "
    elif fault == "missing":
        rows = rows[:2]
    elif fault == "duplicate":
        rows = [*rows, dict(rows[0])]
    elif fault == "unit":
        rows[2]["unit"] = "minutes"
    else:
        rows[0]["calibration_state"] = "UNAVAILABLE"
    with pytest.raises(prestage.PrestageError, match="display bounds"):
        prestage.load_display_bounds(_write(tmp_path, rows))


def _capture(monkeypatch: Any) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(prestage, "run", fake_run)
    return seen


BASE = "--research-dir u --nasdaq-listed n --other-listed o --symbol-map m".split()


def test_the_command_line_reads_the_shipped_bounds_by_default(monkeypatch: Any) -> None:
    seen = _capture(monkeypatch)
    assert prestage.main(BASE) == 0
    assert seen["bounds"] == DisplayBounds(Decimal("0.05"), Decimal("2"), 600)


def test_explicit_bounds_need_all_three_flags(monkeypatch: Any) -> None:
    seen = _capture(monkeypatch)
    flags = ["--gap-bound", "0.07", "--rvol-bound", "3", "--bounds-max-age-s", "300"]
    assert prestage.main([*BASE, *flags]) == 0
    assert seen["bounds"] == DisplayBounds(Decimal("0.07"), Decimal("3"), 300)
    seen.clear()
    assert prestage.main([*BASE, "--gap-bound", "0.07"]) == 2
    assert seen == {}
