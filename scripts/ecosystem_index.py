#!/usr/bin/env python3
"""Build llms.txt, the one-page entry point into Sextant's written record.

Every ADR, lane README, handbook page and research note is listed with a short
description taken from its own first paragraph. The file is generated, so it cannot
drift from the docs: `--check` fails when it does.
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LLMS = ROOT / "llms.txt"
DESCRIPTION_WIDTH = 180


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def _is_metadata(line: str) -> bool:
    return (
        line.startswith(
            (
                "Status:",
                "**Status",
                "- **Status",
                "*Status",
                "- **Date",
                "*Design,",
                "- **Harvested",
                "*Harvested",
                "- **Source",
                "- **Owner",
                "- **Decided",
            )
        )
        or re.match(r"^- \*\*[^*]+:\*\*", line) is not None
    )


def _first_paragraph(text: str) -> str:
    started = False
    paragraph: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not started:
            if stripped.startswith("# "):
                started = True
            continue
        if not stripped:
            if paragraph:
                break
            continue
        if stripped.startswith(("#", "```", "|", "---", "<!--", ">")):
            if paragraph:
                break
            continue
        if not paragraph and _is_metadata(stripped):
            continue
        paragraph.append(stripped)
    if not paragraph:
        return "Sextant documentation."
    joined = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", " ".join(paragraph))
    joined = re.sub(r"\s+", " ", joined).strip()
    return textwrap.shorten(joined, width=DESCRIPTION_WIDTH, placeholder="...")


def _entry(path: Path, title_override: str | None = None) -> str:
    relative = path.relative_to(ROOT).as_posix()
    text = path.read_text(encoding="utf-8")
    title = title_override or _title(text, path.stem.replace("-", " ").replace("_", " "))
    description = _first_paragraph(text)
    return f"- [{title}]({relative}) \u2014 {description}"


def _glob(pattern: str) -> list[Path]:
    return sorted(ROOT.glob(pattern))


def _section(title: str, paths: list[Path]) -> list[str]:
    if not paths:
        return []
    return [f"## {title}", "", *(_entry(path) for path in paths), ""]


def render() -> str:
    core = [
        (ROOT / "AGENTS.md", "Agent rules"),
        (ROOT / "docs" / "start.md", None),
        (ROOT / "docs" / "design.md", "Design"),
        (ROOT / "docs" / "manifest.md", "Manifest"),
        (ROOT / "docs" / "handbook" / "philosophy.md", None),
        (ROOT / "docs" / "handbook" / "practices.md", None),
    ]
    core_entries = [_entry(path, title) for path, title in core if path.is_file()]
    lines = [
        "# Sextant ecosystem index",
        "",
        "The fastest path from a question to the page that answers it. Generated from the",
        "tree by `scripts/ecosystem_index.py`; run `just session-start` to check it.",
        "",
        "## Start here",
        "",
        *core_entries,
        "",
        *_section("Decisions", _glob("docs/adr/*.md")),
        *_section("Lanes", _glob("lanes/*/README.md")),
        *_section("Handbook", _glob("docs/handbook/*.md")),
        *_section("Research", _glob("docs/research/*.md")),
    ]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when llms.txt is stale")
    parser.add_argument("--write", action="store_true", help="rewrite llms.txt")
    args = parser.parse_args(argv)

    expected = render()
    if args.check:
        if not LLMS.is_file():
            print("llms.txt is missing; run scripts/ecosystem_index.py --write", file=sys.stderr)
            return 1
        actual = LLMS.read_text(encoding="utf-8")
        if actual != expected:
            print("llms.txt is stale; run scripts/ecosystem_index.py --write", file=sys.stderr)
            return 1
        return 0
    if args.write:
        LLMS.write_text(expected, encoding="utf-8")
        return 0
    print(expected, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
