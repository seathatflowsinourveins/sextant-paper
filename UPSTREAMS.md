# Upstreams

The pinned upstream repositories Sextant's workers read, as one table. `scripts/clone-upstreams.sh` reads this table and reproduces the folder: a shallow clone of each public repository at its tag, then a check that the checked-out commit equals the full commit below. Rows marked `full` are cloned with full history, so `git log` and `git blame` can answer why a line exists. Read the copies only: never edit or commit in them, cite `file:line` at the pin, and read another version with `git -C <dir> show <tag>:<path>` instead of moving a checkout.

Source: Sextant's shared upstream folder as the lead verified it on 2026-11-11 (its README and the three clone scripts behind it); each commit below was read from that folder with `git rev-parse HEAD`. Which kind may decide what is set out in [SOURCE-HIERARCHY.md](SOURCE-HIERARCHY.md).

## Official: the source of truth

The first two are Sextant's trading engine and broker SDK; replace them with your own project's official upstreams. The rest serve the gate, the review path and the hosts the harness runs on, and carry over as they are.

| Directory | Repository | Tag | Commit | History |
|---|---|---|---|---|
| nautilus_trader | nautechsystems/nautilus_trader | v2.0.0rc8 | d53849b216e20f2c122ca01b4dd0da377638b186 | full |
| alpaca-py | alpacahq/alpaca-py | v0.44.0 | cc4cb3b7ba50ae250e621983c2779047fb16bb28 | full |
| mcp-python-sdk | modelcontextprotocol/python-sdk | v2.3.0 | 2118f14f8a19bc158d8a1cf90af58d85d187f849 | shallow |
| exchange_calendars | gerrymanoim/exchange_calendars | 4.13.2 | dbe38b1f6887434bbdd1a7d2df6ff8f1742a048a | shallow |
| requests | psf/requests | v2.34.2 | 6e83187b8feb273ed4c6cdab5efd8d54901dfab3 | shallow |
| lightweight-charts | tradingview/lightweight-charts | v5.2.1 | b2ce010e4ad59f6556c9ed60e2b2b30feccdd2d1 | shallow |
| river | online-ml/river | 0.26.1 | 64285b9dd6c606804753235fe992bcf25b9856ee | shallow |
| danger-js | danger/danger-js | 14.1.0 | 72540533c4f99bad772ba303be0eb4af6eddbdc4 | shallow |
| codex | openai/codex | rust-v0.167.0 | 086ccc833bc9cf01024ad2dd6af7dbd421b6bf9a | shallow |
| codex-action | openai/codex-action | v1.13 | bdf19a4a223ec2549a3e2274a0cf61556bc07675 | shallow |
| claude-code-action | anthropics/claude-code-action | v1.0.263 | 8041f1d17e0e7d2f2cfe35002962e9f26237c0bb | shallow |
| dsh-github | PerryLink/dsh-github | v0.7.27 | 2fc0009cdb84945fdd8066be0a7f98aed9910c9b | shallow |
| deepseek-harness | deepseek-ai/deepseek-harness | dsh-v0.2.2-rc.1 | 66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db | shallow |
| gh-aw | github/gh-aw | v1.0.1 | 0627fdbe4beb5f3b71aa569f8d97150b87f2111a | shallow |
| docker-otel-lgtm | grafana/docker-otel-lgtm | v0.38.0 | 9c57b1400ccdb8920e1a2b6083134cc5ef244a8f | shallow |
| orca | stablyai/orca | v1.4.251 | a162e08b29b2883b02d8a0dfcc99e26c80b2cfe7 | shallow |
| agent-orchestrator | OrchestratorInc/agent-orchestrator | v0.14.10 | 033d597032e448a0ea16c298350e36b099f74106 | shallow |
| just | casey/just | 1.59.0 | 2638e9c0941edb1f409da7426fa7842887656973 | shallow |

One pin differs from the gate: `.github/workflows/gate.yml` runs `anthropics/claude-code-action` at v1.0.264 (`d70510b174a1244958df0b03166a54912811f3e6`), while the folder and the manifest row hold v1.0.263. The workflow pin is the one that runs.

## Candidates: not yet evaluated

Nothing may rest on these alone until each has had a clean end-to-end run and an evaluation, recorded with its commit. The script clones them under `candidates/`.

| Directory | Repository | Tag | Commit | History |
|---|---|---|---|---|
| candidates/iliaal-tradingview-mcp | iliaal/tradingview-mcp | 1.4.0 | 233f29e180031291dacac4c4cc2736a8dde161be | shallow |
| candidates/jostqq-tradingview-mcp | JostQQ/tradingview-mcp | v1.0.0 | 89317db92b3b962fd8e89f3ec20462da6e4f2733 | shallow |
| candidates/ferroxlabs-tvcontrol | FerroxLabs/tvcontrol | v2.5.3 | 85200201f92967db9e4b63a9251d51fcdfe1f3a8 | shallow |

## Not reproduced

- Sextant's folder also holds snapshots of its two private predecessor repositories, which are candidates too. They are private, so they are not listed here and the script does not clone them.
- Its `docs-saved/` holds vendor documentation pages that workers saved, each with its read date. Save your own the same way: the page, its URL and the date you read it.
