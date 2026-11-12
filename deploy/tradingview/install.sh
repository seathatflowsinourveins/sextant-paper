#!/usr/bin/env bash
# Adapt deploy/calibration/install.sh at Sextant@abcf121ebd6a76fae3e720c18ced5df03404d462.
# The lead runs this after merge. By default it installs and enables the producer alone, as the
# calibration installer enables its own unit; the sync service and timer are written only with
# --with-sync, and never enabled here. The memory limits have no measurement, so each is required.
set -euo pipefail

usage() {
  echo "usage: install.sh --producer-memory-max BYTES [--with-sync --sync-memory-max BYTES]" >&2
  exit 2
}

producer_memory="" sync_memory="" with_sync=0
while (($#)); do
  case "$1" in
    --with-sync)
      with_sync=1
      shift
      continue
      ;;
    --producer-memory-max | --sync-memory-max) (($# >= 2)) || usage ;;
    *) usage ;;
  esac
  case "$1" in
    --producer-memory-max) producer_memory="$2" ;;
    --sync-memory-max) sync_memory="$2" ;;
  esac
  shift 2
done

# Refuse rather than quote: each value must need no systemd quoting, specifier or sed escape.
bytes='^[1-9][0-9]*[KMGT]?$'
[[ "$producer_memory" =~ $bytes ]] || usage
if ((with_sync)); then
  [[ "$sync_memory" =~ $bytes ]] || usage
else
  [[ -z "$sync_memory" ]] || usage
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="${SEXTANT_REPO:-$(cd "$here/../.." && pwd)}"
# An unquoted ExecStart splits at whitespace and expands % and $ (systemd.service(5)).
if [[ "$repo" =~ [[:space:]%\$\"\'\\] ]]; then
  echo "install.sh: the checkout path needs systemd quoting; use a plain path" >&2
  exit 2
fi
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"

units=(sextant-tradingview-prestage.service)
if ((with_sync)); then
  units+=(sextant-tradingview-sync.service sextant-tradingview-sync.timer)
fi

# Upstream's literal sed replacement; restrict the prefix so the data home stays put.
escaped="${repo//\\/\\\\}"
escaped="${escaped//&/\\&}"
escaped="${escaped//|/\\|}"
for unit in "${units[@]}"; do
  sed -e "s|%h/sextant/|$escaped/|g" -e "s|%h/sextant\$|$escaped|" \
    -e "s|@PRODUCER_MEMORY_MAX@|$producer_memory|g" \
    -e "s|@SYNC_MEMORY_MAX@|$sync_memory|g" \
    "$here/$unit" >"$unit_dir/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now sextant-tradingview-prestage.service
systemctl --user status sextant-tradingview-prestage.service --no-pager --lines=0 || true
if ((with_sync)); then
  echo "Sync units written, not enabled; see deploy/tradingview/README.md."
fi
