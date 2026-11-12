# Delivery lifecycle

Work on one branch in one Orca worktree. The [landing decision](../adr/0001-sextant-foundation.md) builds on sextant-harness's branch/PR helper and GitHub's ruleset API, with ci as the only merge check.

## Standing authority

The lead decides from pinned upstream evidence and measurement; sign-ins, stored keys, live actions, owner-order custody, Rule 1 exceptions and unrequested destructive work stay with the owner. AGENTS.md holds the hard lines.

A draft is a hold: only the drafting session, lead or owner makes it ready. `just pr` checks the body locally, pushes the branch, opens the PR and arms squash auto-merge. GitHub requires ci; there is no lander or review-key service here. Work tracking uses GitHub Issues.

Run `just ci` before handoff and retain exact commands/results. A real paper start separately requires AGENTS.md's exact-SHA Codex max PASS. Keep the branch/worktree until merged; prove GitHub's MERGED state and matching head before force-deleting a squash-merged branch.

## Adaptation lesson, 2026-11-11

Remove an abandoned workflow and its proofs together, while retaining proofs of still-live CI guarantees. Inspect consumers before deleting files: the session-start doctor still consumes `.github/gate/dsh-profile.json`, even after the gate workflow is removed. Evidence: this adaptation's RED/GREEN landscape and ci-only ruleset runs; the checks live in `tests/repo/test_landscape.py` and `tests/proofs/test_ci_only.py`.
