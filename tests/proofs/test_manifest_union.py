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
# Row keys main already repeats: the per-section table header, and one row recorded twice
# before this proof.
KNOWN_REPEATS = {"Practice or pick": None, "Paper-search MCP": 2}
# Row keys retired or renamed since the cutover: on the pre-cutover main (695bc7c) or a later gate
# commit, and gone at c149c0a. Union also takes both versions of a hunk where one side deleted or
# renamed a row, so a merge, rebase or cherry-pick from an older base restores it, or keeps its
# old twin beside the renamed row, without repeating a key: #31 restored the runner row the gate
# bootstrap retired, and #109 removed it again. Add a row's key here when you retire or rename it.
RETIRED = (
    "A PR cannot change its own checks. PR code runs on a local ephemeral JIT runner",
    "All three required-review families use local one-job JIT runners",
    "C live main: SIP feed, witness entry and broker-held trail",
    "C receipt diagnostics and durable trial-before-receipt publication",
    "C's opt-in full-universe SIP tap: one socket, tap-only measurement symbols, guarded abort",
    "C's request-rate log line: pacer wiring and data calls, every 60 s",
    "EdgarTools documents and 8-K parsers behind archived SEC bytes",
    "Live Alpaca SIP tick feed for paper arming",
    "One other-family review per PR through gh-aw",
    "Overnight BOATS polling feed for the C lane",
    "Owner-ruled J5 cadence, disabled until landing",
    "Private local ephemeral Actions runners",
    "Read-only one-socket feed tap and capacity gate",
    "Size budgets, 'never bloat': src under 20,800 lines",
    "Size budgets, 'never bloat': src under 21,550 lines",
    "Size budgets, 'never bloat': src under 24,000 lines",
    "The App's pushes activate the required review",
)


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


def test_manifest_records_every_adr_0021_display_pin() -> None:
    manifest = (ROOT / "docs/manifest.md").read_text()
    controller_rows = [
        row
        for row in manifest.splitlines()
        if row.startswith("| TradingView Desktop display controller pins |")
    ]
    assert len(controller_rows) == 1, "Record the complete ADR-0021 Desktop controller roster"
    for repository, commit in (
        ("iliaal/tradingview-mcp@1.4.0", "233f29e180031291dacac4c4cc2736a8dde161be"),
        ("JostQQ/tradingview-mcp@v1.0.0", "89317db92b3b962fd8e89f3ec20462da6e4f2733"),
        ("FerroxLabs/tvcontrol@v2.5.3", "85200201f92967db9e4b63a9251d51fcdfe1f3a8"),
    ):
        assert f"{repository} `{commit}`" in controller_rows[0]
    for repository, commit in (
        (
            "[dgunning/edgartools](https://github.com/dgunning/edgartools)@v5.62.1",
            "9f2ff388c0ad21cfbaba4c7587ba8be7abe6816d",
        ),
        ("gerrymanoim/exchange_calendars@4.13.2", "dbe38b1f6887434bbdd1a7d2df6ff8f1742a048a"),
    ):
        assert f"{repository} `{commit}`" in manifest
    policy = (ROOT / "docs/adr/0021-tradingview-display.md").read_text()
    assert "Lightweight Charts v5.2.1 (`b2ce010e4ad59f6556c9ed60e2b2b30feccdd2d1`)" in policy


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
