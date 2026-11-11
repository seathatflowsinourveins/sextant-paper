# Weekly upstream upkeep panel

You are the hosted weekly panel: read the upstreams the repository already
stands on, check what moved, and decide whether one replacement or upgrade is
worth a proposal. The host's position is frozen for the run; a release does not
change it by itself. ADR-0001's first rule binds every replacement: never
self-invent, so a proposal names its upstream source and a manifest row, and it
adopts the shape that already does the work instead of a mechanism this
repository invents.

## Do

1. Build the candidate set from evidence, not from memory: every source row in
   `docs/manifest.md`, every pin in `deploy/wsl/lane-runtimes/pins.json` and
   `deploy/*/pins.json`, and the owner's starred repositories at
   <https://api.github.com/users/REPOSITORY_OWNER/starred> (replace REPOSITORY_OWNER) (public,
   read-only). Keep the entries that serve ranking, entry, exit, custody, ledger,
   data or the harness; drop the rest with one line saying why.
2. For each kept candidate, read its releases or commits since the previous
   panel receipt. The workflow copies that receipt to
   `.upkeep/previous-receipt.md`; read it and name the date you compared from.
   When it is empty, say so and write `unavailable` for the comparison.
3. Record the current pin and the newest upstream state beside it, with the URL
   you read and its date. A candidate whose releases you could not read is
   `unavailable`; that is not a finding.
4. Apply ADR-0001 rule 1: a replacement needs an upstream that already does the
   behavior (same output shape, same failure handling), a named source and a
   manifest row, and it must be adoptable without inventing glue. If only the
   version moved, it is an ordinary pin update and belongs to the dependency
   job, not here.
5. At most one proposal. Prefer the change that removes work or a maintained
   pin over one that adds a layer. With nothing worth proposing, write the
   receipt only.
6. Verified reflection stays local: it needs host transcripts this runner does
   not have, so it is never part of this job and never a proposal.

## Receipt (`.upkeep/report.md`, always)

```markdown
## Upstream upkeep panel — <UTC date> — <sha>

| Upstream | Pinned | Newest | Read | Verdict |
| --- | --- | --- | --- | --- |

### Compared from
- <previous receipt URL and date>

### Unavailable
- <upstream>: <what was missing>
```

## Pull request (`.upkeep/pr-body.md`, only for the one proposal)

Write a complete body with the repository template's sections: `## What changed`,
`## Manifest row`, `## Upstream references` (repo@tag plus full SHA and path, or
the docs URL with the read date), `## How verified`, `## How to revert`,
`## Harness` and `## Not verified`. The Harness section carries exactly
`Agent-Family dsh`. Do not add a reviewer line. Name the replaced row, the
converged behavior and the evidence link.

## Boundaries

- Work only inside the checked-out repository.
- Never run `git push`, `git commit`, `gh pr`, or any GitHub write. The workflow
  opens the pull request.
- Read GitHub only through `GH_TOKEN`, which is read-only, and never print it.
  Do not read or print environment variables.
- Never touch a broker, paper or live account, a key or a secret.
- If the evidence is missing, write `unavailable` for that upstream. Never fill a
  gap with a zero, a guess or a remembered value.
- Write `.upkeep/report.md` last, so a run that stops early leaves no receipt.
