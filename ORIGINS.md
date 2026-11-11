# Origins

Every file in this tree and where it comes from. Source: Sextant, `main` at `3b59f140036a0e5804b6ed0b50ade30919965079` (2026-11-11). **verbatim** means byte-identical to that blob; **generalised** means copied and then changed only as the note says; **new** files describe what is here.

| File | Origin | Blob at that commit | State | Note |
|---|---|---|---|---|
| `.devcontainer/agents/package-lock.json` | `.devcontainer/agents/package-lock.json` | `fbc1a3d92bd3` | verbatim |  |
| `.devcontainer/agents/package.json` | `.devcontainer/agents/package.json` | `8c7e6f5fb0f4` | verbatim |  |
| `.gitattributes` | `.gitattributes` | `87cdbe5b5384` | verbatim |  |
| `.githooks/pre-commit` | `.githooks/pre-commit` | `ff95d0064ad2` | verbatim |  |
| `.github/actions/ci-tools/install.sh` | `.github/actions/ci-tools/install.sh` | `9b4578a2e53d` | verbatim |  |
| `.github/actions/dsh-github/LICENSE` | `.github/actions/dsh-github/LICENSE` | `261eeb9e9f8b` | verbatim |  |
| `.github/actions/dsh-github/action.yml` | `.github/actions/dsh-github/action.yml` | `52307c2c4444` | verbatim |  |
| `.github/actions/dsh-github/scripts/action-patch.mjs` | `.github/actions/dsh-github/scripts/action-patch.mjs` | `9c642c0dc380` | verbatim |  |
| `.github/actions/dsh-github/scripts/action-post.mjs` | `.github/actions/dsh-github/scripts/action-post.mjs` | `c96c43b4f0ec` | verbatim |  |
| `.github/dependabot.yml` | `.github/dependabot.yml` | `96ad4d7335db` | verbatim |  |
| `.github/dsh-workers/briefs/noop-docs.md` | `.github/dsh-workers/briefs/noop-docs.md` | `15425cdbe13a` | verbatim |  |
| `.github/dsh-workers/briefs/noop-docs.pr.md` | `.github/dsh-workers/briefs/noop-docs.pr.md` | `bec0a8553dd5` | verbatim |  |
| `.github/dsh-workers/briefs/upkeep-dependencies.md` | `.github/dsh-workers/briefs/upkeep-dependencies.md` | `da7405dc869e` | verbatim |  |
| `.github/dsh-workers/briefs/upkeep-models.md` | `.github/dsh-workers/briefs/upkeep-models.md` | `da106934463f` | verbatim |  |
| `.github/dsh-workers/briefs/upkeep-panel.md` | `.github/dsh-workers/briefs/upkeep-panel.md` | `cb840a94433a` | generalised | owner's GitHub handle in a URL replaced by REPOSITORY_OWNER |
| `.github/gate/bundles/api-only/cordis.patch.yml` | `.github/gate/bundles/api-only/cordis.patch.yml` | `e81b5aa348ce` | verbatim |  |
| `.github/gate/bundles/api-only/index.mjs` | `.github/gate/bundles/api-only/index.mjs` | `315442ee2550` | verbatim |  |
| `.github/gate/bundles/api-only/package.json` | `.github/gate/bundles/api-only/package.json` | `377c41fc0d01` | verbatim |  |
| `.github/gate/dsh-profile.json` | `.github/gate/dsh-profile.json` | `cfeba76e2f82` | verbatim |  |
| `.github/gate/gate.cjs` | `.github/gate/gate.cjs` | `f2cb9fbe0252` | verbatim |  |
| `.github/gate/headless-services.cordis.yml` | `.github/gate/headless-services.cordis.yml` | `096ecc11ee38` | verbatim |  |
| `.github/gate/parity-entry.py` | `.github/gate/parity-entry.py` | `98de7cb3d5a0` | verbatim |  |
| `.github/gate/upstream/parity.json` | `.github/gate/upstream/parity.json` | `90df6ad3fe5a` | generalised | a private absolute path in an `authority` string replaced by its basename |
| `.github/gate/upstream/parity.py` | `.github/gate/upstream/parity.py` | `d3d5e60af4d3` | verbatim |  |
| `.github/gate/upstreams.json` | `.github/gate/upstreams.json` | `5be53225290d` | verbatim |  |
| `.github/gate/verdict.schema.json` | `.github/gate/verdict.schema.json` | `c33db6c5a47e` | verbatim |  |
| `.github/pull_request_template.md` | `.github/pull_request_template.md` | `f2c36ad884b8` | verbatim |  |
| `.github/review/rubric.md` | `.github/review/rubric.md` | `85e89cb7eead` | verbatim |  |
| `.github/rulesets/apply.sh` | `.github/rulesets/apply.sh` | `a82311bf6b13` | generalised | repository name from `REPO`; applies `main.json` only |
| `.github/rulesets/environments.json` | `.github/rulesets/environments.json` | `e15adf88bc27` | generalised | only the `review` and `hosted-workers` environments |
| `.github/rulesets/main.json` | `.github/rulesets/main.json` | `03b35dcf8fe5` | verbatim |  |
| `.github/workflows/ci.yml` | `.github/workflows/ci.yml` | `18494687cec6` | verbatim |  |
| `.github/workflows/consolidation.yml` | `.github/workflows/consolidation.yml` | `e1d0191f7692` | verbatim |  |
| `.github/workflows/dsh-worker.yml` | `.github/workflows/dsh-worker.yml` | `46f15b8c145a` | generalised | App token owner and repository from the `github` context |
| `.github/workflows/gate.yml` | `.github/workflows/gate.yml` | `9b68212cf5e7` | generalised | Sextant's App bot login replaced by `your-github-app-slug[bot]` |
| `.github/workflows/review.md` | `.github/workflows/review.md` | `929597938ad2` | verbatim |  |
| `.github/workflows/shared/review-rules.md` | `.github/workflows/shared/review-rules.md` | `e9bbf7dac2a9` | verbatim |  |
| `.github/workflows/upkeep.yml` | `.github/workflows/upkeep.yml` | `b8d269b0a346` | generalised | App token owner and repository from the `github` context |
| `ADAPT.md` | new: index of Sextant-specific passages in the copied files | | new |  |
| `AGENTS.md` | `AGENTS.md` | `7d45f886c00a` | generalised | generic rules kept word for word; Sextant-specific passages marked or replaced by a note; two lines added (the source hierarchy and #573's witness rule) |
| `CLAUDE.md` | `CLAUDE.md` | `8316396de7fa` | generalised | focus line replaced by a marker; a decision number removed |
| `MANIFEST-TEMPLATE.md` | new: index around verbatim rows of `docs/manifest.md` (lines 1-3, 14, 29, 229, 236) and ADR-0018:22 | | new |  |
| `ORIGINS.md` | new: this list | | new |  |
| `README.md` | new: index of this tree | | new |  |
| `SETUP.md` | new: settings and secret names read from the workflows in this tree | | new |  |
| `SOURCE-HIERARCHY.md` | new: a lead ruling of 2026-11-11 (LEAD-0845, private), reworded for a public reader | | new |  |
| `UPSTREAMS.md` | new: the shared upstream folder's README and its clones' commits (`git rev-parse HEAD`), 2026-11-11 | | new |  |
| `config/dsh/bili-ports.cordis.yml` | `config/dsh/bili-ports.cordis.yml` | `b41e68c2002b` | verbatim |  |
| `config/dsh/hard-lines/hard-lines.cordis.yml` | `config/dsh/hard-lines/hard-lines.cordis.yml` | `efdfc4f2f3be` | verbatim |  |
| `config/dsh/hard-lines/hard-lines.mjs` | `config/dsh/hard-lines/hard-lines.mjs` | `48d9def765b5` | verbatim |  |
| `config/dsh/hard-lines/package.json` | `config/dsh/hard-lines/package.json` | `9b8c2b629e59` | verbatim |  |
| `config/dsh/mcp/codegraph.cordis.yml` | `config/dsh/mcp/codegraph.cordis.yml` | `5e6f5e0b1a60` | verbatim |  |
| `config/dsh/mcp/context7.cordis.yml` | `config/dsh/mcp/context7.cordis.yml` | `babd5f1dcb3f` | verbatim |  |
| `config/dsh/mcp/deepwiki.cordis.yml` | `config/dsh/mcp/deepwiki.cordis.yml` | `e392260c3b6c` | verbatim |  |
| `config/dsh/mcp/exa.cordis.yml` | `config/dsh/mcp/exa.cordis.yml` | `c77d352e5be5` | verbatim |  |
| `config/dsh/mcp/max-effort.cordis.yml` | `config/dsh/mcp/max-effort.cordis.yml` | `6eaf557a79ff` | verbatim |  |
| `config/dsh/mcp/package.json` | `config/dsh/mcp/package.json` | `9654d59ffcd3` | verbatim |  |
| `config/dsh/mcp/paper-search.cordis.yml` | `config/dsh/mcp/paper-search.cordis.yml` | `780e90b1432f` | verbatim |  |
| `config/dsh/mcp/playwright.cordis.yml` | `config/dsh/mcp/playwright.cordis.yml` | `22352b13602e` | verbatim |  |
| `config/dsh/mcp/source-tools.cordis.yml` | `config/dsh/mcp/source-tools.cordis.yml` | `e0afd691a4a5` | verbatim |  |
| `config/dsh/mcp/source-tools.mjs` | `config/dsh/mcp/source-tools.mjs` | `9e07c305dc90` | verbatim |  |
| `config/dsh/native-auth.cordis.yml` | `config/dsh/native-auth.cordis.yml` | `2b66b454dfcb` | verbatim |  |
| `config/dsh/native-parallel.cordis.yml` | `config/dsh/native-parallel.cordis.yml` | `20f4beb76a4c` | verbatim |  |
| `config/dsh/native-startup.mjs` | `config/dsh/native-startup.mjs` | `1128a42ce81d` | verbatim |  |
| `config/dsh/openviking.cordis.yml` | `config/dsh/openviking.cordis.yml` | `4fcb46957fdb` | verbatim |  |
| `config/dsh/package.json` | `config/dsh/package.json` | `3e2651edff84` | verbatim |  |
| `config/dsh/profile.lock.json` | `config/dsh/profile.lock.json` | `689d9492fb5d` | verbatim |  |
| `config/dsh/quality/journal.cordis.yml` | `config/dsh/quality/journal.cordis.yml` | `ae47cf9e79d3` | verbatim |  |
| `config/dsh/quality/lsp-actions.cordis.yml` | `config/dsh/quality/lsp-actions.cordis.yml` | `ae48e5816bc6` | verbatim |  |
| `config/dsh/quality/package.json` | `config/dsh/quality/package.json` | `bde917446bfe` | verbatim |  |
| `config/dsh/review-cmd/cordis.patch.yml` | `config/dsh/review-cmd/cordis.patch.yml` | `b96a82419dea` | verbatim |  |
| `config/dsh/review-cmd/index.js` | `config/dsh/review-cmd/index.js` | `802229abfef7` | verbatim |  |
| `config/dsh/review-cmd/package.json` | `config/dsh/review-cmd/package.json` | `6e67229bc348` | verbatim |  |
| `config/dsh/review-cmd/pnpm-lock.yaml` | `config/dsh/review-cmd/pnpm-lock.yaml` | `aefde746d8b4` | verbatim |  |
| `config/dsh/session-search.cordis.yml` | `config/dsh/session-search.cordis.yml` | `bde40c9fdd8a` | verbatim |  |
| `config/dsh/startup-receipt.cordis.yml` | `config/dsh/startup-receipt.cordis.yml` | `b0d2902b7c75` | verbatim |  |
| `dangerfile.js` | `dangerfile.js` | `3b8d706d7e29` | verbatim |  |
| `deploy/openviking/pins.json` | `deploy/openviking/pins.json` | `e76a818b2a04` | verbatim |  |
| `deploy/upkeep/README.md` | `deploy/upkeep/README.md` | `b2df9173690a` | verbatim |  |
| `deploy/upkeep/renovate.sh` | `deploy/upkeep/renovate.sh` | `8a5978ffff0f` | verbatim |  |
| `docs/LESSONS.md` | new: the lead's lesson list of 2026-11-11 with evidence from that day's rulings (private) | | new |  |
| `docs/adr/0001-sextant-foundation.md` | `docs/adr/0001-sextant-foundation.md` | `c03da60eb21c` | generalised | one marker paragraph added under the title; two private path prefixes and a host user id removed |
| `docs/handbook/ecosystem.md` | `docs/handbook/ecosystem.md` | `20253eac2e7a` | generalised | marker added; one paper-lane section omitted with a note |
| `docs/handbook/lifecycle.md` | `docs/handbook/lifecycle.md` | `d1c02f72d56f` | generalised | markers added; three sections omitted with notes; settings note added |
| `docs/handbook/philosophy.md` | `docs/handbook/philosophy.md` | `e909ab4e8a41` | generalised | markers added; lessons-journal paragraph replaced (it named a private path) |
| `docs/handbook/practices.md` | `docs/handbook/practices.md` | `3e7d34b9a0c2` | generalised | six trading sections omitted with notes; Evidence section cut to generic lines; markers |
| `docs/manifest.md` | `docs/manifest.md` lines 1-8, 29 and 222-251 | `e0259dd203c7` | generalised | header, the secret-scan row and the whole Public gate section, verbatim, plus one seed note |
| `examples/registry/parameters.example.json` | `deploy/ranking-node/parameters.json` | `2b5be621158b` | verbatim |  |
| `justfile` | `justfile` | `7fd71d31161d` | generalised | gate recipes only; `session-start` keeps the two checks whose scripts ship here |
| `llms.txt` | generated by `scripts/ecosystem_index.py --write` over this tree | | new |  |
| `pyproject.toml` | `pyproject.toml` | `23c4e3356c8b` | generalised | trading dependencies, extras, uv source and build system removed; tool settings and dev pins kept |
| `renovate.json` | `renovate.json` | `ba9e81e46c63` | verbatim |  |
| `scripts/clone-upstreams.sh` | new: based on the lead's three clone scripts of 2026-11-11 (private scratch), private snapshots removed | | new |  |
| `scripts/dsh_profile_doctor.py` | `scripts/dsh_profile_doctor.py` | `52a1a182a384` | verbatim |  |
| `scripts/ecosystem_index.py` | `scripts/ecosystem_index.py` | `4822dbcd78e5` | verbatim |  |
| `scripts/pr-check.sh` | `scripts/pr-check.sh` | `5a0d45de2450` | verbatim |  |
| `tests/proofs/public-gate.test.cjs` | `tests/proofs/public-gate.test.cjs` | `3f290535904b` | verbatim |  |
| `tests/proofs/test_manifest_union.py` | `tests/proofs/test_manifest_union.py` | `64b3067c39a9` | verbatim |  |
| `tests/proofs/test_public_gate.py` | `tests/proofs/test_public_gate.py` | `5e5b46272693` | verbatim |  |
| `tests/repo/test_budgets.py` | `tests/repo/test_budgets.py` | `4d940ee42bb0` | generalised | the source-cap raise history comment replaced by two lines; values unchanged |
| `tests/repo/test_dangerfile.py` | `tests/repo/test_dangerfile.py` | `6bb990356e78` | verbatim |  |
| `tests/repo/test_pr_check.py` | `tests/repo/test_pr_check.py` | `b98fc2d4f1a6` | verbatim |  |

## Not included, and why

| Left out | Why |
|---|---|
| `src/`, `lanes/`, `research/`, `evals/`, trading proofs under `tests/proofs/` | Sextant's trading code, lanes and their proofs; not part of a reusable harness |
| `docs/design.md`, trading ADRs (0002-0017, 0019-0030), `docs/research/`, the other handbook pages, `docs/handbook/lessons.jsonl` | Sextant's trading design and records; ADR-0018's registry rule is quoted in MANIFEST-TEMPLATE.md |
| `.github/workflows/scoreboard.yml` | builds Sextant's devcontainer image, which is not included |
| `.github/rulesets/live.json`, `ledger.json`; `paper` and `live` environments | Sextant's live-trading and ledger branches |
| `.devcontainer/` (except `agents/`), `deploy/` (except `openviking/pins.json` and `upkeep/`), `.claude/`, `.codex/`, `.mcp.json`, `.qmd/` | Sextant's host, sandbox image, runtime launchers, monitor stack and session settings |
| `scripts/` other than `pr-check.sh`, `dsh_profile_doctor.py`, `ecosystem_index.py` | host doctors, key store and wrappers, role launchers, trading data scripts |
| `tests/proofs/test_foundation.py`, `test_delivery_rules.py`, `test_landing_practices.py`, `test_review_bots.py`, `test_start.py`, `test_workflows.py` | mixed or host-specific proofs: they assert Sextant's settings, documents, App file or WSL setup |
| `uv.lock` | locks Sextant's trading dependencies; run `uv lock` after adoption |
