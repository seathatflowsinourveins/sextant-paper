# Harness template

A reusable agent-development harness, taken from the Sextant project. It brings a working set of rules and the automation that enforces them:

- **Rule 1, never self-invent.** Every PR names a pinned upstream source (a repository at a tag and full commit, a docs page with its read date, or a paper) and a row in `docs/manifest.md`. `dangerfile.js` refuses a PR that does not. [SOURCE-HIERARCHY.md](SOURCE-HIERARCHY.md) says which sources may decide: the official upstream, with every other repository a candidate until it is evaluated.
- **Rule 2, compound and self-maintain.** Lessons land through the PR that taught them. Tunable values are registry rows with a source. Scheduled upkeep jobs propose dependency and model bumps as PRs.
- **A gate that a PR cannot change.** `ci` runs PR code on hosted runners with no secrets. A default-branch `workflow_run` gate then runs Danger through the API and a dsh review (plus an API reviewer from another model family when the route calls for one), and posts the required `source` and `review` statuses. Squash auto-merge lands the PR when `ci`, `source` and `review` all pass.
- **A bounded landing rule.** Only an Always-P0 item or a concrete failing witness at the exact head blocks a PR, and every blocking finding states an executed witness or `static: no witness run`. From the third reviewed head only P0 blocks, and everything else goes to one follow-up issue per PR.

Every file here is either copied from Sextant's `main` with its path kept (names generalised, Sextant-specific passages marked) or is a short index that describes what is here. [ORIGINS.md](ORIGINS.md) lists each file's origin.

## What is here

| Path | What it is |
|---|---|
| `AGENTS.md`, `CLAUDE.md` | The operating rules: Rule 1, Rule 2, landing, generic hard lines |
| `SOURCE-HIERARCHY.md` | Official upstream first; other repositories are candidates until evaluated |
| `docs/handbook/` | Philosophy, practices, delivery lifecycle, the ecosystem map and model mix (Sextant's, with markers) |
| `docs/LESSONS.md` | Lessons that transfer, each with its evidence |
| `docs/manifest.md`, `MANIFEST-TEMPLATE.md` | The manifest seeded with the gate's own rows; the pattern, example rows and the registry-row states |
| `examples/registry/` | One real registry file: `UNCALIBRATED` and `UNAVAILABLE` rows |
| `.github/` | Workflows (`ci`, `gate`, `upkeep`, `dsh-worker`, `consolidation`), the review prompt, rules and rubric, the gate adapter, the dsh-github composite action, the ruleset and its applier, the PR template, Dependabot |
| `dangerfile.js`, `scripts/pr-check.sh`, `justfile` | The source rule, its local run before a push, and `just pr`, `just ci`, `just prove`, `just session-start` |
| `config/dsh/`, `.devcontainer/agents/`, `deploy/` | What the gate and the upkeep jobs install: the locked dsh profile, the agent runtime pins, the retrieval pins and the Renovate lookup |
| `tests/repo/`, `tests/proofs/` | Size budgets, the Danger and pr-check proofs, the gate's proofs, the manifest-union proof |
| `UPSTREAMS.md`, `scripts/clone-upstreams.sh` | The pinned upstream table and a script that clones and verifies it |
| `SETUP.md`, `ADAPT.md` | Secrets and settings by name, in order; the Sextant-specific passages to replace |

## Adopt it in about ten minutes

1. **Copy** into your repository: `.github/`, `dangerfile.js`, `justfile`, `scripts/`, `tests/`, `config/dsh/`, `.devcontainer/agents/`, `deploy/`, `.githooks/`, `.gitattributes`, `pyproject.toml`, `renovate.json`, `AGENTS.md`, `CLAUDE.md`, `SOURCE-HIERARCHY.md` and `docs/`. Merge `pyproject.toml` and `justfile` into your own if you have them.
2. **Adapt** the passages [ADAPT.md](ADAPT.md) lists: at least `RISK` in `.github/gate/gate.cjs`, `AUTHORITY` in `dangerfile.js`, the Always-P0 list and carve-out in `.github/workflows/shared/review-rules.md`, the reviewer models in `.github/workflows/gate.yml`, and the proofs that assert Sextant's shapes. Then run `uv lock` and `python3 scripts/ecosystem_index.py --write`.
3. **Set** what [SETUP.md](SETUP.md) lists, in its order: squash merge and auto-merge, Actions permissions and allowlist, then (after the workflows are on `main`) the review secrets and the ruleset through `REPO=<owner>/<name> bash .github/rulesets/apply.sh`.
4. **First PR through the gate.** Open a branch, fill a body from `.github/pull_request_template.md` (name the manifest row in double quotes, name your source under Upstream references, set `Agent-Family <your family>`), and run `just pr "title" body.md`. `scripts/pr-check.sh` applies the Danger body rules locally before the push, then the PR opens with auto-merge on. It lands when `ci`, `source` and `review` pass.

The gate runs from the default branch, so the very first PR that adds `.github/workflows/gate.yml` gets `ci` only. Merge it, then open a small second PR to see `source` and `review` post.

## What is not included

- **No trading code.** None of Sextant's `src/`, its trading lanes, its strategies, or its trading proofs.
- **No account detail.** No broker account, paper or live slot, price, quantity, symbol, order id, key value or token, and no host name or private path.
- **Not Sextant's host setup:** its WSL distro scripts, local key store, devcontainer image, runtime launchers, monitor stack, charting-service integration and role-session definitions.
- **Not its trading records:** the design document, the trading ADRs, the research notes, the append-only lessons journal, or the rulesets and environments for the `live` and `ledger` branches.

[ORIGINS.md](ORIGINS.md) lists what was left out and why.

## Licence

Not yet chosen; the owner decides. Until a `LICENSE` file is added, no licence is granted. Two vendored pieces keep their own terms: `.github/actions/dsh-github/` carries its upstream `LICENSE`, and the pinned upstreams in [UPSTREAMS.md](UPSTREAMS.md) keep theirs.
