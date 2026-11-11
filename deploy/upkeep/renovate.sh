#!/usr/bin/env bash
# Dependency evidence through Renovate's supported local platform: no token, no
# GitHub writer, no branch and no lockfile, just a dry-run report against the
# pinned release. The hosted upkeep job runs it and the brief reads the report.
#
# Sources: renovatebot/renovate@44.181.5
# d58ffdb10f533fa86c3fa2c46f4f6b6a72c60f85 lib/modules/platform/local/readme.md
# (`--platform=local`, `--dry-run=lookup`, `--report-type=file`,
# `--report-path`) and lib/config/options/index.ts (`minimumReleaseAge`,
# `minimumReleaseAgeBehaviour`), read 2026-11-08.
set -Eeuo pipefail

mode="${1:-lookup}"
case "$mode" in
  lookup | extract) ;;
  *)
    echo "usage: $0 [lookup|extract]" >&2
    exit 2
    ;;
esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"

pinned="${RENOVATE_VERSION:-}"
if [[ -z "$pinned" ]]; then
  echo "RENOVATE_VERSION is required: the workflow pins it and this script never guesses" >&2
  exit 2
fi
if ! command -v renovate >/dev/null 2>&1; then
  echo "renovate is not on PATH; the workflow installs the pinned release" >&2
  exit 2
fi

running="$(env -i PATH="$PATH" renovate --version)"
if [[ "$running" != "$pinned" ]]; then
  echo "renovate $running answers while $pinned is pinned" >&2
fi

umask 077
scratch="$(mktemp -d "${TMPDIR:-/tmp}/sextant-renovate.XXXXXXXX")"
chmod 700 "$scratch"
report="$scratch/report.json"
printf '{}\n' >"$scratch/global.json"

# An empty global config keeps the repository's renovate.json authoritative;
# the environment holds only PATH and that config, so no token or proxy setting
# reaches the lookup.
status=0
env -i PATH="$PATH" RENOVATE_CONFIG_FILE="$scratch/global.json" renovate \
  --platform=local \
  --onboarding=false \
  --dry-run="$mode" \
  --report-type=file \
  --report-path="$report" || status=$?

printf '{"running": "%s", "pinned": "%s"}\n' "$running" "$pinned" >"$scratch/renovate-version.json"
chmod 600 "$scratch/renovate-version.json" "$scratch/global.json"

if [[ ! -s "$report" ]]; then
  rm -rf "$scratch"
  echo "renovate produced no report; nothing to compare" >&2
  exit 1
fi

chmod 600 "$report"
# The caller keeps the scratch directory as-is and removes it after reading;
# the path is the only thing printed, so the report can be quoted unedited.
echo "$report"
exit "$status"
