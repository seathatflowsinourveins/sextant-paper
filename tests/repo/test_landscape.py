"""Package coverage from sextant-movers' test_landscape.py; verdicts from this brief.

Read the table as the authority; do not maintain a second package list in the test.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SHA = re.compile(r"\b[0-9a-f]{40}\b")
PATH = re.compile(r"`([^`]+)`")
VERDICTS = {"use as is", "build on", "keep ours"}


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _rows(text: str) -> list[dict[str, str]]:
    section = re.split(r"^## Capabilities\s*$", text, flags=re.MULTILINE)[1]
    lines = [line for line in section.split("\n## ")[0].splitlines() if line.startswith("|")]
    header = _cells(lines[0])
    return [dict(zip(header, _cells(line), strict=True)) for line in lines[2:]]


def _sources(package: Path) -> set[str]:
    found: set[str] = set()
    if not package.exists():
        return found
    for child in package.iterdir():
        if child.is_dir() and child.name != "__pycache__" and any(child.rglob("*.py")):
            found.add(f"{child.name}/")
        elif child.suffix == ".py" and child.name != "__init__.py":
            found.add(child.name)
    return found


def uncovered(source: Path, text: str) -> list[str]:
    named = {path for row in _rows(text) for path in PATH.findall(row["Package"])}
    found = _sources(source)
    if "sextant/" in found:
        found.remove("sextant/")
        found.update(f"sextant/{path}" for path in _sources(source / "sextant"))
    return sorted(found - named)


def test_every_source_package_or_module_has_a_landscape_row() -> None:
    missing = uncovered(ROOT / "src", (ROOT / "docs/landscape.md").read_text())
    assert not missing, f"docs/landscape.md has no row whose Package cell names: {missing}"


def test_an_uncovered_package_fails_and_a_row_covers_it(tmp_path: Path) -> None:
    for name in ("sextant/__init__.py", "sextant/scores/__init__.py", "sextant/contract.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    text = "## Capabilities\n\n| Package |\n|---|\n| `sextant/contract.py` |\n"
    assert uncovered(tmp_path, text) == ["sextant/scores/"]
    assert uncovered(tmp_path, text + "| `sextant/scores/` |\n") == []


def test_namespace_packages_and_root_modules_also_need_rows(tmp_path: Path) -> None:
    (tmp_path / "namespace").mkdir()
    (tmp_path / "namespace/mod.py").write_text("")
    (tmp_path / "module.py").write_text("")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__/x.pyc").write_bytes(b"")
    text = "## Capabilities\n\n| Package |\n|---|\n"
    assert uncovered(tmp_path, text) == ["module.py", "namespace/"]


@pytest.mark.parametrize("row", _rows((ROOT / "docs/landscape.md").read_text()))
def test_every_row_names_an_upstream_source_and_one_verdict(row: dict[str, str]) -> None:
    assert row["Verdict"] in VERDICTS, row
    assert re.search(r"https://\S+", row["Upstream"]), row
    pinned_file = SHA.search(row["Pin"]) and re.search(r"`[^`]+:[^`]+`", row["Source"])
    dated_docs = re.search(r"read 20\d\d-\d\d-\d\d", row["Pin"] + row["Source"])
    assert pinned_file or dated_docs, row
