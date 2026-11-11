# Adapt before the first PR

The gate files in this tree are Sextant's, copied verbatim so that their proofs still hold (only names were generalised; see [ORIGINS.md](ORIGINS.md)). Some of them carry Sextant's own paths, models, issue numbers and trading rules. This list marks each such passage by file and line, so an adopting repository can replace it on purpose rather than discover it in a failed check. Line numbers are for this tree as assembled.

## Review and routing

| File:line | Sextant-specific passage | Replace with |
|---|---|---|
| `.github/gate/gate.cjs:9` | `RISK`: the paths that get one extra API review. It lists the gate files (keep those) and Sextant's trading directories (`src/sextant/{execution,exits,paper,risk,alpaca}/`, `lanes/{paper,live}/`, `deploy/live/`). | your own risk paths; `tests/proofs/public-gate.test.cjs` asserts the trading prefixes, so change both together |
| `.github/workflows/shared/review-rules.md:8,11` | "Always P0" items for live orders and floats for money | your domain's hard lines (keep the self-invention, secret, gate-weakening and destructive-action items) |
| `.github/workflows/shared/review-rules.md:15-19` | Upstream-first: names Sextant's SDKs and a Sextant file as the example witness | your pinned SDKs; keep the rule |
| `.github/workflows/shared/review-rules.md:29` | the owner's paper carve-out, with Sextant's paper and e2e paths | delete, or your own carve-out; `tests/proofs/test_public_gate.py` (`test_paper_carveout_keeps_only_the_hard_lines_blocking`, `test_paper_and_e2e_carveout_is_finding_level`) asserts its text |
| `.github/workflows/shared/review-rules.md:47` | authority line citing a private ruling file by name and hash | keep as provenance, or cite your own ruling |
| `.github/workflows/gate.yml:105,169-170,235` | reviewer models and effort (`deepseek-flash`; `gpt-6.1-sol` at `max`; `claude-fable-5-2` at `high`), the owner's assignments in Sextant | your families' current top models, by AGENTS.md Rule 2 |
| `.github/workflows/gate.yml:176,231` | `your-github-app-slug[bot]` placeholder (Sextant's App bot was here) | your App's bot login, or delete it |
| `dangerfile.js:81-84` | strategy rule for `src/sextant/strategies/` | your own path, or delete |
| `dangerfile.js:88-` | `AUTHORITY`: Sextant's governed surfaces and the upstreams that may govern each | your surfaces and their official upstreams ([SOURCE-HIERARCHY.md](SOURCE-HIERARCHY.md)); `tests/repo/test_dangerfile.py` and `tests/repo/test_pr_check.py` use Sextant's paths in their cases |

## Local check and proofs

| File:line | Sextant-specific passage | Replace with |
|---|---|---|
| `scripts/pr-check.sh:24,30` | install hint `bash deploy/wsl/setup.sh` (Sextant's host installer, not included) | your install command; `tests/repo/test_pr_check.py:280,289` asserts the hint |
| `scripts/pr-check.sh:6,46,50` | Sextant issue numbers #305 and #306 and the 45,000-character review window | your issue numbers; keep or re-measure the window |
| `tests/repo/test_pr_check.py:349` | asserts the justfile has exactly 104 lines (Sextant's); this tree's justfile is shorter | the new count, or assert only the chained line |
| `tests/repo/test_budgets.py` | Sextant's caps: ADRs, research notes, source lines (37,000), `docs/design.md` (220 lines, file not included, so this test fails until you add one or delete it), lane READMEs, practices | your own caps |
| `tests/proofs/test_manifest_union.py:16,22,60` | `KNOWN_REPEATS`, `RETIRED` and the ADR-0021 display-pin test (file not included) | empty lists; delete that test |
| `scripts/dsh_profile_doctor.py` | checks the live dsh profile at Sextant's default profile path in the user's home | your profile path, or drop the line from `just session-start` |

## dsh profile and hosted workers

| File | Sextant-specific passage | Replace with |
|---|---|---|
| `config/dsh/hard-lines/hard-lines.mjs` | Sextant's hard-line tool guard: its deny rules come from Sextant's `.claude/settings.json` and a generator that is not included, and it refuses writes under `/mnt/wsl` outside one shared folder, a rule for Sextant's WSL host | your own deny list; the guard mechanism (`ctx.tools.guard`) is the upstream's |
| `config/dsh/native-startup.mjs`, `config/dsh/startup-receipt.cordis.yml` | plugin ids and a log folder named after Sextant's predecessor project | rename if you like; behaviour is unchanged |
| `config/dsh/` (whole) and `.github/gate/dsh-profile.json` | Sextant's 24-bundle dsh profile, locked in `config/dsh/profile.lock.json` | keep as is, or trim bundles and re-lock |
| `.github/dsh-workers/briefs/upkeep-*.md` | point at Sextant's `deploy/wsl/lane-runtimes/pins.json` (not included) and its trading roles | your pin files and roles |
| `.github/dsh-workers/briefs/noop-docs*.md` | the first-run smoke test, which mentions Sextant's old `gate` branch | keep as a smoke test |
| `.github/workflows/upkeep.yml`, `dsh-worker.yml` | `SEXTANT_APP_ID` and `SEXTANT_APP_PRIVATE_KEY` names; prompts say "Sextant" | rename consistently, or keep |
| `.github/gate/upstream/parity.py`, `parity.json` | a copy of Sextant's runtime-parity installer; most of it serves Sextant's host, and the gate uses only its dsh helpers | keep (the gate imports it) |
| `.github/dependabot.yml`, `renovate.json` | Sextant's directories, and holds on its engine and broker SDK | your directories and holds |
| `.devcontainer/agents/package.json` | Sextant's agent runtime pins (package name `sextant-agents`); `ci.yml` and `pr-check.sh` read the `danger` pin from it | keep; bump through Dependabot |
| `deploy/openviking/pins.json` | Sextant's retrieval pins; `gate.yml` reads only its `paper-search-mcp` source | keep |

## Docs

`AGENTS.md`, `CLAUDE.md` and the four handbook pages mark their Sextant-specific passages inline with **[Sextant-specific]**. `docs/manifest.md` holds Sextant's gate rows verbatim; their paths and issue numbers are Sextant's. `docs/adr/0001-sextant-foundation.md` keeps its name because `gate.yml`, `AGENTS.md` and the handbook link to it.
