# Attended paper node units

Trade step: custody and protection, then recorded paper evidence. These two user units
run the existing node entry points once, bounded by the node's own `--duration-s`.
All use `Restart=no`, with no timer or install target: a failure or normal stop
needs a new attended start. Paper B's attended
test entry is its own unit beside the protection-only stage 1.

The lead installs and starts only after the separate max gate, landing, and the
owner-confirmed writer/stream handover. These files grant no trading authority.
Keep every pre-check and post-check in the approved run kits. Do not retry a denied
shell start command; use this reviewed unit path after landing.

1. From the verified merged checkout, the lead runs `bash deploy/paper/install.sh`.
   Like the TradingView installer, this renders the checkout prefix into the user
   unit directory and reloads the manager. It never enables or starts anything,
   reads a parameter file, or touches keys. Rerunning replaces the unit file.
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
   exclude each current holder in its own location. No separate recoverable state
   directory exists: receipt-dir contains the receipt and lifecycle journal;
   restarting requires the
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
