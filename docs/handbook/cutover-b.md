# CUTOVER-B: Paper B handover checklist

The lead's checklist for handing paper protection from the incumbent writer to the engine node, and stepping back safely. Complete each check in order; retain private evidence locally and publish only masked outcomes.

Baseline: sextant-paper `origin/main` at
`455badf3c6031e9b28f845341808cae7f1ae232e`, read 2026-11-11.
All repository paths below refer to that exact main snapshot. Recheck them at the
selected run SHA. This is an operating checklist, not a run receipt or permission.

Sources and verdict: **build on** the existing CUTOVER-B handoff and LEAD-1124
(with its two addenda), LEAD-1857 and LEAD-2233, read 2026-11-11; use this
repository's shipped [paper deployment](../../deploy/paper/README.md),
[display deployment](../../deploy/tradingview/README.md) and admission code below.
The historical branch-rename ruling in LEAD-1857 is not a paper restart instruction.
**Use as is** [systemd.service(5)](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
(`Type=`, `Restart=`, `RestartPreventExitStatus=`), read 2026-11-11.
Existing manifest rows: “Paper deployment transplant”, “Independent REST display
reservation” and “Unattended Paper B stage 2 protection supervision”.

## 1. Freeze the run and the step back

- [ ] Record the clean, merged run SHA and its exact-SHA Codex max review PASS
  covering order sending, leases, key handling and the start path. A merge or
  passing CI alone does not authorize a paper start. Preserve the incumbent's
  approved pinned start kit before stopping it. Source: [AGENTS.md](../../AGENTS.md)
  (Hard lines), [paper README](../../deploy/paper/README.md) (installation and handover).
- [ ] Select one unit: `sextant-paper-b-stage1.service` for attended protection,
  `sextant-paper-b-test-entry.service` for the separately approved attended entry,
  or `sextant-paper-b-stage2.service` for approved Stage 2 protection supervision.
  Stage 1 and Stage 2 carry no attended-entry flag. Stage 2 retains a required
  bounded duration and does not enable ordinary ranked entries. Source:
  [paper README](../../deploy/paper/README.md) and the three units beside it.
- [ ] Confirm the selected unit's required non-secret choices are complete through
  the approved private kit: account identity, approved claim artifact and digest,
  fresh receipt destination, positive run duration, both incumbent lease roots,
  and sourced stream admission. No default duration or fresh substitute lease
  root is authorized here. The strict wrapper supplies credentials to one process;
  do not source a runner environment. Source: [paper README](../../deploy/paper/README.md),
  `src/sextant/paper/node_order.py:249-338,782-812,986-1012`.
- [ ] Install only from the reviewed merged checkout by the paper README's path.
  Its installer renders units and reloads the manager; it starts none of them.
  Keep the selected run command and the incumbent's recovery command in the
  approved private kit. Source: `deploy/paper/install.sh`,
  [paper README](../../deploy/paper/README.md).

## 2. Establish custody before releasing either reservation

- [ ] The owner/lead privately compares venue holdings and open orders with the
  incumbent's evidence before it stops. Start requires a flat account and no
  working house order; do not cancel, sell or resize the owner's orders to make
  admission pass. If that cannot be established, leave the incumbent protecting.
  This checklist supplies no new venue reader. Source: [AGENTS.md](../../AGENTS.md)
  (Custody), `src/sextant/paper/node_order.py:426-470`.
- [ ] Identify the **two node reservations** separately: account writer under the
  incumbent writer's canonical root, and market-data stream under the incumbent
  stream holder's canonical root. Both must be released and then held by the node
  for the run. One shared directory can lose exclusion against an incumbent.
  Source: `src/sextant/paper/node_order.py:986-1012`,
  `tests/proofs/custody/test_b_order_node.py:578-589,678-714`.
- [ ] Determine which producer binary is actually deployed from the approved kit.
  The shipped REST display reservation is a distinct fixed scope under the
  canonical market-data root; it permits coexistence with the sole stream holder
  and grants no stream admission. An old producer can still hold the stream
  reservation. Stop and prove release of that old producer before the attended
  migration; old and new producer locks do not exclude each other. Never run two
  producers. If deployment identity is unknown, hold the handover. Source:
  [display README](../../deploy/tradingview/README.md) (Install, Refusals),
  `src/sextant/tradingview/producer.py:29-69`.
- [ ] The authorized operator stops the incumbent writer and any incumbent stream
  holder, then proves each process has exited and each original lock is free
  using the approved kit. Check by lock identity; do not put the old writer's
  module name in command text after its terminal has been rearmed. Do not delete
  locks or infer release from silence. Source: [restart lesson](LESSONS.md#5-after-enter-keep-the-old-writers-module-out-of-command-text),
  `src/sextant/paper/node_order.py:986-1012`, [AGENTS.md](../../AGENTS.md).

The gap before node admission has no measured protection guarantee. A new holding
in that gap requires refusal and the step back below, not a relaxed flat gate.
Source: `src/sextant/paper/node_order.py:426-470,630-655`.

## 3. Start once and read the admission result

- [ ] The lead starts only the selected unit through the paper README's reviewed
  start path after the handover checks. Use its masked observation path. Manager
  status is not node admission: these are `Type=simple` units. Read
  `order-mode-receipt.json` and `b-lifecycle-journal.jsonl` privately in the chosen
  receipt destination. Source: [paper README](../../deploy/paper/README.md),
  `deploy/paper/sextant-paper-b-stage2.service`,
  `src/sextant/paper/node_order.py:898-942`.
- [ ] Record the receipt's `phase`, `reason`, `gate`, `refused_sends`,
  `journal.errors`, and any `reconciliation` or `dispose_failure` result. Check the
  claim digest against the approved artifact. `starting` is provisional;
  `stopped` is the successful terminal phase, not evidence by itself of complete
  venue protection. Reconcile runtime journal evidence and the venue view.
  Source: `src/sextant/paper/node_order.py:782-819,898-942`.

| Refusal or failure | Required response | Main source |
|---|---|---|
| Missing/relative directory, invalid registries, account choices or duration | Hold before admission; correct through the approved kit, without weakening a guard. A directory refusal writes a receipt only when its destination is valid. | `src/sextant/paper/node_order.py:232-338`; `tests/proofs/custody/test_b_order_node.py:766-846` |
| `ranking_consumer`, `account_mismatch`, `house_prefix`, `claim_list`, `execution_client` | Keep stopped; confirm the intended protection-only composition and private inputs with the lead. | `src/sextant/paper/node_order.py:391-403,852-896` |
| `writer_lease` or `market_data_lease` | Keep stopped; resolve the real incumbent holder under its original root. No alternate root or lock removal. | `src/sextant/paper/node_order.py:986-1012` |
| `not_flat`, `house_order_open`, `holding_outside_claims` | Send nothing; compare venue custody privately and step back. | `src/sextant/paper/node_order.py:426-470` |
| `replacement_unresolved` | Keep stopped; unresolved, missing or cyclic successors are not proof that a replaced order ended. A working successor still refuses, even outside the old prefix. | `src/sextant/paper/node_order.py:446-495`; `tests/proofs/custody/test_b_order_node.py:414-525` |
| `failed`, `aborted_at_start`, reconciliation or disposal failure | Preserve the receipt and journal, page the lead and perform the step back checks. Never equate process exit with safe custody. | `src/sextant/paper/node_order.py:898-953` |

A fully terminal replacement chain can pass; a terminal-looking middle with a
working successor cannot. The old handoff's terminal-status-only check is obsolete.
Source: `src/sextant/paper/node_order.py:446-495`.

## 4. Observe the selected stage

- [ ] For an attended entry, inspect the bounded stop and entry/protection outcome
  separately from the owner-fill witness. No usable ask may leave an attempt
  unsent; unresolved entry at stop is a failure. Source:
  [paper README](../../deploy/paper/README.md) (attended test entry),
  `src/sextant/paper/node_order.py:914-942`,
  [attended-entry practice](practices.md).
- [ ] For Stage 2, each failure restart repeats the same command, both leases and
  every admission check. Exit 2 stays stopped; successful duration completion,
  operator stop and the documented signal stops stay stopped. Start limits may
  leave the unit failed. Keep the sourced supervision settings unchanged; they
  are uncalibrated starting values, not measured availability. Source:
  `deploy/paper/sextant-paper-b-stage2.service`,
  [paper README](../../deploy/paper/README.md) (Unattended stage 2 protection),
  `tests/proofs/test_b_paper_units.py:257-400`.
- [ ] A restart with a holding or working house order refuses. Campaign recovery
  is unavailable; a journal is evidence, not restored state. Escalate a refusal
  instead of forcing another start. Lesson: supervision repeats admission and
  cannot establish continuous protection. Source: [restart lesson](LESSONS.md#6-a-restart-repeats-admission),
  `src/sextant/paper/node_order.py:426-470`, [paper README](../../deploy/paper/README.md).

## 5. Step back and close the custody comparison

- [ ] Stop the selected node unit through the approved kit, including Stage 2 so
  it cannot race the incumbent's return. Confirm process exit and release of both
  original reservations; do not rely on a receipt alone. Preserve final evidence
  before another writer starts. Source: [paper README](../../deploy/paper/README.md),
  `src/sextant/paper/node_order.py:860-942,986-1012`; systemd.service(5) `Restart=`.
- [ ] Compare the venue's holdings and open protection with the final receipt and
  lifecycle journal privately, including unresolved entry and disposal outcomes.
  Do not assume shutdown cancelled protection, or that resting protection is still
  being managed. Source: [paper README](../../deploy/paper/README.md),
  `src/sextant/paper/node_order.py:914-953`, [AGENTS.md](../../AGENTS.md) (Custody).
- [ ] The authorized operator restores the incumbent only by its saved approved
  pinned kit after writer/stream exclusion and custody are established. That kit
  remains an external prerequisite: no incumbent launcher is shipped here. If
  remaining engine protection is treated as an owner reservation by that writer,
  verify coverage and escalate any unmanaged remainder; do not cancel or resize
  it to make the handover fit. Source: the CUTOVER-B handoff section 9,
  [AGENTS.md](../../AGENTS.md), [paper README](../../deploy/paper/README.md).
- [ ] Restore display operation according to the deployed producer's reservation
  model. An old producer must wait for stream release; the migrated REST producer
  uses its separate reservation. Record an accepted tick and measure shared REST
  pacing after migration. Display freshness proves neither writer custody nor
  protection. Source: [display README](../../deploy/tradingview/README.md)
  (Install, Refusals), `src/sextant/tradingview/producer.py:29-69`.
- [ ] Close with masked admission, shutdown, lease-release and custody outcomes,
  plus explicit unresolved items. Real venue reconciliation, protection continuity,
  producer migration state, shared pacing and the incumbent's treatment of remaining
  protection require run evidence; this documentation change proves none of them.
  Source: [design](../design.md), [paper README](../../deploy/paper/README.md),
  [display README](../../deploy/tradingview/README.md).
