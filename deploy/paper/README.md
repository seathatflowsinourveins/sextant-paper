# Paper node units

Trade step: custody and protection, then recorded paper evidence. The two attended user units
run the existing node entry points once, bounded by the node's own `--duration-s`.
They use `Restart=no`, with no timer or install target: a failure or normal stop
needs a new attended start. Paper B's attended
test entry is its own unit beside the protection-only stage 1. Stage 2 adds a separate
protection-only unit with bounded failure restarts (below).

The lead installs and starts only after the separate max gate, landing, and the
owner-confirmed writer/stream handover. These files grant no trading authority.
Keep every pre-check and post-check in the approved run kits. Do not retry a denied
shell start command; use this reviewed unit path after landing.

1. From the verified merged checkout, the lead runs `bash deploy/paper/install.sh`.
   Like the TradingView installer, this renders the checkout prefix into the user
   unit directory and reloads the manager. It never enables or starts anything,
   reads a parameter file, or touches keys. Rerunning replaces all three unit files.
2. Create `$HOME/.config/sextant/paper/` and copy each needed `.params.example` there,
   dropping `.example`. Complete its blank values locally from the approved kit.
   These are required **non-secret** UTF-8 systemd assignment files, never `*.env`:
   no credential, full account identity, shell command or free-form flag list belongs
   in them. Keys stay in ADR-0028's store and enter one process through strict
   `with-keys.sh` using the fixed names in the unit. Do not source `runner.env`.
3. Use absolute paths; file values do not expand `~`, `$HOME`, other variables or
   `%h`. Quote a value with spaces using systemd.exec(5)'s assignment syntax.
   `${NAME}` in ExecStart passes the entire value as one argument, never as shell
   code. No run duration or private path is supplied by the checked-in example.
4. For B, supply the verified account-config **path**, claim list, fresh receipt
   directory, positive duration, sourced stream count/statement and non-secret
   expected-account suffix. Set `B_WRITER_LEASE_ROOT` to the old writer's existing
   locks directory and `B_STREAM_LEASE_ROOT` to the producer's existing leases
   directory, as the kit specifies. Never choose fresh per-run roots: these must
   exclude each current holder in its own location. Receipt-dir contains the receipt
   and lifecycle journal. Campaign recovery is unavailable; restarting requires the
   node's existing flat-start checks. Clear any stale full expected-account ID in
   the manager's environment before using a suffix; a full identity takes precedence.

After the approved kit's handover/pre-checks, the lead starts **one** selected unit:
`systemctl --user start sextant-paper-b-stage1.service`.
Watch with `systemctl --user status <unit> --no-pager` and
`journalctl --user -u <unit> -f`. A successful systemctl start alone does not prove
node admission; read the starting/refused receipt and follow the kit's checks.
Stop early with `systemctl --user stop <unit>`; otherwise the node's duration stops it.

B writes `order-mode-receipt.json` and `b-lifecycle-journal.jsonl` in `B_RECEIPT_DIR`.
A missing key exits 3; parser/start
refusals remain the node's own. Neither a stop nor a receipt establishes venue-flat custody: perform
the kit's venue and lease post-checks before another writer starts.

## Paper B attended test entry

Stage 1 remains protection-only. The separately selected
`sextant-paper-b-test-entry.service` adds the value-taking `--attended-test-entry`
option with one instrument argument from `B_ATTENDED_TEST_INSTRUMENT`.
Copy `paper-b-test-entry.params.example` to the same local parameter directory as
`paper-b-test-entry.params`; complete every stage 1 choice plus the validated
claim-list instrument. Keep the existing holder-specific writer and stream lease
roots and choose a fresh receipt directory. There is no literal price or sizing
input in this file: the node's quote and reviewed registry own those choices.

After T1's node option and this unit have both landed, the separate max gate,
approved kit pre-checks and confirmed handover, the lead selects this unit with
`systemctl --user start sextant-paper-b-test-entry.service`. Watch status and the
journal and stop early using the commands above with this unit name. The node's
bounded stop handles its own test-entry remainder; read the same B receipt and
journal under `B_RECEIPT_DIR` and perform the kit's venue and lease post-checks.
This run does not replace the separately recorded owner-fill witness.

The parameter file is required, without systemd's optional-file prefix. Missing
or incomplete choices must refuse before node admission; an empty instrument
remains one empty argument for the node to reject. This unit requires T1's parser
and admission validation; it cannot run on the earlier protection-only parser.
It is never enabled or started by the installer and never restarts itself.

Lesson: retain a separately required parameter file and fixed option for an
attended entry, so protection-only starts cannot acquire entry intent through an
optional argument list. Preserve that separation in the unit-delta proof.

## Unattended stage 2 protection

After the separate Codex max writer-start gate, landing and confirmed writer/stream
handover, the lead may select `sextant-paper-b-stage2.service`. Complete the required
`paper-b-stage2.params` from its blank example using the approved stage 2 run kit.
The strict key-by-name wrapper, fixed node flags, both incumbent lease roots and
all node admission checks are **identical to stage 1**. There is no attended-entry
flag, new protection behavior, recovered campaign state or free-form argument list.
The registry keeps ordinary entries off. This unit is not an installation receipt.

