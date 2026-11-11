#!/usr/bin/env bash
# Apply .github/rulesets through GitHub's REST API, by name. Needs an administrator's gh login.
# Source: docs.github.com/en/rest/repos/rules, read 2026-11-05.
# Environments and branch policies: docs.github.com/en/rest/deployments/environments and
# docs.github.com/en/rest/deployments/branch-policies, read 2026-11-09.
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
# The environment recipe writes branch policies only, so any environment it creates stays empty:
# its secrets, variables and reviewers are the owner's.
recipe=.github/rulesets/environments.json
while IFS= read -r name; do
  printf '{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}' |
    gh api -X PUT "repos/$repo/environments/$name" --input - >/dev/null
  printf 'environment %s set\n' "$name"

  branches=$(jq -r --arg name "$name" '.environments[] | select(.name == $name) | .branches[]' "$recipe")
  policies=$(gh api "repos/$repo/environments/$name/deployment-branch-policies")
  # A policy is reused only when both its name and its type match: a tag named like the branch is stale.
  while IFS=$'\t' read -r id policy_name kind; do
    if [[ $kind != branch ]] || ! grep -Fxq "$policy_name" <<<"$branches"; then
      gh api -X DELETE "repos/$repo/environments/$name/deployment-branch-policies/$id" >/dev/null
      printf 'removed %s from %s\n' "$policy_name" "$name"
    fi
  done < <(jq -r '.branch_policies[] | "\(.id)\t\(.name)\t\(.type)"' <<<"$policies")

  while IFS= read -r branch; do
    if ! jq -e --arg branch "$branch" '.branch_policies | any(.name == $branch and .type == "branch")' \
      <<<"$policies" >/dev/null; then
      gh api -X POST "repos/$repo/environments/$name/deployment-branch-policies" \
        -f name="$branch" -f type=branch >/dev/null
      printf 'allowed %s on %s\n' "$branch" "$name"
    fi
  done <<<"$branches"
done < <(jq -r '.environments[].name' "$recipe")
