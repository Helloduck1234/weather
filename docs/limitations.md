# Limitations

What hasn't been proven, what's known to be imperfect, and how the code was checked. As of October 31, 2026.

## Not verified

- **Live code has never touched a real exchange.** It was tested only against fakes built from the API docs, so the first real order is a test of the code. The meaning of `assetNotional`, which feeds the daily loss limit, and of `avgPx` on a NO fill are both unconfirmed. See [Check these against the first real orders](../README.md#check-these-against-the-first-real-orders).
- **macOS scheduling has never run.** Everything was developed on Windows. Tests parse the plist and the cron line, but launchd has never loaded them.
- **No real DST change has passed through it.** The first is November 1, 2026. Day windows were checked by calculation across 32 zones and 730 days, but no live data has crossed one.
- **Real weather data covers October only.** That's a single season, and the second review ran on 14 stations.
- **The real ensembles have no track record.** The replay uses four deterministic models with a wider kernel, so the true calibration of the live model is unknown until paper trading shows it.
- **Illinois access to Polymarket US is unconfirmed.** See [research notes](research.md#polymarket-us-us).

## Model

- **Late-day `us` prices probably know more than the bot.** No free source has the sensor's 5-minute data, so treat a big late-day `us` edge as adverse selection until proven otherwise.
- **The sensor-gap odds come from one month,** 112 station-days, and they're shared across stations, although KLAX and KSFO ran widest.
- **Forecast peaks can read low.** Part of the ensemble data is 3-hourly and interpolated. Bias correction removes the average error, not the day-to-day error.
- **`intl` near-certainty looks a little overconfident at 9–12 hours left,** in a replay with stand-in forecasts. Paper trading will show whether that's real.
- **There's a small double count on `us`.** The sensor-gap odds already contain some first-minute highs, and the opening term adds its own. On a flat night the opening moves P(60°F) from 0.460 to 0.394. Real data can't size it yet, so it stays.
- **Same-day bias comes from the first forecast seen that day,** and it's applied to later runs too.

## Choices with a cost

- **Trading stops when the settlement day ends.** The market stays open for hours and the high looks known, but the bot can't prove it has seen every report. This gives up any late-market edge on purpose.
- **Positions are held to resolution.** There's no exit logic.
- **Caps reset at 00:00 UTC,** not on any local day.
- **Correlation across cities isn't modelled.** A cold front can move many stations at once, and only the day cap limits that.
- **Kill cancels by market,** through the API's bulk cancel, so it also cancels any order you placed by hand in those markets.
- **Daily loss compares account equity,** so a withdrawal looks like a loss and a deposit hides one.
- **The maker rebate is ignored on paper.** So is `intl` maker fill quality, since paper fills ignore queue position.

## Known small issues

Each was judged too small, or too unlikely, to fix before paper trading.

- A hole under 3 hours that falls across the peak can store a falsely low high in the bias record.
- An `unknown` order counts toward the day cap only on the UTC day it was created.
- `decision_state` is shared across paper and live, so switching mode can skip one repeat log row.
- If `kill` cancels alongside a stuck cycle, both can book the same fill. Reconcile against the app after that.
- The engine's `orders` count includes an order the exchange rejected.
- Nothing prunes the ledger or rotates `data/run.log`, and `exposure` scans all fills, so a very long run will slow down.
- A cycle has no overall deadline. Each request has its own timeout, a listing failure aborts early, and a `us` book outage stops the walk.
- The replay has small timing approximations, such as settlement timing, and the report says so.

## How it was checked

- **Tests.** 820 passing tests, written alongside the code. Warnings count as failures.
- **Independent review.** Two separate reviews read the code without the author's reasoning. The first led to 24 fixes. The second confirmed those were gone and found five more, all fixed. Neither judged the unverified exchange behaviour, Illinois law or the Open-Meteo licence.
- **What the second review's fixes didn't get.** They had no third reader. They were checked with the reviewer's own proof scripts and with mutation runs. Several were proved only against fakes.
- **Mutation runs.** Code broken on purpose, to confirm a test fails: 74 of 83 caught on the core and 14 of 15 on live code in the first passes, 22 of 23 on a follow-up where the survivor changed no behaviour, and 35 of 35 on the second-review fixes after 4 missing tests were added. The scripts aren't in this repo.
- **Real data.** Real cycles on both venues, and 21-day replays with a rule audit of 327 agreements out of 329 events.
