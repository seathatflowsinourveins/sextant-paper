#!/usr/bin/env bash
# Adapt deploy/calibration/install.sh at Sextant@214329db6b1b1c9c4c5ac8f6ef4d0b3c74c7b3f8.
# The lead runs this after merge. It writes the two user units and reloads the user manager;
# it enables and starts nothing (deploy/listings/README.md has the lead's commands).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="${SEXTANT_REPO:-$(cd "$here/../.." && pwd)}"
# An unquoted ExecStart splits at whitespace and expands % and $ (systemd.service(5)).
if [[ "$repo" =~ [[:space:]%\$\"\'\\] ]]; then
  echo "install.sh: the checkout path needs systemd quoting; use a plain path" >&2
  exit 2
fi
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"

# Upstream's literal sed replacement; restrict the prefix so the data home stays put.
escaped="${repo//\\/\\\\}"
escaped="${escaped//&/\\&}"
escaped="${escaped//|/\\|}"
for unit in sextant-tradingview-listings.service sextant-tradingview-listings.timer; do
  sed -e "s|%h/sextant/|$escaped/|g" -e "s|%h/sextant\$|$escaped|" \
    "$here/$unit" >"$unit_dir/$unit"
done
systemctl --user daemon-reload
echo "Installed; nothing enabled or started. See deploy/listings/README.md."
