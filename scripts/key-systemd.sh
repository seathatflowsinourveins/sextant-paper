#!/usr/bin/env bash
# Carried from northstar@b384d298d217d3f8d9aa83356994629536a34795:scripts/key-systemd.sh; only the store's name changed.
# Linux implementation of the keys.sh / with-keys.sh contract (ADR-0063).
# Sourced only on Linux. No plaintext file, privileged decrypt, or unlock fallback.
# upstream checked: systemd/systemd@v259.9:man/systemd-creds.xml;
# src/creds/creds.c; src/shared/{creds-util.c,creds-util.h}; src/basic/fileio.c
# (2026-10-23). The root encryptor atomically replaces its output with a root-owned
# inode, so chown is needed before publishing that ciphertext as the owner.
# keys, strict and is_interactive_claude are supplied by with-keys.sh.
# shellcheck disable=SC2154
set +x
set +a
# Bash manual, BASH_XTRACEFD: unset closes the named fd (possibly stderr); unexport only.
export -n BASH_XTRACEFD
unset ns_key_value ns_key_values

ns_systemd_init() {
  ns_key_uid="$(id -u)" || return 1
  [[ "$ns_key_uid" =~ ^[0-9]+$ && "$ns_key_uid" != 0 ]] || {
    echo "keys: run as the original owner, not root" >&2; return 1;
  }
  [[ "${HOME:-}" == /* && "$HOME" != / ]] || return 1
  ns_key_store="$HOME/.config/sextant/credentials"
}

# Check each directory before traversing it; never follow a link or accept a
# foreign-owned or group/world-writable parent. Only set creates directories.
ns_systemd_dirs() {
  local make="${1:-}" d meta owner mode
  for d in "$HOME" "$HOME/.config" "$HOME/.config/sextant" "$ns_key_store"; do
    [[ ! -L "$d" ]] || return 1
    if [[ ! -e "$d" && "$make" == create && "$d" != "$HOME" ]]; then
      mkdir -m 700 -- "$d" 2>/dev/null || return 1
    fi
    [[ -e "$d" ]] || return 44
    [[ -d "$d" && ! -L "$d" ]] || return 1
    meta="$(stat -c '%u:%a' -- "$d" 2>/dev/null)" || return 1
    owner="${meta%%:*}"; mode="${meta#*:}"
    [[ "$owner" == "$ns_key_uid" && "$mode" =~ ^[0-7]{3,4}$ ]] || return 1
    (( (8#$mode & 0022) == 0 )) || return 1
    if [[ "$d" == "$ns_key_store" ]]; then [[ "$mode" == 700 ]] || return 1; fi
  done
}

# An absent file is allowed only when explicitly requested (set/rm/list).
# Root-owned encryptor output is checked before chown, including its link count.
ns_systemd_file() {
  local f="$1" meta
  [[ ! -L "$f" ]] || return 1
  if [[ ! -e "$f" ]]; then [[ "${2:-}" == absent ]]; return; fi
  [[ -f "$f" ]] || return 1
  meta="$(stat -c '%u:%a:%h' -- "$f" 2>/dev/null)" || return 1
  if [[ "${2:-}" == encrypted ]]; then
    [[ ( "${meta%%:*}" == 0 || "${meta%%:*}" == "$ns_key_uid" ) && "${meta##*:}" == 1 ]]
    return
  fi
  [[ "$meta" == "$ns_key_uid:600:1" ]]
}

ns_systemd_state() {
  local rc=0
  ns_systemd_dirs || rc=$?
  if ((rc == 44)); then echo missing
  elif ((rc != 0)) || ! ns_systemd_file "$ns_key_store/$1" absent; then echo unknown
  elif [[ -f "$ns_key_store/$1" ]]; then echo set
  else echo missing; fi
}

# Plaintext is returned only to the calling shell's command substitution.
ns_systemd_read() {
  ns_systemd_dirs && ns_systemd_file "$ns_key_store/$1" && [[ -s "$ns_key_store/$1" ]] || return 1
  systemd-creds --user --uid="$ns_key_uid" --name="$1" --newline=no --no-ask-password \
    decrypt "$ns_key_store/$1" - 2>/dev/null
}

ns_systemd_store() (
  local name="$1" ns_key_work="" ns_key_frame=""
  unset ns_key_value ns_key_frame
  umask 077
  trap 'if [[ -n "$ns_key_work" ]]; then rm -rf -- "$ns_key_work"; fi' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  ns_systemd_dirs create && ns_systemd_file "$ns_key_store/$name" absent || {
    echo "keys: unsafe credential directory or entry; sextant/$name not changed" >&2; exit 1;
  }
  printf 'Value for sextant/%s (input hidden): ' "$name" >&2
  IFS= read -r -s ns_key_value || [[ -n "$ns_key_value" ]] || true
  printf '\n' >&2
  if [[ "${2:-}" == --sealed-stdin ]]; then
    # EOF alone cannot distinguish a failed source that already wrote bytes. Only the
    # source's successful exit appends this public marker; require it AND EOF before encryption.
    # No extra value-bearing process or plaintext file: reuse the existing hidden read.
    if [[ -t 0 ]] || ! IFS= read -r -s ns_key_frame || [[ "$ns_key_frame" != NS_SEALED_TRANSFER_COMPLETE ]]; then
      unset ns_key_value ns_key_frame
      echo "keys: incomplete sealed stdin; sextant/$name not changed" >&2; exit 1
    fi
    unset ns_key_frame
    if IFS= read -r -s ns_key_frame || [[ -n "$ns_key_frame" ]]; then
      unset ns_key_value ns_key_frame
      echo "keys: trailing sealed stdin; sextant/$name not changed" >&2; exit 1
    fi
    unset ns_key_frame
  fi
  if trim_name "$name"; then ns_key_value="${ns_key_value//[[:space:]]/}"; fi
  [[ -n "${ns_key_value//[[:space:]]/}" ]] && printable_ascii "$ns_key_value" || {
    echo "keys: empty value or non-ASCII/control characters; sextant/$name not changed" >&2; exit 1;
  }
  if anthropic_name "$name"; then
    anthropic_verdict "$name" "$(anthropic_class "$ns_key_value")" "not changed" || exit 1
  fi
  ns_key_work="$(mktemp -d "$ns_key_store/.write.XXXXXXXX")" || exit 1
  if ! printf '%s' "$ns_key_value" | sudo -n systemd-creds --user --uid="$ns_key_uid" \
    --with-key=host --name="$name" encrypt - "$ns_key_work/ciphertext" >/dev/null 2>&1; then
    unset ns_key_value
    echo "keys: systemd encryption failed; sextant/$name not changed" >&2; exit 1
  fi
  unset ns_key_value
  if ! ns_systemd_file "$ns_key_work/ciphertext" encrypted || [[ ! -s "$ns_key_work/ciphertext" ]] \
    || ! sudo -n chown -- "$ns_key_uid" "$ns_key_work/ciphertext" >/dev/null 2>&1 \
    || ! chmod 600 -- "$ns_key_work/ciphertext" \
    || ! ns_systemd_file "$ns_key_work/ciphertext" || [[ ! -s "$ns_key_work/ciphertext" ]] \
    || ! ns_systemd_dirs || ! ns_systemd_file "$ns_key_store/$name" absent \
    || ! mv -fT -- "$ns_key_work/ciphertext" "$ns_key_store/$name"; then
    echo "keys: could not publish ciphertext; sextant/$name not changed" >&2; exit 1
  fi
  echo "stored sextant/$name"
)

ns_systemd_remove() {
  [[ "$1" =~ ^[A-Z][A-Z0-9_]*$ ]] || { echo "NAME must be UPPER_SNAKE_CASE" >&2; return 2; }
  ns_systemd_dirs && ns_systemd_file "$ns_key_store/$1" || {
    echo "keys: missing or unsafe credential entry" >&2; return 1;
  }
  rm -- "$ns_key_store/$1" && echo "removed sextant/$1"
}

ns_systemd_inject() {
  # Keep all values private until every requested decrypt/validation has finished.
  # In particular, no earlier value may enter a later decryptor's environment.
  local spec name envname missing="" ns_key_i
  local ns_key_envnames=()
  ns_key_values=()
  for spec in ${keys[@]+"${keys[@]}"}; do
    name="${spec%%:*}"; envname="${spec#*:}"
    [[ "$name" =~ ^[A-Z][A-Z0-9_]*$ ]] || { echo "bad key name: $name (UPPER_SNAKE_CASE, as stored by keys.sh)" >&2; return 2; }
    [[ "$envname" =~ ^[A-Z_][A-Z0-9_]*$ ]] || { echo "bad env name: $envname" >&2; return 2; }
    [[ "$name" == *LIVE* || "$envname" == *LIVE* ]] && { echo "refusing to inject $name (live credentials are not allowed here)" >&2; return 3; }
    # D-23 (docs/adr/0021): only the TradingView sync's own loader reads its stored sign-in.
    [[ "$name" == TRADINGVIEW_MCP_OAUTH ]] && { echo "refusing to inject $name (read only by the TradingView sync)" >&2; return 3; }
    if [[ ( "$name" == ANTHROPIC_API_KEY || "$envname" == ANTHROPIC_API_KEY ) && $is_interactive_claude == 1 ]]; then
      echo "skipping ANTHROPIC_API_KEY for interactive claude (keeps the lead on your subscription)" >&2; continue
    fi
    if ns_key_value="$(ns_systemd_read "$name")" && [[ -n "$ns_key_value" ]]; then
      ns_key_envnames+=("$envname"); ns_key_values+=("$ns_key_value")
    elif ((strict)); then missing+=" $name"
    else echo "note: $name missing, empty or undecryptable in systemd credentials (scripts/keys.sh set $name)" >&2
    fi
    unset ns_key_value
  done
  if [[ -n "$missing" ]]; then
    unset ns_key_values
    echo "with-keys --strict: missing, empty or undecryptable in systemd credentials:$missing — set each with scripts/keys.sh set NAME; not running $1" >&2
    return 3
  fi
  for ns_key_i in "${!ns_key_envnames[@]}"; do
    export "${ns_key_envnames[$ns_key_i]}=${ns_key_values[$ns_key_i]}"
  done
  unset ns_key_values
  exec "$@"
}
