# The ecosystem map

> Copied from Sextant's `docs/handbook/ecosystem.md` (see [ORIGINS.md](../../ORIGINS.md)). The routing rule, the citing rule and the search order carry over. The routing table's rows and **the model mix** name Sextant's records, tools and the owner's model assignments: **[Sextant-specific]**, kept as the worked example. Replace the model names with your own, chosen by the same rule (AGENTS.md, Rule 2).

One page for routing a question to the record that answers it. The generated
[llms.txt](../../llms.txt) lists every decision, lane, handbook page and research note;
this page says which tool to reach for first.

## Session start

Run `just session-start`. It checks the live dsh profile against
`config/dsh/profile.lock.json`, then checks that `llms.txt` is current. Drift stops the
session: fix the profile or the lock, never the check.

## Route a question

| Question | First move | Owner |
| --- | --- | --- |
| What Sextant is for, and the trade lifecycle | `docs/design.md`; rank, enter, protect, exit, custody | Design |
| Why a decision was made | Search `docs/adr`; cite the ADR number | Decision record |
| How a lane behaves or is configured | `lanes/<lane>/README.md`, then `config/` | Lane owner |
| Which pick, version, source or proof is in force | `docs/manifest.md`; `just prove` for its test | Manifest |
| Where a symbol is defined or called | `codegraph_explore` first, then `read` | Code |
| What a previous session concluded | `session_search`; then `session_trace` | Session record |
| A library, SDK, CLI or cloud API | `Context7` docs before guessing | Upstream |
| A paper, article or external claim | `paper-search`, `deepread`, or `Exa`; keep the URL | Source |
| A pull request, issue, check or run | `gh pr view`, `gh issue view`, `gh run view` | GitHub |
| A run, worker or automation | `orca orchestration`; `worker-list` for state | Orca |
| A plugin's admission | `docs/handbook/plugins.md`; no row, no install | Plugin gate |
| A lifecycle gap | `handbook/lifecycle.md`, then the owner above | Delivery stage |
| Which runtime, model and effort a job runs on | the model-mix table below; ADR-0030's second amendment | Harness |

### The model mix [Sextant-specific values]

| Work | Runtime and model | Effort |
| --- | --- | --- |
| Lead: orchestration, rulings | Claude Fable 5.2 | high |
| `/code-review` of protection, custody, gate, hard-line or live-promotion changes | Claude Fable 5.2: the lead runs the skill, or a Fable reviewer session | high; `max` only when the owner asks |
| Hard writer tasks and hard reviews where the situation needs Claude | Claude Opus 5.5 | high; `xhigh` where a sweep shows a gain |
| Role sessions: dispatcher, lander, tracker | Claude Sonnet 5.5 | high |
| Lookups, inventories | Claude Haiku 5.5 or Sonnet 5.5 (the later `.claude/agents/<role>.md` definitions); dsh; Muse | low or medium |
| Writers on well-specified briefs; bulk and delta reviews | GPT through Codex (`gpt-6.1-sol`) and dsh: main workhorses | per LEAD-2345 |
| Independent screens and second-family checks | dsh, Muse, agy (Google) | the runtime's pin |

Fable's share of Claude weekly use is at most one half; the tracker reports the split by model each pass and alerts past 50% or when the pace would exhaust the week before its reset. Opus 5.5 is for work, not relaying. A Claude review in a class this table names needs no special word; it names its model and effort and is time-bounded. Every Agent call and launch names its model and effort, in the command or in the checked-in role definition it starts; a fork inherits Fable, so forks are only for the owner's side questions. Each value is a registry row with its source, uncalibrated until the sweeps report. [ADR-0030](../adr/0030-latest-sota-models.md)'s second amendment carries the owner-directions requirements D-9, D-10 and D-11, the tracker's 22:50-23:50 split, and why each level is uncalibrated.

Since 2026-11-10, a role session starts from its checked-in definition in `.claude/agents/<role>.md` through `python3 scripts/role_launch.py <role>`: the definition carries the model and the first turn, the launcher adds `--name` and, for the four coordination roles, `--autocompact 200000`, and passes no `--model`, `--effort`, `--settings` or `--advisor`. That supersedes the 2026-11-09 role table's where-pinned cells; the model and effort values above do not move.

## The rule behind the table

Every question has one first move and one record owner; a second owner is a duplicate,
not redundancy. The delivery-stage owners are [lifecycle.md](lifecycle.md)'s table, not a
second list here.

## Cite

Every answer names its evidence: `path:line`, a commit or run URL, or a source URL.
"Cite" means the reader can open the same record and check the claim. An uncited claim is
a draft, not a finding.

## Search order

1. The generated index: `llms.txt`.
2. The written record: `docs/`, `lanes/`.
3. The code graph: `codegraph_explore`.
4. Session history: `session_search`.
5. External sources: `Context7`, then the cited paper or page.
6. GitHub and Orca: `gh`, then `orca orchestration`.

If a route is missing, add it here with the same one-owner rule; do not add a second
plugin or a second index.

## Complete B protection reserves [Sextant-specific, omitted]

Review lessons on one paper lane's order-rate reserves (2026-11-10) are trading detail and are not included.
