"""docs/manifest.md merges by git's built-in union driver, no row key is ever duplicated, and no
retired row returns.

Source: git/git@v2.56.0 `a018953688f1b10bddf91bff8747068f5f4746a4`:
`Documentation/gitattributes.adoc`, the built-in `union` merge driver: "Run 3-way file level merge
for text files, but take lines from both versions, instead of leaving conflict markers."
"""

import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parents[2]
KNOWN_REPEATS: dict[str, int | None] = {}
RETIRED: tuple[str, ...] = ()


def _keys() -> Counter[str]:
    rows = (ROOT / "docs" / "manifest.md").read_text().splitlines()
    cells = [r.split("|")[1].strip() for r in rows if r.startswith("| ") and not r.startswith("|-")]
    return Counter(cells)


def test_the_manifest_merges_by_union() -> None:
    assert "docs/manifest.md merge=union" in (ROOT / ".gitattributes").read_text().splitlines()


def test_no_row_key_is_duplicated() -> None:
    for key, count in _keys().items():
        if count == 1 or (key in KNOWN_REPEATS and KNOWN_REPEATS[key] in (None, count)):
            continue
        raise AssertionError(f"docs/manifest.md row key repeated {count}x: {key!r}")


def test_no_retired_row_returns() -> None:
    back = [key for key in _keys() if key.startswith(RETIRED)]
    assert not back, f"a union merge from an older base restored retired rows; delete them: {back}"


def test_union_keeps_both_sides_rows(tmp_path: Path) -> None:
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        env = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": "/usr/bin:/bin",
        }
        command = ["git", "-C", str(tmp_path), *args]
        return subprocess.run(command, capture_output=True, text=True, env=env)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (tmp_path / ".gitattributes").write_text("docs/manifest.md merge=union\n")
    (tmp_path / "docs").mkdir()
    table = tmp_path / "docs" / "manifest.md"
    table.write_text("| Practice or pick | Source |\n|---|---|\n| A | a |\n")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    for branch, row in (("one", "| B | b |\n"), ("two", "| C | c |\n")):
        git("switch", "-q", "-c", branch, "main")
        table.write_text(table.read_text() + row)
        git("commit", "-q", "-am", branch)
    git("switch", "-q", "one")
    merged = git("merge", "-q", "--no-edit", "two")
    assert merged.returncode == 0, merged.stderr + merged.stdout
    text = table.read_text()
    assert "| B | b |" in text and "| C | c |" in text and "<<<<<<<" not in text
