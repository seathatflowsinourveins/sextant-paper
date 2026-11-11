@AGENTS.md

You are the lead. Use Claude Code's native features first: skills, subagents, worktrees and saved Workflows.

Between unrelated tasks, send a bare `/clear`; it costs nothing and stops one task's history from being re-read by the next.

[Sextant-specific, replace with your own focus line: Sextant's says to start with its clean-WSL instructions and its design document, and names its trading focus.] Measure each claim.

A PR lands through squash auto-merge only when ci, source and review pass. CI runs on hosted pull_request jobs; the default-branch workflow_run gate supplies API-only Danger and dsh-github review. API-key reviewers run only for dsh authors (Codex by default, Claude when selected) or as one extra quality review on risk or gate paths, with no API reviewer for a Codex author unless it selects Claude. Follow the exact-head witness and bounded landing rule in shared/review-rules.md. Local Codex uses the subscription pool, never OPENAI_API_KEY.

# Compact instructions

- Keep the owner's words quoted in this session, verbatim.
- Keep this session's role and its state-file path.
- Keep peer names and what each one holds.
- Keep open tasks, and what waits on the owner.
- Keep each ruling's file name and SHA-256.
- Keep decisions and why, including rejected alternatives.
- Keep exact paths, commands, signatures, errors and values.
- Drop tool output, fetched pages, finished work and status checks.
