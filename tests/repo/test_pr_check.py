"""Exercise the local body contract, adopted from the template's Danger rules.

No API call, push, dependency install, reviewer, or diff-size ceiling is involved.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
BODY = (
    '## What changed\nx\n## Manifest row\n"Real row"\n'
    "## Upstream references\na/b@v1 " + "c" * 40 + "\n"
    "## How verified\ny\n## How to revert\nz\n## Harness\nAgent-Family codex\n"
)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/pr-check.sh", tmp_path / "scripts/pr-check.sh")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/manifest.md").write_text(
        "| Practice or pick | Source |\n|---|---|\n| Real row | upstream |\n"
    )
    env = _env()
    for args in (
        ("init", "-q"),
        ("add", "."),
        ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True)
    return tmp_path


def _env() -> dict[str, str]:
    # Explicit environment prevents inherited service wrappers from replacing fixture tools.
    return {
        "PATH": os.defpath,
        "HOME": "/tmp",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _check(checkout: Path, body: str) -> subprocess.CompletedProcess[str]:
    (checkout / "body.md").write_text(body)
    return subprocess.run(
        ["bash", "scripts/pr-check.sh", "body.md"],
        cwd=checkout,
        env=_env(),
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    "source",
    ["a/b@v1 " + "c" * 40, "https://docs.example.org/page, read 2026-11-11", "doi:10.1234/abc"],
)
def test_a_cited_body_passes_without_danger_or_a_review_window(checkout: Path, source: str) -> None:
    body = BODY.replace("a/b@v1 " + "c" * 40, source)
    result = _check(checkout, body + "\n" + "x" * 45001)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "source", ["my idea", "https://docs.example.org/page", "a/b@v1 abc1234", "<!-- fill me -->"]
)
def test_an_uncited_body_is_refused(checkout: Path, source: str) -> None:
    result = _check(checkout, BODY.replace("a/b@v1 " + "c" * 40, source))
    assert result.returncode != 0
    assert "Upstream references" in result.stderr


@pytest.mark.parametrize(
    "marker",
    ["", "Agent-Family <family>", "Agent-Family: codex", "Agent-Family codex\nAgent-Family dsh"],
)
def test_a_missing_or_malformed_author_is_refused(checkout: Path, marker: str) -> None:
    result = _check(checkout, BODY.replace("Agent-Family codex", marker))
    assert result.returncode != 0
    assert "Agent-Family" in result.stderr


def test_a_manifest_row_must_exist_in_head(checkout: Path) -> None:
    (checkout / "docs/manifest.md").write_text("| Missing row | upstream |\n")
    result = _check(checkout, BODY.replace('"Real row"', '"Missing row"'))
    assert result.returncode != 0
    assert "Manifest row" in result.stderr


def test_template_comments_are_not_a_completed_section(checkout: Path) -> None:
    result = _check(checkout, BODY.replace("\ny\n", "\n<!-- tests -->\n"))
    assert result.returncode != 0
    assert "How verified" in result.stderr
