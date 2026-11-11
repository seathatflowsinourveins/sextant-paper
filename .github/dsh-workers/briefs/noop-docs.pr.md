Agent-Family dsh
Review-Family codex

## What changed

This is the first dispatched hosted dsh worker: a deliberate no-op docs job
that adds `docs/handbook/worker-smoke.md` and nothing else. It exercises the
dispatch-to-PR path; the worker workflow itself lands separately.

## Manifest row

"Hosted dsh workers: one dispatched headless job on a free public runner"

## Upstream references

- GitHub, "Triggering a workflow", https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow, read 2026-11-09.
- PerryLink/dsh-github@2fc0009cdb84945fdd8066be0a7f98aed9910c9b: `action.yml:51-62,118-161`, `scripts/action-patch.mjs:49-152`.
- peter-evans/create-pull-request@5f6978faf089d4d20b00c7766989d076bb2fc7f1: `README.md:50-95`.
- actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510: `action.yml:1-40`.

## How verified

The dsh worker ran one headless job from the dispatched brief. The X2 proof
tests cover the owner-only dispatch, the gate-only ref, the minimal permissions,
the key placement, the pins, the parity profile's hard-lines bundle and the
per-slug concurrency. The gate reviews this pull request at its exact head.

## How to revert

Delete `docs/handbook/worker-smoke.md` and close this pull request.
