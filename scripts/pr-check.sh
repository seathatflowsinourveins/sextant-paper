#!/usr/bin/env bash
# Refuse a push from `just pr` before it leaves the machine: the Danger body rules and the
# review window's raw size. danger/danger-js@14.1.0 `72540533c4f99bad772ba303be0eb4af6eddbdc4`:
# `source/commands/danger-local.ts` (no PR metadata; body absent), `source/platforms/LocalGit.ts`
# (`danger.github` absent), `source/platforms/git/localGetDiff.ts` (three-dot review window);
# `docs/tutorials/fast-feedback.html.md`, read 2026-11-10. Issue #306.
set -euo pipefail

body_file="${1:-}"
if [[ -z "$body_file" ]]; then
  echo "pr-check: pass the PR body file: bash scripts/pr-check.sh <body.md>" >&2
  exit 1
fi
repo_root="$(git rev-parse --show-toplevel)"
if [[ "$body_file" != /* ]]; then
  body_file="$PWD/$body_file"
fi
cd "$repo_root"

pin="$(node -p 'require("./.devcontainer/agents/package.json").dependencies.danger')"
danger_path="$(command -v danger || true)"
if [[ -z "$danger_path" ]]; then
  echo "pr-check: danger ${pin} is not installed. Install the pinned agent runtimes with:" >&2
  echo "  bash deploy/wsl/setup.sh" >&2
  exit 1
fi
version="$("$danger_path" --version 2>/dev/null || true)"
if [[ "$version" != "$pin" ]]; then
  echo "pr-check: danger ${version:-unknown} is not the repository's pinned ${pin}. Install the pinned agent runtimes with:" >&2
  echo "  bash deploy/wsl/setup.sh" >&2
  exit 1
fi

# `danger local` skips the dangerfile when the window is empty, so refuse before the body rules
# could silently not run. `git push` would refuse an empty branch anyway.
if [[ "$(git rev-list --count origin/main..HEAD)" -eq 0 ]]; then
  echo "pr-check: no commits between origin/main and HEAD; nothing to land." >&2
  exit 1
fi

LOCAL_PR_BODY_FILE="$body_file" "$danger_path" local \
  --dangerfile "$repo_root/dangerfile.js" \
  --base origin/main \
  --failOnErrors

readonly RAW_DIFF_LIMIT=45000  # `git diff origin/main...HEAD | wc -c`; raised or removed by the PR that closes #305.
raw_chars="$(git diff origin/main...HEAD | wc -c)"
raw_chars="${raw_chars//[[:space:]]/}"
if (( raw_chars > RAW_DIFF_LIMIT )); then
  echo "pr-check: the review window is ${raw_chars} raw characters, over the ${RAW_DIFF_LIMIT} limit. Split the PR; issue #305 raises or removes the limit in the PR that closes it." >&2
  exit 1
fi
echo "pr-check: the Danger body rules and the ${RAW_DIFF_LIMIT}-character review window pass."
