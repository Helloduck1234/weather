# Model and trading rules

What `model.py`, `metar.py` and `strategy.py` do, step by step, with the setting that controls each step. Settings live in `config.yaml`, and the values here are the defaults.

## 1. What a market settles on

Each market's text decides its rule, and the rule decides the day and which readings count.

| Rule family | Recognised from | Day | Readings that count |
| --- | --- | --- | --- |
| `metar_routine` | An `aviationweather.gov` source URL | Local midnight to midnight, with DST | Routine reports only. SPECI reports are excluded |
| `noaa_timeseries` | A `weather.gov` source URL | Local, with DST | All reports |
| `wunderground` | A `wunderground.com` source URL | Local, with DST | All reports |
| `nws_cli` | Every Polymarket US market | Local standard time, all year | All reports, plus the sensor's 6-hour and 24-hour maximum groups |

A reading uses the T-group in the remarks when it has one, in tenths of a degree, and the whole-degree body group otherwise. To settle, it's converted to the market's unit and rounded half up, so −8.5 becomes −8.

Under `nws_cli`, a 6-hour group raises the high only if its whole span lies inside the day. A 24-hour group counts if it's filed within an hour of the day's end, and it settles the day.

## 2. Forecast members

Each model's ensemble run is fetched from Open-Meteo: GFS (`gfs025`, 31 members) and ECMWF IFS (`ecmwf_ifs025`, 51 members). A run stays usable for `forecast.max_age_hours` (18) if a refresh fails. Members are pooled on one time axis, so ECMWF has more weight simply by having more members.

## 3. Bias correction

Raw ensembles run cold, and the models can differ by degrees, so each is corrected on its own record before pooling. The record is kept separately for each station, rule policy, lead in days and model.

```
error = observed high − raw ensemble mean high, for each settled day in the last window_days
bias  = mean(error) × n / (n + shrink_days)
member = raw member + bias
```

`bias.window_days` is 30 and `bias.shrink_days` is 3, so a thin record is trusted less.

Until every model has `bias.min_days` (7) settled days at that station and lead, the market is held as `warming_up`. It's still priced and scored, but it can't trade.

## 4. The distribution of the final high

Let `w` be the observation weight, which rises through the day:

```
w = clamp((local_hour − obs_start_hour) / (obs_full_hour − obs_start_hour), 0, 1)
```

`model.obs_start_hour` is 6 and `model.obs_full_hour` is 17. `w` is 0 before the day starts.

Only the part of the day still ahead counts for the forecast, because what has already happened is carried by the floor in step 5. If fewer than `model.min_horizon_coverage` (75%) of the remaining hours have data, the day is skipped as `forecast_horizon`.

1. **Anchor.** Each member's current error is carried forward and fades with time. `latest` is the newest report of any kind, SPECI included.

   ```
   path += w × (latest − member at that time) × exp(−hours ahead / anchor_tau_hours)
   ```

2. **Member highs.** Each member's high is the maximum of its adjusted path.
3. **Inflate the spread.** Ensembles are underdispersed, so the spread widens, but less as observations take over.

   ```
   alpha = 1 + (spread_inflation − 1) × (1 − w)
   high  = mean + alpha × (high − mean)
   ```

   `model.spread_inflation` is 1.3.

4. **Smooth.** Each member becomes a Gaussian. Its width narrows from `kernel_sigma_start_c` (0.7°C) to `kernel_sigma_end_c` (0.25°C) as `w` goes from 0 to 1.

After the last forecast step of the day, only the temperature right now can still beat the high, so the distribution is a single member at the newest reading, with the narrow kernel.

## 5. Bucket probabilities

The final high is the largest of what the rest of the day does, what's already been seen, and, on `us`, the day's first moment:

```
P(high < x) = P(forecast < x) × P(observed < x) × P(opening < x)
```

