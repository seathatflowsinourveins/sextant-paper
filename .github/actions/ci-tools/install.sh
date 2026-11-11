#!/usr/bin/env bash
# Download, verify and install the pinned check tools under RUNNER_TEMP/ci-tools.
# `gh release download` (read 2026-11-09) and the versions/checksums come from the
# caller's env: the values main's .devcontainer/Dockerfile:124-138 already pins.
set -euo pipefail
mkdir -p "$RUNNER_TEMP/ci-tools"
for tool in "$@"; do
  case "$tool" in
    actionlint)
      version="$ACTIONLINT_VERSION"
      sha="$ACTIONLINT_SHA256"
      repo=rhysd/actionlint
      tag="v$version"
      pattern="actionlint_${version}_linux_amd64.tar.gz"
      member=actionlint
      ;;
    shellcheck)
      version="$SHELLCHECK_VERSION"
      sha="$SHELLCHECK_SHA256"
      repo=koalaman/shellcheck
      tag="v$version"
      pattern="shellcheck-v${version}.linux.x86_64.tar.gz"
      member="shellcheck-v${version}/shellcheck"
      ;;
    gitleaks)
      version="$GITLEAKS_VERSION"
      sha="$GITLEAKS_SHA256"
      repo=gitleaks/gitleaks
      tag="v$version"
      pattern="gitleaks_${version}_linux_x64.tar.gz"
      member=gitleaks
      ;;
    just)
      version="$JUST_VERSION"
      sha="$JUST_SHA256"
      repo=casey/just
      tag="$version"
      pattern="just-${version}-x86_64-unknown-linux-musl.tar.gz"
      member=just
      ;;
    *)
      echo "::error::unknown tool $tool"
      exit 1
      ;;
  esac
  archive="$RUNNER_TEMP/$tool.tgz"
  gh release download "$tag" --repo "$repo" --pattern "$pattern" --output "$archive"
  printf '%s  %s\n' "$sha" "$archive" | sha256sum -c -
  if [[ "$tool" == shellcheck ]]; then
    tar -xzf "$archive" -C "$RUNNER_TEMP/ci-tools" --strip-components=1 "$member"
  else
    tar -xzf "$archive" -C "$RUNNER_TEMP/ci-tools" "$member"
  fi
  rm -f "$archive"
done
echo "$RUNNER_TEMP/ci-tools" >> "$GITHUB_PATH"
