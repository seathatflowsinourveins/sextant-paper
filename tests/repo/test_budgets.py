"""Paper harness budgets, adopted from the template; changes need a sourced PR.

SOURCE_LINE_CAP is the lead's uncalibrated starting cap: 25,000 (LEAD-1915,
2026-11-11), raised to 25,300 for the remaining reviewed chain links
(LEAD-2046-addendum-2); it ratchets down after the chain lands and the
move-only splits. See docs/landscape.md and docs/import-evidence.md.
"""

from pathlib import Path

ROOT = Path(__file__).parents[2]
SOURCE_LINE_CAP = 25_300


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