The lead starts with `systemctl --user start sextant-paper-b-stage2.service`, watches
`journalctl --user -u sextant-paper-b-stage2.service -f` through the run kit's account
mask, and stops with `systemctl --user stop sextant-paper-b-stage2.service`.
Read `B_RECEIPT_DIR/order-mode-receipt.json` and `b-lifecycle-journal.jsonl` and perform
the same venue/lease checks as stage 1. Manager status alone never proves admission.

`Restart=on-failure` repeats the **same ExecStart**, including both lease exclusions,
account identity, registry and reconciled flat-start checks. `RestartPreventExitStatus=2`
keeps every guard refusal stopped. A held position or working house order after a
crash therefore refuses admission; a restart is no promise of continuous protection.
An exit 0 at the explicit duration, SIGINT/SIGTERM or operator stop stays stopped.
Runtime exceptions the node maps to exit 2 also stay stopped. Read the refusal or
failure receipt and page the lead; do not edit a gate, force a retry, remove a lock
or cancel any resting protection. Stage 2 still has an explicit bounded duration;
it is neither enabled at login nor scheduled, and the installer never starts it.

The deployment registry [parameters.json](parameters.json) records `RestartSec=100ms`,
`StartLimitIntervalSec=10s` and `StartLimitBurst=5`, explicitly reusing upstream's
manager defaults as **UNCALIBRATED starting values**. Both manual and automatic starts
count toward the limit; exhausting it leaves the unit failed and needs lead review.
No retry value is a measured recovery or availability result. The unit pins these
values rather than inheriting local manager overrides; changes require review.

Sources: **BUILD ON** #661, Sextant@`7252b0f5c7ccdb416356aaddd9b53fa9a33bd405`
`deploy/paper/sextant-paper-b-stage1.service:1-13`, `paper-b-stage1.params.example:1-15`
and `tests/proofs/test_paper_units.py:34-138` (strict command, parameters and offline
renderer); **USE AS IS** [systemd.service(5)](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
`ExecStart=`, `Restart=`, `RestartSec=`, `RestartPreventExitStatus=`,
[systemd.unit(5)](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html)
`StartLimitIntervalSec=`/`StartLimitBurst=`, and
[systemd-system.conf(5)](https://www.freedesktop.org/software/systemd/man/latest/systemd-system.conf.html)
`DefaultRestartSec=`/`DefaultStartLimit*=` (defaults), all read 2026-11-11.
Searched `deploy/`, the node's `main`/`run`/`take_leases`, and the official man pages.
The repeated-start proofs run the actual wrapper and node CLI with a fixture
credential provider and different held leases on the two attempts; no manager,
credential store or broker is used. Supervision changes no node code.

Sources (**BUILD ON**, searched all deploy units and the B node parser):
Sextant@`7252b0f5c7ccdb416356aaddd9b53fa9a33bd405`
`deploy/paper/sextant-paper-b-stage1.service:1-13`, `deploy/paper/install.sh:23-28`,
`deploy/paper/paper-b-stage1.params.example:1-15`,
`tests/proofs/test_paper_units.py:34-138` (the unit and renderer contracts the test-entry unit extends; it adds no launcher or supervision logic);
Sextant@`7a5b42312cc4a10ee9991ae94f914049a3c78b4a`
`deploy/tradingview/install.sh:49-70`, `sextant-tradingview-prestage.service:6-16`,
`tests/proofs/test_tradingview_units.py:35-97,262-285`,
ADR-0028;
Sextant@`d617cf8e91fbf4b5f57a12faa1ae339259e2e9a1`
`src/sextant/paper/node_order.py:200-247,606-628` (the kit's two lease roots);
nautilus_trader@`d53849b216e20f2c122ca01b4dd0da377638b186`
`docs/concepts/live.md:119-121,186-189` (native run/signal/shutdown ownership);
[systemd.service(5)](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
(`ExecStart=`, `${VAR}`, `Restart=`) and
[systemd.exec(5)](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
(`EnvironmentFile=`, `WorkingDirectory=`), read 2026-11-11.

For the future entry composition, `--entry-state-parameters PATH` opts into the three
entry dependencies; neither attended unit supplies it. Copy
`paper-b-entry-state.params.example` to a non-secret local `.params` file. This file
is JSON registry rows, not a systemd assignment file: complete both absolute path
values and record the operator's approval in each source. The existing approved
`basis_state_home` and the basis file's parent must exist and be writable; resolved
paths outside that home refuse. Keep the same `basis_path` for the intended segment
across run/receipt directories. `SegmentBasis` writes its account/segment/params_hash
receipt there and refuses mismatched account or same-segment parameter changes;
it never recovers orders, positions or campaign state. No supplied option preserves
the default protection-only path; an incomplete explicit file refuses by name.
The option maps only B-scoped exits and the scoped paper fee model. Exit chase and
round-trip cost remain unavailable. It neither enables ranking nor admits entries.
