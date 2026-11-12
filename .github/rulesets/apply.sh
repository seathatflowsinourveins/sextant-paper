#!/usr/bin/env bash
# Apply .github/rulesets through GitHub's REST API, by name. Needs an administrator's gh login.
# Source: docs.github.com/en/rest/repos/rules, read 2026-11-05.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
repo="${REPO:?set REPO=owner/name}"
rulesets=(.github/rulesets/main.json) # add a ruleset file here to apply it too
for file in "${rulesets[@]}"; do
  name=$(jq -r .name "$file")
  id=$(gh api "repos/$repo/rulesets" --jq ".[] | select(.name == \"$name\") | .id")
  if [[ -n $id ]]; then
    gh api -X PUT "repos/$repo/rulesets/$id" --input "$file" --jq '"updated \(.name)"'
  else
    gh api -X POST "repos/$repo/rulesets" --input "$file" --jq '"created \(.name)"'
  fi
done
