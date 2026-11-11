#!/usr/bin/env bash
# Reproduce the read-only upstream folder from UPSTREAMS.md's tables: each public GitHub repository
# shallow at its pinned tag, then verified against the table's full commit; `full` rows are then
# unshallowed so `git log` and `git blame` work. No key and no prompt: a private or missing
# repository fails its row instead of asking for a login.
# Based on Sextant's lead clone scripts of 2026-11-11 (clone at a tag with `git clone --depth 1
# --branch`, compare `git rev-parse HEAD`, `git fetch --unshallow --tags` for full history);
# git-clone(1) and git-fetch(1) document those options.
# Usage: bash scripts/clone-upstreams.sh <target-dir>
set -uo pipefail

target="${1:-}"
if [[ -z "$target" ]]; then
  echo "usage: bash scripts/clone-upstreams.sh <target-dir>" >&2
  exit 2
fi
table="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/UPSTREAMS.md"
mkdir -p "$target/candidates" && cd "$target" || exit 2
export GIT_TERMINAL_PROMPT=0

ok=0
bad=0
# Table rows: | dir | owner/repo | tag | 40-hex commit | full or shallow |
while IFS='|' read -r _ dir repo tag commit history _; do
  dir="$(xargs <<<"$dir")" repo="$(xargs <<<"$repo")" tag="$(xargs <<<"$tag")"
  commit="$(xargs <<<"$commit")" history="$(xargs <<<"$history")"
  [[ "$commit" =~ ^[0-9a-f]{40}$ && "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || continue
  if [[ ! -d "$dir/.git" ]]; then
    git clone --quiet --depth 1 --branch "$tag" "https://github.com/$repo.git" "$dir" >/dev/null 2>&1
  fi
  if [[ ! -d "$dir/.git" ]]; then
    echo "FAIL $dir $repo@$tag (clone refused, private or tag missing)"
    bad=$((bad + 1))
    continue
  fi
  head="$(git -C "$dir" rev-parse HEAD)"
  if [[ "$head" != "$commit" ]]; then
    echo "DIFF $dir $repo@$tag head $head expected $commit"
    bad=$((bad + 1))
    continue
  fi
  if [[ "$history" == full && -f "$dir/.git/shallow" ]]; then
    git -C "$dir" fetch --quiet --unshallow --tags origin || { echo "FAIL $dir unshallow"; bad=$((bad + 1)); continue; }
  fi
  echo "OK   $dir $repo@$tag $head $history"
  ok=$((ok + 1))
done < <(grep -E '^\| [A-Za-z0-9_./-]+ \| ' "$table")

echo "ok $ok, not ok $bad"
[[ "$bad" -eq 0 ]]
