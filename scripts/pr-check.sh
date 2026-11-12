#!/usr/bin/env bash
# Local-only PR body validation; not a required status check.
# Adopted from sextant-harness@cc156d07e6d8fda8263a7c1323c641aae65765ac:
# dangerfile.js (section/source/manifest rules), .github/gate/gate.cjs (author markers).
# This repository's ci-only policy removes Danger, review routing and the size window.
set -euo pipefail
body_file="${1:-}"
if [[ -z "$body_file" ]]; then
  echo "pr-check: pass the PR body file: bash scripts/pr-check.sh <body.md>" >&2
  exit 1
fi
if [[ "$body_file" != /* ]]; then body_file="$PWD/$body_file"; fi
cd "$(git rev-parse --show-toplevel)"
python3 - "$body_file" <<'PY'
import re
import subprocess
import sys
from pathlib import Path

try:
    body = Path(sys.argv[1]).read_text()
    manifest = subprocess.check_output(["git", "show", "HEAD:docs/manifest.md"], text=True)
except (OSError, subprocess.CalledProcessError) as exc:
    sys.exit(f"pr-check: cannot read body or committed manifest: {exc}")


def section(name):
    match = re.search(r"^## " + re.escape(name) + r"\s*$([\s\S]*?)(?=^## |\Z)", body, re.M)
    return re.sub(r"<!--[\s\S]*?-->", "", match[1]).strip() if match else ""


errors = []
for name in ("What changed", "Manifest row", "Upstream references", "How verified", "How to revert"):
    if not section(name):
        errors.append(f'PR body: "{name}" is empty.')
sources = section("Upstream references")
repo = r"[\w.-]+/[\w.-]+(?:\]\([^)\s]*\))?@\S*[\s\S]*?\b[0-9a-f]{40}\b"
docs = r"https?://\S+[\s\S]*?read 20\d\d-\d\d-\d\d"
paper = r"doi\.org/10\.|\bdoi:10\.|arxiv\.org/abs/|\barXiv:\d"
if not any(re.search(pattern, sources, re.I if pattern == paper else 0)
           for pattern in (repo, docs, paper)):
    errors.append("Upstream references: cite repo@tag and full SHA, docs with read YYYY-MM-DD, or DOI/arXiv.")
named = re.findall(r'"([^"]+)"', section("Manifest row"))
rows = [line for line in manifest.splitlines() if not line.startswith("| Practice or pick |")]
if not any(line.startswith(f"| {name} |") for name in named for line in rows):
    errors.append('Manifest row: name a committed docs/manifest.md row in "double quotes".')
markers = re.findall(r"Agent-Family[^\r\n]*", body)
if len(markers) != 1 or not re.fullmatch(r"Agent-Family (codex|claude|dsh|muse|agy)", markers[0]):
    errors.append("Agent-Family: use exactly one literal author marker with one ASCII space.")
if errors:
    sys.exit("\n".join("pr-check: " + error for error in errors))
print("pr-check: local upstream references, manifest row and Agent-Family pass.")
PY
