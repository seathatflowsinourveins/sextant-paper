#!/usr/bin/env bash
# Adapt deploy/tradingview/install.sh at Sextant@7a5b42312cc4a10ee9991ae94f914049a3c78b4a.
# The lead installs after review/merge. Render and reload only; never enable or start.
set -euo pipefail

if (($#)); then
  echo "usage: install.sh (SEXTANT_REPO optionally selects the merged checkout)" >&2
  exit 2
fi
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="${SEXTANT_REPO:-$(cd "$here/../.." && pwd)}"
# Same restriction as the existing installer: no systemd quoting or expansion needed.
if [[ "$repo" =~ [[:space:]%\$\"\'\\] ]]; then
  echo "install.sh: the checkout path needs systemd quoting; use a plain path" >&2
  exit 2
fi
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
escaped="${repo//\\/\\\\}"
escaped="${escaped//&/\\&}"
escaped="${escaped//|/\\|}"
for unit in sextant-paper-b-stage1.service sextant-paper-b-test-entry.service sextant-paper-b-stage2.service; do
  sed -e "s|%h/sextant/|$escaped/|g" -e "s|%h/sextant\$|$escaped|" \
    "$here/$unit" >"$unit_dir/$unit"
done
systemctl --user daemon-reload
echo "Paper units written, not enabled or started; see deploy/paper/README.md."
