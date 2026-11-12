#!/usr/bin/env bash
# Run ONE command with stored keys in its environment only, like `op run`.
#   scripts/with-keys.sh --strict -k DEEPSEEK_API_KEY -- dsh ...
#   scripts/with-keys.sh -k ALPACA_PAPER_1_API_KEY:ALPACA_API_KEY -- uv run ...   # rename on inject
# With --strict a missing or empty key is exit 3, and the command does not run.
# Never injects ANTHROPIC_API_KEY into an interactive `claude`, which would move the lead off the
# owner's subscription onto API billing, and never injects anything named *LIVE*.
# Carried from northstar@b384d298d217d3f8d9aa83356994629536a34795:scripts/with-keys.sh, its Linux path only.
set -euo pipefail

keys=()
strict=0
while (($#)); do
  case "$1" in
    -k)
      keys+=("${2:?-k needs a NAME}")
      shift
      ;;
    --strict) strict=1 ;;
    --)
      shift
      break
      ;;
    *) break ;;
  esac
  shift
done
(($#)) || {
  echo "usage: with-keys.sh [--strict] [-k NAME[:ENVNAME]]... -- CMD ARGS..." >&2
  exit 2
}

is_interactive_claude=0
if [[ "$(basename "$1")" == claude ]]; then
  is_interactive_claude=1
  for a in "$@"; do [[ "$a" == -p || "$a" == --print ]] && is_interactive_claude=0; done
fi

[[ "$(uname -s)" == Linux ]] || {
  echo "with-keys: Linux only so far" >&2
  exit 3
}
# shellcheck source=scripts/key-systemd.sh
source "$(dirname "${BASH_SOURCE[0]}")/key-systemd.sh"
ns_systemd_init || exit 3
ns_systemd_inject "$@"
