# Delivery lifecycle

> Copied from Sextant's `docs/handbook/lifecycle.md` (see [ORIGINS.md](../../ORIGINS.md)). The stage table, review routing, standing authority, feedback, owner-direction, settings and consolidation sections carry over; directory names in them are Sextant's. The protected-modules list, the relaunch runbook and the handoff index are **[Sextant-specific, omitted]**. `LEAD-*.md` names with a SHA-256 are private ruling files, cited by basename and hash only.

One stage, one owner. The 25 pinned skills are shared by dsh and the lane runtimes (`deploy/wsl/lane-runtimes/parity.json`); Claude loads the same 25 from its shelf (`.claude/shelf.lock.json`) and the full compound-engineering plugin beside them. This page routes the rules in [practices](practices.md) and [ADR-0001](../adr/0001-sextant-foundation.md) and holds the delivery process: the stage table, [review routing](#review-routing), and the board, protected-module, owner-direction, settings, relaunch, handoff and consolidation sections below. Each section names its source.

| Stage | dsh | Claude | Gate |
|---|---|---|---|
| Frame | `brainstorming` | `brainstorming` | the question, its non-goals and the owner's words |
| Plan | `writing-plans` | `writing-plans` | the plan is on record before code |
| Isolate | `using-git-worktrees` | `using-git-worktrees` | one task, one Orca worktree |
| Implement | `test-driven-development` | `test-driven-development` | a failing test first |
| Debug | `systematic-debugging` | `systematic-debugging` | root cause before a fix |
| Review | `requesting-code-review` then `receiving-code-review` | the same pair, report-only `ce-code-review` and native `/code-review` pending #180's measurement below | findings answered, or rejected with a reason |
| Verify | `verification-before-completion` | `verification-before-completion` | the command and its output, not a claim |
| PR | `finishing-a-development-branch`, `just pr` | the same | `ci`, `source` and `review` pass (ADR-0001) |
| Feedback | `receiving-code-review`; the `gh` CLI for threads and failed jobs | `ce-resolve-pr-feedback`; the `gh` CLI | every thread resolved or answered |
| Learn | `ce-compound` | `ce-compound` | a lesson in `docs/handbook/` or the append-only lessons |
| Land | squash auto-merge, then Orca worktree removal | the same | GitHub shows MERGED and the head is the PR's head |

## Review routing

- Both families request a review with the Superpowers pair. A review is report-only until the invoker asks for the fix, and receiving one answers every finding or rejects it with a reason.
- Claude adds `ce-code-review`, a report-only pipeline of personas, a validator and a rendered report. It reads its artifact root from `.compound-engineering/config.yaml` (default `docs`), dispatches through the host's subagent and collection semantics, and starts its cross-model peer through compound-engineering's own scripts.
- Claude Code 2.1.321's built-in `/code-review` takes a diff, PR number or branch, an effort level and `--comment` to post findings inline. A Claude-family writer runs it on its branch before `just pr`; a Claude local cross-family reviewer runs it on the branch or PR under review. Default `high`; `xhigh` on `src/sextant/paper/`, `src/sextant/execution/`, `src/sextant/exits/`, `src/sextant/risk/` and `.github/`; `max` only on the owner's word, as the native skill requires.
- Its findings follow the [landing verdict rule](../../.github/workflows/shared/review-rules.md#report) unchanged: only Always-P0 or a concrete failing witness at the exact head blocks; everything else goes to one follow-up issue per PR; from the third reviewed head, only P0 blocks.
- Once #180 lands, compare `/code-review`, `ce-code-review` and the Superpowers pair on the same known-defect set; that measurement decides the Claude Review row's one owner. Until then they run beside each other by the owner's word, not a claim of measured superiority. The lead's first use is once on #197 at `xhigh` with inline findings; afterward this is the reviewers' step, not the lead's.
- Every local review brief names the pinned upstream path and asks whether upstream has moved since the pin in a way that changes the PR. The weekly hosted panel remains the landscape check.
- `ce-code-review` is not copied into dsh. The copy would bind a second owner to compound-engineering's config and peer route, and dsh would fail at a route it does not have. Reopen: compound-engineering publishes a harness-neutral review entry (documented non-Task dispatch and no CE config), then a leg and a row.
- The PR gate is family-blind: one other family reviews, as ADR-0001 routes; live promotion keeps its separate exact-SHA two-family requirement. A session skill outside the 25 is not routed here; a stage's owner is a pinned skill or the native tool in its row.

## Standing authority

The owner, verbatim, 2026-11-09, in the lead's 17:59 EST standing-delegation ruling (SHA-256 `4ab39a83c800866c43416c5ea878a8d7ff3aa8adad811df52163a83686d34ecd`):

> "should we stop all lanes and start a clean sota env with the new era clean sota workflow with every adhrs form sota convergenced repos? and please proceed wiht the sota decision making as long as they are evidance andresearch convergneced sota driven,we can make it as part of our standing rules with prs,/code-review etc also using the sota repos upstream to make sure they land cleanly to be sota aligned at current landscape"

That ruling records the evidence-led delegation, owner-only reservations, draft hold and single lander now in `AGENTS.md`, and the native review step above. GitHub's [changing the stage of a pull request](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/changing-the-stage-of-a-pull-request), read 2026-11-09, supplies the hold: a draft cannot merge until marked ready. Restricting who marks it ready and who merges is the owner's policy, not a GitHub permission claim. The review source is the installed Claude Code 2.1.321 runtime's built-in `code-review` skill; upstream release [anthropics/claude-code@v2.1.321](https://github.com/anthropics/claude-code/tree/a4fcccc70a89df6607e66cc30682d551269d0680), `a4fcccc70a89df6607e66cc30682d551269d0680` (`CHANGELOG.md:3-5` identifies the release), checked 2026-11-09. This adds no plugin or hosted gate.

- Owner, 2026-11-10: the owner's directions are inputs; where pinned upstream evidence or a measurement contradicts one, the lead follows the evidence, records the correction with its sources and tells the owner. The lead orchestrates, rules, reviews landing heads and may build integration work no lane owns, with a cross-family review of its own builds.

## Feedback and learning

The `gh` CLI reads review threads and failed job logs natively ([manual](https://cli.github.com/manual/), read 2026-11-09), so neither family needs a plugin for the feedback stage. A lesson lands with the PR that taught it, in `docs/handbook/` or the append-only `lessons.jsonl`; `ce-compound` is the skill that turns the solved problem into that artifact. A stopped practice is deleted, not left as a fallback.

## Board and worktrees [Sextant-specific board]

A task's card moves to `in-review` when its PR opens and to `completed` when the PR merges; the worker's hand-back or Landing's merge note triggers the move, not a later sweep. The lead refreshes the eight lane comments when a lane's state changes. The owner's attention is the unread dot on the lane plus the tab's name in the lead's message, until bring-to-front is enabled on the desktop side.

Authority: LEAD-1712-board-consolidation.md, SHA-256 `2e83e8e370b2e262c6a8a892e2430892bb6a9904e2a95feff9821d7dd16c8421`.

## Protected modules [Sextant-specific, omitted]

Sextant's list of trading modules that must not be deleted while a plan of record depends on them. The transferable rule: "imported only by tests" is not "unused" while an open plan names the module; a proposal that touches a protected module stops and comes to the lead.

## Owner directions

The owner's directions are recorded as professional engineering requirements, not raw quotes. Each entry carries a D-number. A brief, PR body, ADR or handbook page cites the restated requirement and its D-number. No raw owner wording goes into the repository or a new brief. The working record is `OWNER-DIRECTIONS.md` in the private handoffs. LEAD-2345 and LEAD-2356 still hold the quotes; writers take the tables and sources from them and the requirement text from `OWNER-DIRECTIONS.md`.

Authority: LEAD-2358-msg-owner-directions.md (direction D-12), SHA-256 `4bb0c15bdf85d282b42c1a3defd85a275e1ed2f4551459a734e1a9964f6e10fa`.

## GitHub settings [environment names partly Sextant-specific]

This template ships only `main.json` and the `review` and `hosted-workers` environments; Sextant's `live` and `ledger` rulesets and its `paper` and `live` environments are not included. The paragraph below is Sextant's.

The tracked recipe is `.github/rulesets/apply.sh`. It applies the three ruleset files and the environments recipe through the GitHub REST API. `environments.json` gives the four environments their branch policies (review, hosted-workers and paper allow `main` only; live allows `live` only); the applier creates them empty, so their secrets stay the owner's. `tests/proofs/test_foundation.py` proves the recipe and those policies. The one-time application on 2026-11-09 created the live ruleset and set the four environment policies, and read them back.

Authority: LEAD-2346-msg-245-applied-258-fix.md, SHA-256 `e34d3d817ad14a79a5c627db07ad3d77b889f811b3489b0f8bc07aa32446ded6`; LEAD-FABLE-1939-d5-rulings.md, SHA-256 `21a3f1f1041967bd6037e798e65ecae64648afe297e0a4e455737f2abb54766d`, landed by #245 at `630f106`.

## Relaunch runbook [Sextant-specific, omitted]

Sextant's seven-condition relaunch gate for its role sessions.

## Handoff index [Sextant-specific, omitted]

An index of Sextant's private operational reports.

## Consolidation and change

The change lessons that survive the fold:

- A cap or limit has one source. A cap change greps the tree for every copy of the old value before its PR opens; the single-source cap PR makes every other reader load the value.
- A ruling that narrows a running task needs the worker's own acknowledgement as the first line of the worker's next turn; without one, the task stops.
- A consolidation estimate cites an import list, not a directory.
- An automation in a frozen repository reports; it does not open pull requests.

Authority: LEAD-1630-lessons-addendum.md items 1, 2, 4 and 5, SHA-256 `b86cb3b322318895573afa3bdef3d7991db77b8258fb06002b7f777ff5e2bbd8`; item 3 is in [the review rules](../../.github/workflows/shared/review-rules.md#two-author-families).
