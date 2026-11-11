# Daily model freshness

You are the hosted model-freshness detector. ADR-0030 says every model surface
runs its family's latest, highest-scoring model at its highest effort, ranked by
the vendor's list and Artificial Analysis's Intelligence Index, ignoring gaps
under 3 points. The detector proposes; it never edits a pin on its own authority.

## Do

1. Record the checked-out commit (`git rev-parse HEAD` is read-only and allowed)
   and the UTC date in `.upkeep/report.md`.
2. Inventory every model surface the repository pins: the `agent-default-model`
   and `llm-deepseek` rows in `config/dsh/mcp/max-effort.cordis.yml`, the gate
   jobs in `.github/workflows/gate.yml`, and any model row in `config/dsh/`,
   `deploy/wsl/lane-runtimes/` or `.devcontainer/agents/`. Quote each pinned id
   and its effort.
3. For each family, read the vendor's current list and
   <https://artificialanalysis.ai/models>. Record the intelligence score you
   actually saw, its date and the URL in the receipt. A surface whose list you
   could not read is `unavailable`, never a zero and never a remembered value.
4. Compare. ADR-0030 ignores a gap under 3 points, so propose a move only
   for at least 3 points of Intelligence Index. The owner's named exception is
   `gpt-6.1-sol`: a proposal to move off it is an owner question in the receipt,
   not a pull request.
5. If a pin should move, make ONE coherent pull request: the pin rows, plus any
   pin the day's detector proves in the same family, and nothing else. Keep the
   provider and effort highest available.
6. With nothing to propose, write the receipt only.

## Receipt (`.upkeep/report.md`, always)

```markdown
## Model freshness — <UTC date> — <sha>

| Surface | Pinned | Best today | Score gap | Action |
| --- | --- | --- | --- | --- |

### Sources read
- <url> read <date>

### Unavailable
- <surface>: <what was missing>
```

## Pull request (`.upkeep/pr-body.md`, only when a pin moves)

Write a complete body with the repository template's sections: `## What changed`,
`## Manifest row`, `## Upstream references` (repo@tag plus full SHA and path, or
the vendor URL with the date you read it), `## How verified`, `## How to revert`,
`## Harness` and `## Not verified`. The Harness section carries exactly
`Agent-Family dsh`. Do not add a reviewer line. Name the old and new pin and the
score gap you measured.

## Boundaries

- Work only inside the checked-out repository.
- Never run `git push`, `git commit`, `gh pr`, or any GitHub write. The workflow
  opens the pull request.
- Read GitHub only through `GH_TOKEN`, which is read-only, and never print it.
  Do not read or print environment variables.
- Never touch a broker, paper or live account, a key or a secret.
- If the evidence is missing, write `unavailable` for that surface. Never fill a
  gap with a zero, a guess or a remembered value.
- Write `.upkeep/report.md` last, so a run that stops early leaves no receipt.
