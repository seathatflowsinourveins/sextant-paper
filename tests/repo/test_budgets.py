"""Paper harness budgets, adopted from the template; changes need a sourced PR.

SOURCE_LINE_CAP is the lead's uncalibrated starting cap: 25,000 (LEAD-1915,
2026-11-11), raised to 25,300 for the remaining reviewed chain links
(LEAD-2046-addendum-2). G3's LEAD-2259 build brief sets measured total + 100:
24,984 raw lines + 100 = 25,084, within the 26,500 hard ceiling; after the node link (#31)
and the TradingView display links reached main, the merged tip measures 25,108 lines, so the
cap is 25,208 (measured total + 100, LEAD-2259; hard ceiling 26,500).
See docs/landscape.md and docs/import-evidence.md.
"""

from pathlib import Path

ROOT = Path(__file__).parents[2]
SOURCE_LINE_CAP = 25_208


def _lines(path: Path) -> int:
    return len(path.read_text().splitlines())


def test_agent_files_stay_short() -> None:
    assert _lines(ROOT / "AGENTS.md") + _lines(ROOT / "CLAUDE.md") < 120


def test_justfile_stays_short() -> None:
    assert _lines(ROOT / "justfile") < 105


def test_src_stays_under_budget() -> None:
    assert sum(_lines(path) for path in (ROOT / "src").rglob("*.py")) <= SOURCE_LINE_CAP


def test_design_stays_short() -> None:
    assert _lines(ROOT / "docs" / "design.md") <= 220
