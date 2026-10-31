# weatherbot

Paper-trades Polymarket daily high-temperature markets. It prices every bucket from weather ensembles and airport observations, logs simulated trades to SQLite, settles them after the markets close, and reports whether the edge is real.

**Status at October 31, 2026: paper only, with no proven edge.** In a 21-day replay the model scored worse than the market's own prices, and both P&L intervals include zero. Real paper trading has yet to run long enough to say more. See [results](docs/results.md).

Live trading is off by default. It needs `--live`, API keys, and every gate below to pass. The live code has never touched a real exchange.

Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0) and the [Aviation Weather Center](https://aviationweather.gov/data/api/).

## Documentation

| Read | For |
| --- | --- |
| This README | Setup, commands, reading the report, going live |
| [Architecture](docs/architecture.md) | Code layout, one cycle step by step, the ledger, extending it, tests |
| [Model and trading rules](docs/model.md) | The pricing maths and trade logic, setting by setting |
| [Results](docs/results.md) | Backtest numbers and calibration charts |
| [Research notes](docs/research.md) | What was checked about the markets and data sources, with links |
| [Limitations](docs/limitations.md) | What's unverified, what's known to be imperfect, and how the code was checked |

## Setup

Needs [uv](https://docs.astral.sh/uv/). Run everything from the project folder.

```bash
git clone https://github.com/Helloduck1234/weather.git ~/weatherbot
```

```bash
cd ~/weatherbot
```

```bash
uv sync
```

```bash
.venv/bin/weatherbot run
```

That is one cycle on both venues. It writes `data/weatherbot.sqlite` and `reports/`.

To update, run `git pull` and then `uv sync`. Git ignores `data/` and `reports/`, so your ledger stays put. `config.yaml` is tracked, so a local edit can conflict with a pull. Never push a config with `jurisdiction_confirmed: true`, or anything from `.env`.

### Schedule it on the Mac mini

launchd runs one cycle every 15 minutes. Before you install it:

- **Keep the project out of Desktop, Documents and Downloads.** macOS blocks launchd and cron from those folders, and iCloud Drive can sync them, which isn't safe for a SQLite database in use. `~/weatherbot` works.
- **Turn off automatic sleep.** A sleeping Mac misses its runs.
- **Turn on automatic login.** This installs a LaunchAgent, which runs only while you are logged in, so a reboot would otherwise stop the bot. To run without a login, install it in `/Library/LaunchDaemons` with a `UserName` key instead.

```bash
mkdir -p data ~/Library/LaunchAgents
```

```bash
sed "s|__PROJECT__|$PWD|g" deploy/com.weatherbot.run.plist > ~/Library/LaunchAgents/com.weatherbot.run.plist
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.weatherbot.run.plist
```

Output goes to `data/run.log`. To stop it:

```bash
launchctl bootout gui/$(id -u)/com.weatherbot.run
```

`deploy/crontab.example` does the same with cron. The bot holds a lock during each run, so overlapping starts are refused rather than doubled up.

Nothing prunes the ledger or rotates `data/run.log`. The ledger grew about 1 MB in a cycle with new forecast runs, mostly snapshots, so watch the disk over a long paper run.

All of this was developed and tested on Windows, so check the first few launchd runs in the log.

## Commands

| Command | What it does |
| --- | --- |
| `weatherbot run` | One paper cycle, then settlement and the report, on every venue in the config |
| `weatherbot run --venue us` | The same on one venue |
| `weatherbot settle` | Settlement on its own |
| `weatherbot report` | Rebuilds the report from the ledger. `--mode live` reports live trades |
| `weatherbot backtest` | Replays closed markets. See below |
| `weatherbot check-live` | Read-only. Shows the geoblock status, what Polymarket US lists, and every live gate |
| `weatherbot kill --reason "..."` | Turns the kill switch on and cancels our live orders |
| `weatherbot unkill` | Releases the kill switch |

Exit codes: 0 fine, 1 something failed, 2 refused (bad config or a live gate), 3 live trading halted itself.

## How it works

Each cycle:

1. Lists open temperature events. The settlement station and rule come from each market's own text, never from a hardcoded table. Events whose station is missing from `config.yaml` are skipped with a logged reason.
2. Refreshes METAR reports, and fetches ensemble forecasts only when a model publishes a new run. Refreshing on every cycle would overrun Open-Meteo's free limit.
3. Takes each member's highest temperature over the local day window, corrects each model by its own record at that station, inflates the spread, and turns the members into bucket probabilities. As the day goes on it clamps members to the high observed so far and carries the current forecast error forward.
4. Prices near-edge rounding risk by shifting the forecast both ways and trading on the worse of the results. The observed high stays put, because it has already happened.
5. Computes edge as conservative probability minus ask minus fee, then sizes the survivors with fractional Kelly under per-trade, per-event, per-day and book-depth caps. Prices under 2¢ or over 98¢ are skipped.
6. Logs each market's decision with the model probability, book snapshot and forecast run times. A market is logged again only when its decision changes, or when it buys. Skips of a whole event, such as an unconfigured station, appear as counts under Health in the report.

The `intl` and `us` venues are separate paper accounts with the same bankroll and caps.

### When it won't trade

| Reason | Meaning |
| --- | --- |
| `warming_up` | A model has too short a record at this station and lead time |
| `model_missing` | A configured model has no current forecast run |
| `thin_observations` | Today's reports have a hole over 3 hours, which could hide the high |
| `day_over` | The settlement day has ended |

The first three still log probabilities, so those markets are scored. A day with a hole that size also stays out of bias correction and the rule audit, because it reads falsely low.

Trading stops when the settlement day ends, even though markets stay open for hours after. The high looks known by then, but the bot can't prove it has seen every report, so late trades would be bets on missing data. Resting orders on that day are cancelled.

`parse_error` under Health counts listings that couldn't be read. One bad listing costs only itself.

### Warm-up

No trades happen at a station until each model has `bias.min_days` days on record there (7 by default, separately for each lead time). Paper, live and backtest all apply this. Probabilities are still logged during that time, so scoring starts on day one.

This matters a lot. On the first real run the raw ensembles sat 2–3°C below tight, coherent market ladders, and a check against observations showed the market was right: yesterday's highs beat the models by 0.5–0.8°C on average, by up to 4°C at single stations, and GFS and ECMWF differed by 3–4°C at Mexico City and Taipei. Every large "edge" on that first run was forecast bias, so expect the first week to show `warming_up` instead of trades.

Correction carries into live mode, because forecasts and observed highs live in the same ledger.

## The two venues

They are different contracts, so a result on one says little about the other.

| | `intl` (polymarket.com) | `us` (Polymarket US) |
| --- | --- | --- |
| Settles on | Highest routine METAR reading for 48 of 49 cities, local clock with DST | NWS Climatological Report (Daily), local standard time |
| Stations | About 49 cities, parsed from each market | NYC (KNYC), Miami, Chicago (KMDW), LA, San Francisco, Philadelphia |
| Buckets | 11 buckets, mostly 1°C | 6 buckets, 2°F |
| Fees | `zero_fees` at time of writing | Coefficient 0.0695 per market, no maker rebate below $10M monthly maker volume |
| Live trading | Never. Its geoblock covers the US and its Python client is dead | Possible, behind the gates below |

Fee schedules are read from each market when it provides one, so these update themselves.

### What the `us` contract adds

The NWS figure is the sensor's own daily maximum, not the highest report. Two things follow, and both are priced for `us` only:

- **The peak can fall between reports.** Over 112 station-days in October 2026, the sensor beat the highest hourly report by 0°F 46% of the time, 1°F 41%, 2°F 8%, 3°F 3% and 4°F 2%. KLAX and KSFO were the widest. The reports reveal the sensor only through their 6-hour and 24-hour maximum groups, so until one of those covers the highest reading, the model treats the high as a mix of those steps (`model.sensor_gap_f`). A 6-hour group reaching outside the day can't raise the high, since its maximum may belong to the day next door, but if it's at or under the high it still settles every reading it spans.
- **The high can be the day's first moment.** The day starts at local standard midnight. On a night when a front is dropping the temperature, the high lands in that first minute and no report lands on it. At KNYC on October 17, 2026, reports either side of the start read 17.2°C and 14.4°C, and the sensor kept 16.7°C. The model interpolates across the start and treats that estimate as a soft floor under the high, as sure of it as of a late-day reading. The estimate drops out once a sensor maximum shows that moment can't beat the high.

Together these took the replay's near-certain `us` prices (0.9 and up) from hitting 84–88% of the time at 6–12 hours out to 98–100%.

Late in the day the market still sees more than the bot does. No free source has the sensor's 5-minute data, and `us` quotes tend to lock around 20:15 UTC on the target day, probably on the NWS afternoon preliminary report. Treat a big late-day `us` edge as a sign the market knows something, not as a gift.

## Reading the report

`reports/report-<venue>-<mode>.md` and `calibration-<venue>-<mode>.png`.

- **Realized P&L, and the 90% interval from resampling days.** Buckets in one event are correlated, so days are the real sample size. An interval that includes zero means no evidence yet.
- **P&L if every fill paid 0.05 × p × (1−p).** International markets are fee-free for now. This shows whether the edge survives if that changes.
- **Brier score, model against market.** The model has to beat the market's own price, not just look calibrated.
- **Rule audit.** Compares the bucket our observations imply with how each market really resolved. A mismatch means the settlement logic is wrong for that station.
- **Health.** Failed cycles and skip reasons, including `warming_up`.

Paper fills are simulated. Takers walk the logged book. Makers fill only once the ask comes down to their limit. That misses trades between cycles and ignores queue position, so maker P&L is the softest number in the report.

Run paper for a few weeks before trusting any of it.

## Backtest

```bash
.venv/bin/weatherbot backtest --days 21
```

It runs the real cycle code over closed markets, in its own `data/backtest.sqlite`, and writes to `reports/backtest/`. Options: `--venue`, `--days`, `--stations KNYC,KPHL`, `--step-hours`, `--depth`, `--half-spread`.

Treat it as a test that the pipeline behaves, **not** as a measure of the edge, because:

- **No ensemble replay exists.** Open-Meteo keeps no past ensemble runs, so it uses four deterministic models from its Single Runs API, with a wider kernel standing in for ensemble spread. The bot archives its own ensemble snapshots going forward.
- **Depth is assumed** (100 shares a side by default). `us` replays real top-of-book quotes every 15 minutes. `intl` has one historical price, widened by ±1¢.
- **It only takes liquidity**, since quotes can't show whether a resting order would have filled.
- **METAR history ends at about 30 days**, and the first week of any replay is warm-up.
- **The wider kernel distorts it.** At 1.5°C it can't put much more than 0.28 on a 2°F bucket, so most `us` replay trades go against the favourite or onto long shots 24–48 hours out, where the market is well calibrated. Whether the real ensembles do better is unknown until paper trading shows it.

Each forecast run stays hidden until 11 hours after its start time, the slowest lag seen, and cycles run after every earlier report would have been published.

`intl` defaults to US stations, because only their reports carry tenths of a degree.

## Going live

Live trading is only on Polymarket US, through its REST API. No VPN or proxy path exists, and none should be added.

**The live code has never touched a real exchange.** It was tested only against fakes built from the API docs. Treat the first order as a test of the code.

1. Paper-trade for a few weeks. Look for positive P&L after fees across many settled days, an interval clear of zero, and a model Brier score below the market's.
2. **Confirm Illinois availability in the Polymarket US app.** Polymarket US lists these markets, but its Illinois status is contested. The Illinois Gaming Board's [cease-and-desist list](https://igb.illinois.gov/sports-wagering/cease-and-desist-letters.html) has a January 27, 2026 entry reported as addressed to its operator, and the CFTC has sued Illinois over the question. No primary source settles it, so this needs your own check and possibly legal advice.
3. Set `live.jurisdiction_confirmed: true` in `config.yaml`.
4. Buy an Open-Meteo plan. The free tier is non-commercial, which fits paper research but not trading for profit, and the [pricing page](https://open-meteo.com/en/pricing) says ensemble data needs Professional or above. Confirm your key works on `customer-ensemble-api.open-meteo.com`.
5. Create a key at polymarket.us/developer, copy `.env.example` to `.env`, fill it in, and run `chmod 600 .env`. Never commit it.
6. Install the signing library:

   ```bash
   uv sync --extra live
   ```

7. Load the keys and check every gate:

   ```bash
   set -a; source .env; set +a
   ```

   ```bash
   .venv/bin/weatherbot check-live
   ```

8. Stop the paper schedule, because the run lock lets only one mode run at a time. Then start by hand, watching the first order in the app:

   ```bash
   .venv/bin/weatherbot run --live --venue us
   ```

   That is one cycle, and then the process exits. Until the next run, a resting order has no fill sync, no loss check and no cancel. If you aren't scheduling it next, close the orders with `weatherbot kill`.

9. Schedule it. launchd doesn't read `.env`, so the installed plist has to load it. In `~/Library/LaunchAgents/com.weatherbot.run.plist`, set `ProgramArguments` to:

   ```xml
   <array>
     <string>/bin/sh</string>
     <string>-c</string>
     <string>set -a; . ./.env; set +a; exec .venv/bin/weatherbot run --live --venue us</string>
   </array>
   ```

   Then `bootout` and `bootstrap` it again. The files in `deploy/` stay paper-only on purpose.

### Limits

Live runs on its own `live.bankroll_usd`, under whichever caps are tighter. Defaults: $100 bankroll, $10 per order, $25 per day, $20 daily loss. The loss limit sits under the day cap so it can trip. Both reset at 00:00 UTC.

- The executor re-checks the caps before every order, at the order's limit price plus any taker fee. A breach halts it.
- The kill file and the daily loss limit are checked first in every cycle, before any market data, so an outage can't hold a stop back.
- A failing `us` book costs only that market, whose resting order is then cancelled. Three failures in a row count as an outage, and that or any other exchange read failure aborts the cycle.
- Orders are never retried. Only an HTTP 400 counts as a clean rejection, because it's the only one the docs describe. Anything else leaves the order's fate open, so the bot engages the kill switch and cancels its own markets.
- An order in that state is marked `unknown`. It keeps counting against the caps until you resolve it.
- Resting orders expire on the exchange at twice the order TTL, so a dead bot can't leave them open for long.
- Daily loss compares account equity with its first reading of the UTC day. A withdrawal looks like a loss and a deposit hides one.

### Kill switch

```bash
.venv/bin/weatherbot kill --reason "looks wrong"
```

This writes `data/KILL` first, so nothing can stand in its way. It then waits up to 30 seconds for a running cycle to finish and cancels our open orders, or cancels alongside that cycle if it never finishes. It cancels by market, so any order you placed by hand in those markets goes too. Without keys it tells you to cancel in the app. If any order isn't confirmed closed, `unknown` ones included, it says so and exits 1, so run it again or check the app. If an order's post was still in flight when its row closed, the running cycle cancels that order by id once it lands, then halts.

A broken config doesn't take the switch away. It still writes the file, at the path the config names if that part can be read and at `data/KILL` beside the config if not, then exits 2. It can't cancel anything without a config, so cancel in the app, and check the printed path if you set your own `live.kill_file`. With no config file at all, it can't know where the bot lives, so it writes nothing and says only that the config is bad.

While the file exists, live runs refuse to start. Only `weatherbot unkill` or deleting the file releases it. Paper runs ignore the switch.

### Check these against the first real orders

The docs left these open, so the code guards them rather than assuming they hold:

- Whether `avgPx` on a NO fill is in YES terms. A fill price outside the limit halts the bot.
- What `assetNotional` means in balances, which feeds the daily loss limit.
- Orders marked `unknown` need review in the app, because the API has no client order ID to match them by.

## Configuration

`config.yaml` holds venues, risk caps, model settings and every station (ICAO, time zone, coordinates, elevation). Unknown keys are errors and numbers are range-checked, so a typo stops the bot at startup instead of reaching a trade. Relative paths follow the config file, not the current folder. To add a city, add its station. Health in the report shows `station_not_configured` for any event it is missing.

[Model and trading rules](docs/model.md) explains what each setting does.

## Development

```bash
uv run pytest
```

The tests use responses recorded from the real APIs, so they need no network and no keys. [Architecture](docs/architecture.md) covers the code layout and how to extend it.

## Where this differs from the original brief

The bot started from a written brief. Each row is a place where the live APIs or official docs said otherwise when checked in October 2026. [Research notes](docs/research.md) has the full detail.

| Brief | Found |
| --- | --- |
| Markets resolve on Wunderground | International markets moved to METAR for 48 of 49 cities around October 21–23, 2026: highest routine T-group, SPECI excluded |
| Weather taker fee 0.05, 25% maker rebate | International temperature markets are now `zero_fees`. [The docs](https://docs.polymarket.com/trading/fees) still show the old rate, so the bot trusts each market's own fee fields |
| Polymarket US has a weather category | It does list temperature markets, but on a different contract: NWS climate report, standard time, 0.0695 fee ([fees](https://docs.polymarket.us/fees.md)) |
| Denver uses KBKF | Denver parses as KDEN. Stations are parsed from each market, as the brief asked |
| Tag 84 or 103040 | Both are real. `daily-temperature` is 103040 and `weather` is 84. The bot tries the first, then the second, and filters by title |
| `py-clob-client` for orders | [Archived](https://github.com/Polymarket/py-clob-client) and no longer functional. Its successor is [py-sdk](https://github.com/Polymarket/py-sdk), but polymarket.com's geoblock covers the US anyway, so live uses Polymarket US REST with Ed25519 signing ([auth](https://docs.polymarket.us/api-reference/authentication.md)) |
| Replay Open-Meteo forecast runs | No past ensembles exist. [Single Runs](https://open-meteo.com/en/docs/single-runs-api) covers deterministic models back to about April 2, 2026 |
| Offset pagination | Fine for open events. Closed events need `/events/keyset`, since offset paging fails with a 422 past about 2,000 |
| Bias correction per station | Per station, model and lead time, because the models disagree with each other by degrees |
| `timezone=auto` for the local day | Open-Meteo's time zone ignores DST changes, so requests use `timeformat=unixtime` and the bot works out each day from the station's zone |
| Settle from closed Gamma markets | `intl` does. `us` isn't on Gamma, so it reads each market's settlement from the Polymarket US gateway |
