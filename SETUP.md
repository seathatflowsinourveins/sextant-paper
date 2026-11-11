# Setup

What each workflow needs from the repository, by name only, and the order to turn it on. No value appears in this repository. Secrets are entered by the repository owner in the GitHub settings pages; never paste one into a file, a commit, a command line or an issue.

## Order

1. **Repository settings** (Settings, General): allow squash merging, turn on "Allow auto-merge", and keep the default branch `main`. GitHub-hosted runners are free on public repositories (Sextant's ADR-0001); on a private repository the jobs bill Actions minutes.
2. **Actions settings** (Settings, Actions, General): set the default `GITHUB_TOKEN` permissions to read; each job asks for what it needs. Require approval for workflows from outside collaborators, so a fork PR runs only after approval and never with secrets. If you restrict which actions may run, allow every `uses:` line listed below at its pinned commit **before** you merge the workflow that uses it; otherwise the whole workflow fails at startup.
3. **First PR with `ci` alone.** Push the tree on a branch and open a PR. `ci` runs on `pull_request` with no secrets: `lint`, `tests`, `budgets` and `secrets` fan into one check named `ci`. Fix what fails there first (see [ADAPT.md](ADAPT.md)).
4. **Merge the tree to `main`.** The `gate` workflow is triggered by `workflow_run` and always runs the default branch's copy, so it does nothing until the workflows are on `main`. A PR can never change its own gate.
5. **Review secrets** (Settings, Secrets and variables, Actions, Repository secrets): `DEEPSEEK_API_KEY` for the required dsh review. Add `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` only if you use the API quality reviewers (a dsh-authored PR, or the one extra review on a gate or risk path). Use low-limit keys you can rotate.
6. **Ruleset**: run `REPO=<owner>/<name> bash .github/rulesets/apply.sh` with an administrator's `gh` login. It creates or updates the `main` ruleset (no deletion, no force-push, PRs only, squash only, required statuses `ci`, `source` and `review` from the GitHub Actions app, integration id 15368) and the two environments below, with branch policies only. Sextant's `live` and `ledger` rulesets are not included.
7. **Hosted workers** (optional, for the upkeep PRs and dispatched workers): create a GitHub App installed on this repository only, with contents and pull requests read and write (the workflows mint read-only tokens for reading and write tokens only for the PR step; issues go through the built-in `GITHUB_TOKEN`). Store its id as the Actions variable `SEXTANT_APP_ID` and its private key as the secret `SEXTANT_APP_PRIVATE_KEY`; the names are Sextant's, so rename them in both workflows if you like. Put `DEEPSEEK_API_KEY` in the `hosted-workers` environment as well, or keep it at repository level. Put the App's bot login (`<app-slug>[bot]`) in place of `your-github-app-slug[bot]` in `.github/workflows/gate.yml` (two lines), so the API reviewers accept the App's PRs.
8. **Dependabot**: turn on Dependabot version updates; `.github/dependabot.yml` lists Sextant's directories with a 14-day cooldown. Remove the directories this tree does not have.

## Per workflow

| Workflow | Trigger | Secrets and variables, by name | Settings and permissions it relies on | Actions it uses (pinned in the file) |
|---|---|---|---|---|
| `ci.yml` | `pull_request` | none; `github.token` downloads the pinned check tools | read-only token; contents read | `actions/checkout`, `astral-sh/setup-uv`, `actions/setup-node` |
| `gate.yml` | `workflow_run` of `ci`, default branch only | `DEEPSEEK_API_KEY` (dsh review, always); `OPENAI_API_KEY` (Codex quality review, when routed); `ANTHROPIC_API_KEY` (Claude quality review, when routed) | per job: `statuses: write` to post `source` and `review`, `pull-requests: write`, `issues: write` for the one follow-up issue per PR, `checks: write` for dsh | `actions/checkout`, `actions/setup-node`, `actions/github-script`, `astral-sh/setup-uv`, the local `./.github/actions/dsh-github`, `openai/codex-action`, `anthropics/claude-code-action` |
| `upkeep.yml` | three crons and `workflow_dispatch` (owner only) | `DEEPSEEK_API_KEY`, `SEXTANT_APP_PRIVATE_KEY`, variable `SEXTANT_APP_ID`; built-in `GITHUB_TOKEN` | environment `hosted-workers`; `issues: write` for the "Hosted upkeep receipts" issue | `actions/checkout`, `actions/setup-node`, `actions/create-github-app-token`, `peter-evans/create-pull-request` |
| `dsh-worker.yml` | `workflow_dispatch` (owner only, default branch) | `DEEPSEEK_API_KEY`, `SEXTANT_APP_PRIVATE_KEY`, variable `SEXTANT_APP_ID` | environment `hosted-workers` | `actions/checkout`, `actions/setup-node`, `actions/create-github-app-token`, `peter-evans/create-pull-request` |
| `consolidation.yml` | weekly cron and `workflow_dispatch` (owner only) | none | `issues: write` for the report issue | `actions/checkout`, `astral-sh/setup-uv` |
| `dependabot.yml` | Dependabot | none | Dependabot version updates on | none |

Environments (`.github/rulesets/environments.json`): `review` and `hosted-workers`, each limited to `main`. The applier creates them empty; their secrets are the owner's to add.

## Local tools

`just pr` runs `scripts/pr-check.sh`, which needs `danger` at the version pinned in `.devcontainer/agents/package.json` on `PATH` (for example `npm ci --prefix .devcontainer/agents`, then add its `node_modules/.bin`). `just ci` needs `uv`, `node`, `gitleaks`, `shellcheck`, `actionlint` and `just`; CI installs the same versions with the checksums in `.github/workflows/ci.yml`. Run `uv lock` once after adoption and commit `uv.lock`: this tree ships `pyproject.toml` without Sextant's lockfile, and `just sync` runs `uv sync --frozen`, which needs it. The `.githooks/pre-commit` secret scan is opt-in: `git config core.hooksPath .githooks`.
