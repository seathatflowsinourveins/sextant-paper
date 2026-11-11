# Hosted upkeep

Three maintenance jobs run on free GitHub-hosted runners from one scheduled
workflow, `.github/workflows/upkeep.yml`. Each job is one headless `dsh` worker in
`.github/workflows/dsh-worker.yml`'s shape: it reads a brief, works in the
checked-out tree with no broker key, and leaves `.upkeep/report.md`. When it has
one coherent change it also leaves `.upkeep/pr-body.md` and the workflow opens a
pull request through a short-lived App token; otherwise the receipt is commented
on the single tracker issue `Hosted upkeep receipts`.

| Cron (UTC) | Brief | What it does |
| --- | --- | --- |
| `0 9 * * *` | `upkeep-models` | ADR-0030 model-freshness detector |
| `17 5 * * *` | `upkeep-dependencies` | Renovate local-platform evidence |
| `25 17 * * 1` | `upkeep-panel` | weekly upstream upkeep panel |

`workflow_dispatch` takes the same three names, so a run can be repeated by hand.

## What stays local

Verified reflection needs the host's session transcripts, so it keeps running on
the host under Claude, not here. The lifecycle sweep is a local Orca automation
too. Neither is part of this workflow.

## Dependency evidence without a token

`renovate.sh` runs Renovate's supported local platform: `--platform=local`,
`--dry-run=lookup`, `--report-type=file`, no branch, no pull request, no
lockfile, and an environment holding only `PATH` and an empty global config. The
hosted job installs the pinned release with `--ignore-scripts`:

- `renovate@44.181.5`
- tag `44.181.5` at `d58ffdb10f533fa86c3fa2c46f4f6b6a72c60f85`
- npm publish `2026-10-24T21:48:02.355Z`, 14 days and 4 hours before the 2026-11-08 check,
  so it passes the repository's 14-day minimum release age
- source: renovatebot/renovate@44.181.5 `lib/modules/platform/local/readme.md`
  and `lib/config/options/index.ts`, read 2026-11-08

`renovate.json` keeps one global `minimumReleaseAge` of 14 days, equal to
`deploy/wsl/lane-runtimes/pins.json`'s `release_cooldown_days`, and disables
major updates plus `nautilus-trader` and `alpaca-py`. The ported config's third
package rule matched `deploy/wsl/lane-runtimes/package.json`, which #288 retired;
it is dropped rather than carried as a rule that matches nothing.

The report and its version record are mode 600 in a mode 700 scratch directory
under the runner's temporary directory. They are read once and deleted; the
receipt carries the numbers that mattered.

## Not verified

No scheduled run has fired yet: the cron triggers are static proof until the
first receipt exists, and a run of this workflow is what turns them into
evidence. The job's pin installs, brief resolution and PR path are proven by
`tests/proofs/test_hosted_upkeep.py`; they are config and fixture evidence, not
a receipt.
