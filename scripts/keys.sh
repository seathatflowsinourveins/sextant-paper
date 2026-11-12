#!/usr/bin/env bash
# Sextant's key store on a Linux host, encrypted with systemd-creds. Values are never echoed, logged,
# written to the repo or passed on argv. Reach them only through scripts/with-keys.sh. See docs/adr/0028.
# Carried from northstar@b384d298d217d3f8d9aa83356994629536a34795:scripts/keys.sh, its Linux path only.
#   scripts/keys.sh list                     # which known keys are set
#   scripts/keys.sh set NAME                 # prompts (hidden) and stores
#   scripts/keys.sh set NAME --sealed-stdin  # value line + NS_SEALED_TRANSFER_COMPLETE line + EOF
#   scripts/keys.sh rm NAME
set -euo pipefail
# xtrace would print every assignment, which here means a value, so switch it off before any is read.
set +x
set +o xtrace
export -n BASH_XTRACEFD

KNOWN=(
  ANTHROPIC_API_KEY # never injected into an interactive claude, which stays on the owner's subscription
  DEEPSEEK_API_KEY
  EXA_API_KEY
  CONTEXT7_API_KEY
  TYPESAFE_API_KEY
  OMNIROUTE_API_KEY # fresh Sextant router's client key, not an old-campaign lane key
  OMNIROUTE_JWT_SECRET
  OMNIROUTE_API_KEY_SECRET
  OMNIROUTE_STORAGE_ENCRYPTION_KEY
  OMNIROUTE_CLI_SALT
  ALPACA_PAPER_1_API_KEY # one writer per account: no orders from here before a handover the owner confirms
  ALPACA_PAPER_1_SECRET_KEY
  ALPACA_PAPER_2_API_KEY
  ALPACA_PAPER_2_SECRET_KEY
  IB_PAPER_USERNAME
  IB_PAPER_PASSWORD
  IB_PAPER_ACCOUNT_ID
  ALPHA_VANTAGE_API_KEY
  OMNIROUTE_KEY_PI # router-minted by scripts/omni_keys.py, one per GPT runtime; never a Claude model
  OMNIROUTE_KEY_OMP
  OMNIROUTE_KEY_OPENCODE
  OMNIROUTE_KEY_HERMES
  OMNIROUTE_KEY_OPENHANDS
  OMNIROUTE_KEY_CODEX # Codex CLI through sextant-codex, on the same restricted GPT combo
  # T9's box 2, the Mac's Keychain-only names the owner chose to move (sextant#58). Registry and comments
  # carried from northstar@0243050704565abaf3a95f0f09613cb974a331e5:scripts/keys.sh:48-75. Not the two
  # GITHUB_APP_NORTHSTAR_* keys: they stay with northstar (owner, 2026-11-06: "clean sota").
  ANTHROPIC_API_KEY_1 # Console API keys, never injected into an interactive claude
  ANTHROPIC_API_KEY_2
  ANTHROPIC_API_KEY_3
  ANTHROPIC_API_KEY_4
  OPENAI_API_KEY
  META_API_KEY # Meta Model API (Muse)
  DATABENTO_API_KEY
  COHERE_API_KEY # research and eval reranker arms only; never trading or live
  VOYAGE_API_KEY # research and eval reranker arms only; never trading or live
  # The owner's Sharadar subscription (owner, 2026-11-06: "Add my Sharadar key"), for row 4's dated universe.
  # House name for the direct service: sharadar.com/docs/auth and llms.txt, read 2026-11-06.
  SHARADAR_API_KEY # x-api-key on api.sharadar.com/v1.0; research only, never a Nasdaq alias
  # The Sextant GitHub App's private key, one line of base64 (owner, 2026-11-06: "Yes, create a Sextant App").
  # One line because --sealed-stdin carries one value line; the old store did the same for its App keys
  # (northstar@0243050704565abaf3a95f0f09613cb974a331e5:scripts/keys.sh:77-78, GITHUB_APP_*_KEY_B64).
  SEXTANT_GITHUB_APP_KEY_B64
  SEXTANT_LEDGER_DEPLOY_KEY_B64 # the ledger branch's one writer (#102): base64 of an ed25519 deploy key, read only by `just ledger-append`
  # The owner's lasting TradingView MCP login (D-23, 2026-11-10; docs/adr/0021 amendment). One JSON
  # line: the official SDK's tokens and client registration, written and read only by
  # src/sextant/tradingview/stored_login.py through --sealed-stdin. `--forget-login` removes it.
  TRADINGVIEW_MCP_OAUTH
)

[[ "$(uname -s)" == Linux ]] || {
  echo "keys: Linux only so far; a Mac needs the Keychain path in the source this was carried from" >&2
  exit 3
}
# shellcheck source=scripts/key-systemd.sh
source "$(dirname "${BASH_SOURCE[0]}")/key-systemd.sh"
ns_systemd_init || exit 1

printable_ascii() { (
  LC_ALL=C
  [[ "$1" =~ ^[[:print:]]+$ ]]
); }
anthropic_name() { [[ "$1" =~ ^ANTHROPIC_API_KEY(_[0-9]+)?$ ]]; }
# Keys, ids and usernames never contain whitespace, so a paste's stray whitespace is dropped.
trim_name() { [[ "$1" == *_KEY || "$1" == *_ID || "$1" == *USERNAME ]] || anthropic_name "$1"; }

# Prints oat | admin | api | other, ignoring whitespace. A banned prefix counts anywhere in the value.
anthropic_class() {
  case "${1//[[:space:]]/}" in
    *sk-ant-oat*) echo oat ;;
    *sk-ant-admin*) echo admin ;;
    sk-ant-api*) echo api ;;
    *) echo other ;;
  esac
}

# The fixed message for a class, which names the class and never the value.
anthropic_verdict() {
  local what
  case "$2" in
    oat) what="a Claude subscription OAuth token, which belongs only to Claude's own sign-in" ;;
    admin) what="an Anthropic Admin API key, too privileged for inference" ;;
    api) return 0 ;;
    *)
      echo "note: sextant/$1 does not start with sk-ant-api" >&2
      return 0
      ;;
  esac
  echo "refusing: the value is $what; sextant/$1 $3" >&2
  return 1
}

check_name() {
  [[ "$1" =~ ^[A-Z][A-Z0-9_]*$ ]] || {
    echo "NAME must be UPPER_SNAKE_CASE" >&2
    exit 2
  }
  [[ "$1" != *LIVE* ]] || {
    echo "refusing: live credentials are never stored on an agent machine" >&2
    exit 3
  }
}

case "${1:-list}" in
  list)
    for name in "${KNOWN[@]}"; do
      printf '%-8s %s\n' "$(ns_systemd_state "$name")" "$name"
    done
    ;;
  set)
    name="${2:?usage: keys.sh set NAME [--sealed-stdin]}"
    check_name "$name"
    if [[ "${3:-}" == --sealed-stdin ]]; then
      registered=0
      for known in "${KNOWN[@]}"; do [[ "$name" != "$known" ]] || registered=1; done
      ((registered)) || {
        echo "keys: unregistered sealed-transfer name refused" >&2
        exit 3
      }
    fi
    ns_systemd_store "$name" "${3:-}"
    ;;
  rm)
    name="${2:?usage: keys.sh rm NAME}"
    check_name "$name"
    ns_systemd_remove "$name"
    ;;
  *)
    sed -n '2,/^set -/{/^set -/!p;}' "$0"
    exit 2
    ;;
esac