- **Forecast.** The mean over members of `Φ((x − high) / sigma)`.
- **Observed.** 1 when `x` is above the high so far, and 0 otherwise. With no reading yet, this factor is dropped.
- **Sensor gap, `us` only.** The NWS figure is the sensor's maximum, which can sit above every report. Until a maximum group settles the highest reading, the floor is a mix of that reading plus 0, 1, 2, 3 or 4°F, with odds `model.sensor_gap_f` (0.46, 0.41, 0.08, 0.03, 0.02), and never below the high so far. A 6-hour group at or under the high settles the readings it spans, even if it reaches outside the day. A group above the high settles nothing, because its maximum may belong to the day next door.
- **Opening, `us` only.** The temperature at the moment the day starts is interpolated between the reports either side, if they're within 2 hours of each other, and used with the narrow kernel. It drops out once a sensor maximum shows that moment can't beat the high.

A bucket's probability is `P(high < hi + 0.5) − P(high < lo − 0.5)`, and open-ended buckets use infinite edges. Half-up rounding puts a reading exactly on an edge in the upper bucket, so the comparisons match settlement.

## 6. Uncertainty near edges

A bucket whose probability hangs on a fraction of a degree shouldn't be trusted to that fraction. So the masses are computed three times: as they are, and with the forecast and opening shifted by ±`model.edge_shift_c` (0.25°C). Observed readings don't shift, because they've already happened.

The lowest and highest of the three form a band. Everything is then shrunk slightly toward uniform, so nothing is ever certain:

```
shrunk = (1 − prob_epsilon) × x + prob_epsilon / number of buckets
```

`model.prob_epsilon` is 0.01. Trades use the band's worse side: `q_yes = low` and `q_no = 1 − high`. Scoring uses the unshifted value.

## 7. Holds

A market on hold is priced and scored, but not traded.

| Hold | Meaning |
| --- | --- |
| `warming_up` | A model has too short a record here |
| `model_missing` | A configured model has no current run |
| `thin_observations` | Today's reports have a hole over 3 hours |

A day with too little to go on isn't priced at all, and shows up in the run's skip counts as `stale_obs`, `no_forecast`, `forecast_horizon` or `no_observations`.

## 8. Deciding a trade

`strategy.evaluate` scores both sides, YES on the book and NO on the mirrored book, and takes the better one.

```
edge = q − ask − fee(ask)
fee(p) per share = rate × (p × (1 − p)) ^ exponent
```

Each market's own fee schedule supplies `rate` and `exponent`. A market with none falls back to the published weather rate of 0.05.

A side qualifies when `edge >= strategy.edge_threshold` (0.05) and the ask is within `min_price` to `max_price` (0.02 to 0.98). Skip reasons include `edge_below_threshold`, `extreme_price`, `opposite_side_held`, `day_cap`, `event_cap`, `kelly_target_met` and `below_min_size`.

## 9. Sizing

```
cost   = ask + fee(ask)
target = kelly_fraction × (q − cost) / (1 − cost) × bankroll
budget = min(target − already held, max_trade_usd, room under max_day_usd, room under max_event_usd)
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `strategy.kelly_fraction` | 0.15 | Fraction of full Kelly. Must stay within 0.10 to 0.25 |
| `strategy.max_trade_usd` | $25 | Per trade |
| `strategy.max_event_usd` | $40 | Per event, because buckets in one event are correlated |
| `strategy.max_day_usd` | $100 | Per UTC day |
| `strategy.max_depth_share` | 0.15 | Share of visible depth at prices that still have edge |

Open orders count as if filled. Live mode runs on its own bankroll, under whichever caps are tighter. See [Limits](../README.md#limits).

## 10. Execution

With `strategy.execution: maker_first`, a signal rests a limit order at `min(ask − tick, bid + tick)`, rounded down to the tick. It stays for `order_ttl_minutes` (30) while the signal holds, and then the bot cancels it and may cross the spread. Set `taker` to always cross.

A taker order walks only levels that still clear the threshold, and it's sized so the budget covers the shares at the worst price it may pay.

The entry signal is always the taker edge above. Resting is an execution preference, so a maker fill pays less than the signal assumed. The maker rebate is ignored on paper.

Positions are held to resolution. There is no exit logic.

## 11. Paper fills

A taker fills at the logged book's levels, with its fee. A maker fills at its limit once the ask has come down to it, up to the size showing at or below the limit. That misses trades between cycles and ignores queue position, so treat maker P&L as the softest number in the report.
