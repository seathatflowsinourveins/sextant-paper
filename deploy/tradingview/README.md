# TradingView display units

Trade step: ranking, shown to the owner. These units keep the newest ranked movers fresh without a hand-started process: the producer writes the display unit and the local ranked page, and the sync puts the unit into TradingView's two named lists, `NS movers` and `NS focus`. They are display only (ADR-0021). The producer reads slot 2's REST bars and opens no stream. The sync writes those two lists and nothing else. Neither places an order or touches Pine, presets, alerts or the Desktop port.

- `sextant-tradingview-prestage.service` runs the producer, `python -m sextant.tradingview.prestage`, as a long-running service.
  - It sleeps across closed windows on its own, refreshes the unit on the J5 cadence and moves to the next segment.
  - Restart it between segments, never inside one: a restart inside a segment costs one full baseline rebuild (about 16 minutes at 2026-11-11's size), while between ticks the ranked view carries its host ([#622](https://github.com/seathatflowsinourveins/sextant/pull/622)).
  - With `--ranked-view-dir` it also writes `ranked.html` and `ranked.json` under `$HOME/sextant-data/tradingview-prestage/ranked-view` each tick (`docs/tradingview.md`, the ranked page).
  - The display bounds come from the sourced `lanes/tradingview/display-bounds.params.json`, the module's default, so the unit passes no bound flag.
  - `Restart=on-failure` uses systemd's default limits, so a repeated refusal ends in `failed`.
  - Slot 2's two paper data names, `ALPACA_PAPER_2_API_KEY` and `ALPACA_PAPER_2_SECRET_KEY`, reach that one command through `scripts/with-keys.sh --strict`. No value is in a unit, an environment file, argv or a log.
- `sextant-tradingview-sync.service` runs `python -m sextant.tradingview --loop --keep-login` for one display session. **It is not installed while the display service's access is suspended**; the installer writes it only with `--with-sync`.
  - The loop loads the owner's stored sign-in from the encrypted key store (ADR-0028) and never opens a browser. A missing or refused sign-in is refused and paged.
  - `sextant-tradingview-sync.timer` starts it on weekdays at 04:00 New York time, the cadence registry's `display_start_minute_et`.
  - `Restart=on-failure`: the loop exits 0 at the 20:00 display close by design, which is never restarted. It refuses a latched run before any session opens, so a restart can only resume a crashed session or end in `failed`.

## Install (the lead, after merge)

1. The producer's three required files sit under `$HOME/sextant-data/tradingview-prestage/`: `nasdaqlisted.txt`, `otherlisted.txt` and `symbol_map.json`. The map must cover the whole filtered universe. Their sources are recorded in the producer section of `docs/tradingview.md`.
2. Stop any hand-started producer first. Both would take the same slot-2 lease, and the second one refuses.
3. From the merged checkout, run `bash deploy/tradingview/install.sh --producer-memory-max M`. The memory limit has no measurement yet, so it is required.
   - The one reading so far: a hand-run producer under a 6 GiB cap peaked at 164,057,088 bytes (`systemctl --user show -p MemoryPeak`, read 2026-11-11 03:37 EST). That was idle, before any display segment ran, so it does not size a ranking tick.
   - Read the peak again after a PRE tick and record it with the value chosen.
4. What the installer does:
   - It refuses a value or checkout path that would need systemd quoting.
   - It writes the producer unit under `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user`. It substitutes only the checkout prefix; the data directories stay under `$HOME/sextant-data`.
   - It reloads the user manager, runs `systemctl --user enable --now sextant-tradingview-prestage.service` and shows its status. Rerunning it replaces the unit.
5. Sync, only once the display service's access is restored: `bash deploy/tradingview/install.sh --producer-memory-max M --with-sync --sync-memory-max N`.
   - This writes the sync service and timer without enabling them.
   - Then run `systemctl --user enable --now sextant-tradingview-sync.timer`. Inside the display window, also run `systemctl --user start sextant-tradingview-sync.service` to start today's session.
   - Never run a hand loop while the unit is active, because the loop has no cross-process lock.

## Refusals

Read `systemctl --user status <unit>` and `journalctl --user -u <unit>`. No monitor rule covers these units yet.

- **Producer.**
  - A missing key exits 3.
  - A missing budget, map, bounds registry or K2 registry exits 2 with "TV producer stopped".
  - A held lease ends the process with an error.
  - A skipped segment or tick is paged on stdout, and the ranked page reads UNAVAILABLE with the reason.
- **Sync.**
  - These exit 2: a disabled registry, no landed SHA, a missing or refused stored sign-in, or a latched, failed or page-pending state. Repeated, they end in `failed (start-limit-hit)`.
  - Stale input pages once on stderr and leaves the lists unchanged.
  - Stopping the service in the middle of an apply latches that run. It must be reconciled before the next start.

Sources: `deploy/calibration/` (adapted), `deploy/openviking/openviking.service` (long-running user service), and [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html), [systemd.unit](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html), [systemd.timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html), [systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html) and [systemd.resource-control](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html), read 2026-11-11 from the installed systemd 259 man pages. [Python `PYTHONUNBUFFERED`](https://docs.python.org/3/using/cmdline.html#envvar-PYTHONUNBUFFERED) was read 2026-11-11. The manifest row is "TradingView producer and display sync as user units".
