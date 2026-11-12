# Overnight display

The OVERNIGHT producer uses the existing display baseline and replay path. It is
disabled by the checked-in `overnight_enabled=0` cadence row. Changing that row
requires a reviewed session-boundary change and cadence-state migration; existing
state with a different registry hash refuses. The general display enable flag,
owner consent, account admission, stale-input checks and failure latch still apply.

The upstream contract is a New York trading-date window from the previous calendar
day at 20:00 to the trading date at 04:00, including Sunday evening and full
overnight windows before half-days. XNYS holidays and weekend closures remain
closed. These are session definitions, not tunable thresholds. Historical anchors
use their own calendar dates and EST/EDT offsets. Scheduling subtracts absolute
clocks when a weekend spans a daylight-saving transition.

Minute history selects `DataFeed.BOATS` using alpaca-py 0.44.0's existing paced REST
transport. Daily references stay SIP, using the latest completed XNYS regular
close, including the calendar's early-close clock. No stream is added. The
distinct derived `DataFeed.OVERNIGHT` feed is never selected. Source:
[Alpaca 24/5 Trading](https://docs.alpaca.markets/us/docs/245-trading-for-trading-api),
[Historical Stock Data](https://docs.alpaca.markets/us/docs/historical-stock-data-1),
read 2026-11-11; alpaca-py v0.44.0 at
`cc4cb3b7ba50ae250e621983c2779047fb16bb28`, `alpaca/data/enums.py`,
`requests.py` and `historical/stock.py`.

The display retains its close-times-volume baseline recipe. It is not claimed to
be the shared calibration recipe, which uses VWAP. The sourced
`overnight_baseline_sessions` row starts at five, UNCALIBRATED, preserving the
existing display policy. Existing `regular_floor_s` and `usage_floor_s` rows govern
overnight cadence; daytime onset windows do not create an overnight onset policy.
An entirely absent historical date refuses the segment; quiet names retain the
existing carry-forward curves. A baseline cannot be known before its last window
ends. Tables, generation topics, host receipts and projected research provenance
all identify BOATS; daily-history provenance identifies SIP.

The installation's BOATS entitlement and actual historical coverage remain
UNKNOWN. No probe was performed for this build. The first attended overnight
start needs a separate ruling; a vendor refusal is a recorded result, not a reason
to switch feeds, create a zero baseline or start a stream.

Lesson: a segment's trading date need not be the calendar date on which its bars
start. Extend the baseline anchor and downstream feed/clock validation together;
an allowlist edit alone cannot establish an admitted display unit. Keep the
cross-midnight, missing-history, DST and feed-mismatch proofs in
`tests/proofs/test_tradingview_overnight.py` when changing this path.
