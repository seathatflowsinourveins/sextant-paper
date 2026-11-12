# Setup

The repository lands through squash auto-merge with `ci` as its only required status check. CI runs unprivileged pull_request code on GitHub-hosted runners, with a read-only token and no repository secrets. Real paper runs follow AGENTS.md's separate exact-SHA review rule.

1. Allow squash merging, enable auto-merge and keep `main` as the default branch.
2. Set default Actions token permissions to read and require approval from all external contributors. Each ci job asks only for contents read; the built-in token downloads pinned tools.
3. Before merging, allow these exact Actions commits. These are every distinct `uses:` in the sole remaining workflow, `.github/workflows/ci.yml`:

| Action | Pinned SHA |
|---|---|
| actions/checkout | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| astral-sh/setup-uv | `07a047e608db84b3d8c06cd9a7c7dcc9c355c18e` |
| actions/setup-node | `949feb2413d6458794dcd2491c4babbbce0c15c1` |

4. The administrator applies `.github/rulesets/main.json` with `REPO=owner/name bash .github/rulesets/apply.sh`. It protects the default branch against deletion and force-push, requires a PR, allows squash only and requires only the `ci` check from GitHub Actions (integration id 15368). No review or hosted-worker environment is created. This adaptation prepares the recipe; it does not apply remote settings.
5. Enable Dependabot for the directories in `.github/dependabot.yml`; it proposes changes through PRs. Broker/engine pins remain held for the lead's measured import decision.

No review API keys, GitHub App keys or other review secrets are needed. CI must not receive broker credentials.

## Local tools

Install `uv`, Python 3.13, Node, `just`, `gitleaks`, `shellcheck` and `actionlint`; CI's tool versions/checksums remain in ci.yml and its installer. `npm ci --prefix .devcontainer/agents --ignore-scripts` installs the pinned node tools; add its node_modules/.bin directory to PATH for pyright. `uv.lock` is committed; `just sync` uses `uv sync --frozen`.

`just session-start` retains the template's live dsh profile parity check and generated index check. `config/dsh/` and `.github/gate/dsh-profile.json` are retained as session-profile metadata used by that check; there is no gate workflow. The host's profile must match the lock. An adopting host supplies the matching profile rather than bypassing the check.

`just ci` runs the complete local suite. `just prove` runs the retained host proofs. `scripts/pr-check.sh BODY` needs only Bash, Python and Git; it checks the body and committed manifest, with no Danger installation or diff-size window. `.githooks/pre-commit` remains the optional local secret scan (`git config core.hooksPath .githooks`).

Sources: template cc156d07e6d8fda8263a7c1323c641aae65765ac; [GitHub rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets), read 2026-11-11.
