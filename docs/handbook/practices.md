# Practices

> Copied from Sextant's `docs/handbook/practices.md` (see [ORIGINS.md](../../ORIGINS.md)). Its trading sections (Rank, Enter, Protect, Re-enter, Custody and budgets, Sessions) are **[Sextant-specific, omitted]**, each replaced by a one-line note. The Evidence and learning section keeps its generic lines; the Harness section is kept whole except one line on the charting service. Links into Sextant's own ADRs, research notes and archive point to the source repository.

*The working practices that survived the harvest. Each line names the record it comes from; a practice with no source is dropped.*

## Rank [Sextant-specific, omitted]

5 trading-domain practice lines, each citing a Sextant ADR, are not included.

## Enter [Sextant-specific, omitted]

6 trading-domain practice lines, each citing a Sextant ADR, are not included.

## Protect [Sextant-specific, omitted]

6 trading-domain practice lines, each citing a Sextant ADR, are not included.

## Re-enter [Sextant-specific, omitted]

5 trading-domain practice lines, each citing a Sextant ADR, are not included.

## Custody and budgets [Sextant-specific, omitted]

9 trading-domain practice lines, each citing a Sextant ADR, are not included.

## Sessions [Sextant-specific, omitted]

3 trading-domain practice lines, each citing a Sextant ADR, are not included.

## Evidence and learning

> 5 of this section's 9 lines are **[Sextant-specific, omitted]** (trading counterfactuals, merit thresholds and research-package rules); the kept lines are cut at the point where they turn to a trading example.

- Each tunable value has a registry row and source. A sourced value without calibration is an uncalibrated starting value, never measured success. If the value or its authority is missing, keep it unavailable; fixtures supply no default authority. Reviewed changes occur between sessions, and nightly tuning only proposes. [ADR-0018](../adr/0018-evidence-and-learning-loop.md)
- One cumulative trial ledger, tiers never pooled, history never reset, rejected variants kept. [ADR-0018](../adr/0018-evidence-and-learning-loop.md)
- Nightly reflection and tuning write proposals only; a label or a one-day anecdote never adopts a change. A nightly proposal PR carries no registry patch and never calls the registry's reviewed-write path; adoption is a separate change in the consuming runtime. Manual Orca runs skip the scheduler's precheck, so readiness is checked at run entry too. [ADR-0018](../adr/0018-evidence-and-learning-loop.md), [L4 design](../research/2026-11-06-nightly-proposals.md) ([#143](https://github.com/seathatflowsinourveins/sextant-archive/pull/143))
- Read outcomes only as of the cutoff; keep superseded and negative lessons. [ADR-0018](../adr/0018-evidence-and-learning-loop.md)

## Harness

- **[Sextant-specific, omitted]** one line on the charting service's tool inventory (discovery is capability inventory, not authority; unknown tools stay refused before any call).
- One manifest row per pick or practice, with pin, application and `just prove` command; nothing enters without one and a stopped practice is deleted. When merging parallel lane work, retain main's rows and append the lane's new rows at the end of their existing section. Reusing a pin that main already carries is not a new pick. Only a new pick waits `pins.json`'s `release_cooldown_days` (lead ruling 2026-11-09). [ADR-0001](../adr/0001-sextant-foundation.md); lead direction for [PR #70](https://github.com/seathatflowsinourveins/sextant-archive/pull/70), 2026-11-06; [merge lesson](retrieval.md)
- A PR lands through squash auto-merge only when ci, source and review pass. `ci` runs on `pull_request` on GitHub-hosted runners with no secrets. One `workflow_run` gate from the default branch then posts `source` (Danger, API-only) and `review` (dsh through PerryLink/dsh-github; the API-key reviewers run only for a dsh-authored PR, a risk-path PR, or a gate-path PR, which gets one extra second-family review in place of an owner label, by the owner's 2026-11-09 decision; a Codex author adds one only when it selects Claude (D-34, 2026-11-10)). So a PR cannot change its own checks. Review to Google's standard: approve a PR that definitely improves code health even if it isn't perfect, and let facts and data overrule opinion. A finding blocks only as an Always-P0 item, or as a hard-line, custody, money or correctness defect with a failing witness at the exact head. Everything else goes to one follow-up issue per PR. Keep one concern per PR. At most two reviewed commits: if a blocking class recurs, split the PR or ship that path disabled and failing closed, and when a bug class recurs, simplify the design instead of patching instances. A branch-only `just ci` pass does not validate CI's merged tree: retain both branches' registered proofs when resolving conflicts, then test the combined tree. Before turning auto-merge on, check the size budgets on current `main` plus the PR, because a green ci on a stale `refs/pull/N/merge` can still break `main`. [ADR-0001](../adr/0001-sextant-foundation.md); [Google eng-practices](https://github.com/google/eng-practices/blob/3bb3ec25b3b0199f4940b1aa75f0ac5c5753301c/review/reviewer/standard.md) `standard.md:31-32,72,90`, `small-cls.md`, `looking-for.md:46-57`; [Release Toggles](https://martinfowler.com/articles/feature-toggles.html), read 2026-11-09 ([#101](https://github.com/seathatflowsinourveins/sextant-archive/pull/101), [#164](https://github.com/seathatflowsinourveins/sextant-archive/pull/164))
- Converge, don't collide: a setup step edits its target in place rather than skipping once it exists ([#55](https://github.com/seathatflowsinourveins/sextant-archive/pull/55)); configure by directory and marker, never a shared list that every lane appends to ([#92](https://github.com/seathatflowsinourveins/sextant-archive/pull/92)); a memory floor reads MemAvailable, not "free"; write a post body with a quoted heredoc (`<<'EOF'`) and a `__STAMP__` that sed fills from `TZ=America/New_York date`, because an unquoted heredoc runs backticked text; grep a regex-form deny rule by its pattern, not a literal word; and one shared GitHub token is one budget, so no polling loops, and GraphQL reads first ([issue #44](https://github.com/seathatflowsinourveins/sextant-archive/issues/44)).
- Keep always-loaded rules short and enforce boundaries instead of hooks; the only hooks are the secret scan and the skills runtime. [ADR-0001](../adr/0001-sextant-foundation.md)
- Agent sessions run locally. Checks run on GitHub-hosted runners, which are free on public repos. PR code runs only in `ci`, with a read-only token and no secrets, and fork PRs need approval and get no secrets. [ADR-0001](../adr/0001-sextant-foundation.md)
- After a lane worker hands off, AO is the branch's only writer for CI/review fixes. Claiming records ownership only: check out the PR head and confirm its SHA before editing; each writer turn takes ONE consolidated rule delivery from its dispatcher, and rulings never reach a running turn along a relay chain. Evidence: on 2026-11-09, B's cut turn 23 received two differing budget rulings by relay, the lead's 00:06 ruling and the trading lead's Entry 13, until Entry 14 reconciled them. Detect a stalled turn by its terminal's lastOutputAt (`orca terminal show`), and resume it with the runtime's own conversation id. [AO hand-off evidence](../research/2026-11-06-agent-orchestrator.md) **[Sextant-specific tools or lanes named]**
- Run harness proof subprocesses with explicit fixture environments; inherited Bash functions or startup files must not replace fake tools with the running service. [AO proof isolation](../research/2026-11-06-agent-orchestrator.md#offline-proof-isolation)
- Probe a replacement router key before publishing its sealed value; a failed probe keeps the previous sealed bytes and router key. Check both in the regression. Hermes's gateway and sessions keep their launch key, so relaunch its clients and restart its gateway before revoking. [Renewal lesson](omni-keys.md) **[Sextant-specific tools or lanes named]**
- Only the owner signs in, stores external keys and approves/applies live limits. The owner may halt at any time; the sanctioned guard and runner also halt automatically under their recorded causes, and the lead promotes, stages and demotes with a record. API keys are spent only where quality needs them. dsh reviews every PR; Codex or Claude review a dsh-authored or risk-path PR, a Codex author's only when it selects Claude (D-34, 2026-11-10); local Codex uses the OmniRoute pool and never OPENAI_API_KEY (owner, 2026-11-09). [ADR-0020](../adr/0020-live-lane.md) **[Sextant-specific tools or lanes named]**
