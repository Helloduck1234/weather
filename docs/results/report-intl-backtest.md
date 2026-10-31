# intl backtest report

Generated 2026-10-31 07:57 UTC.

**Not a measure of the edge.** Open-Meteo doesn't archive ensemble members, so a few deterministic models stand in for the ensemble the bot trades on. Book depth is assumed, polymarket.com quotes are one price with an assumed spread, and settlement timing is approximate. Use this to check the pipeline. Only paper trading measures the edge.

## P&L

| Measure | Value |
| --- | --- |
| Starting bankroll | $1,000.00 |
| Realized P&L | -$107.40 |
| Unrealized P&L (at mid) | +$0.00 |
| Equity | $892.60 |
| Cost of open positions | $0.00 |
| Fees paid | $6.13 |
| Realized P&L if every fill paid 0.05 x p x (1-p) | -$118.36 |
| Fills, of which settled | 106, 106 |
| Win rate by position | 41% |
| Max drawdown by event day | $111.66 |
| 90% interval on realized P&L, resampling days | -$248.19 to +$35.88 |
| Average edge at entry, per share | +26.3c |
| Realized edge after fees, per share | -6.9c |

### Settled P&L by station

| Station | Fills | P&L |
| --- | --- | --- |
| KATL | 10 | +$26.43 |
| KSFO | 4 | +$13.81 |
| KDAL | 12 | +$4.13 |
| KLAX | 6 | -$5.47 |
| KMIA | 10 | -$5.72 |
| KLGA | 16 | -$7.80 |
| KSEA | 5 | -$10.19 |
| KHOU | 14 | -$24.39 |
| KAUS | 15 | -$40.05 |
| KORD | 14 | -$58.16 |

### Settled P&L by lead time

| Lead time | Fills | P&L |
| --- | --- | --- |
| 6-24h | 24 | -$24.60 |
| 48h+ | 33 | -$37.67 |
| 24-48h | 49 | -$45.13 |

### Settled P&L by side

| Side | Fills | P&L |
| --- | --- | --- |
| yes | 14 | -$5.22 |
| no | 92 | -$102.18 |

### Settled P&L by order style

| Order style | Fills | P&L |
| --- | --- | --- |
| taker | 106 | -$107.40 |

## Brier score

Lower is better. Each row pairs the model with the market mid at the same moment.

| Set | Forecasts | Model | Market |
| --- | --- | --- | --- |
| All buckets | 4537 | 0.1057 | 0.0950 |
| Market mid 5-95% | 3464 | 0.1331 | 0.1218 |
| 0-6h | 39 | 0.0152 | 0.0214 |
| 24-48h | 1182 | 0.1356 | 0.1091 |
| 48h+ | 2289 | 0.0717 | 0.0807 |
| 6-24h | 1027 | 0.1509 | 0.1134 |

Model minus market, 90% interval resampling days: +0.0076 to +0.0140.
An interval wholly below zero favours the model.

## Calibration

![Calibration chart](calibration-intl-backtest.png)

### Model

| Bin | Forecasts | Mean forecast | Happened | 90% interval |
| --- | --- | --- | --- | --- |
| 0%-10% | 1846 | 3.9% | 1.4% | 1.0%-1.9% |
| 10%-20% | 1593 | 15.3% | 14.2% | 12.8%-15.7% |
| 20%-30% | 1056 | 23.3% | 36.2% | 33.8%-38.6% |
| 30%-40% | 8 | 35.8% | 0.0% | 0.0%-25.3% |
| 40%-50% | 8 | 44.0% | 37.5% | 16.1%-65.2% |
| 50%-60% | 1 | 51.0% | 0.0% | 0.0%-73.0% |
| 60%-70% | 4 | 61.6% | 75.0% | 35.6%-94.2% |
| 80%-90% | 3 | 86.4% | 100.0% | 52.6%-100.0% |
| 90%-100% | 18 | 98.2% | 94.4% | 78.5%-98.8% |

### Market mid

| Bin | Forecasts | Mean forecast | Happened | 90% interval |
| --- | --- | --- | --- | --- |
| 0%-10% | 1867 | 4.6% | 1.3% | 0.9%-1.8% |
| 10%-20% | 816 | 14.2% | 7.7% | 6.3%-9.4% |
| 20%-30% | 831 | 24.9% | 15.6% | 13.7%-17.8% |
| 30%-40% | 482 | 34.0% | 30.9% | 27.6%-34.5% |
| 40%-50% | 278 | 44.3% | 43.9% | 39.1%-48.8% |
| 50%-60% | 156 | 54.7% | 60.3% | 53.7%-66.5% |
| 60%-70% | 79 | 63.3% | 64.6% | 55.4%-72.8% |
| 70%-80% | 8 | 72.5% | 75.0% | 46.0%-91.3% |
| 80%-90% | 1 | 87.0% | 100.0% | 27.0%-100.0% |
| 90%-100% | 19 | 96.5% | 100.0% | 87.5%-100.0% |

## Rule audit

Our own reading of each result, against the bucket that actually won.

| Rule family | Checked | Agreed | Share | No verdict |
| --- | --- | --- | --- | --- |
| metar_routine | 84 | 84 | 100% | 0 |
| noaa_timeseries | 130 | 129 | 99% | 0 |

## Health

195 cycles, 0 failed.

| Skip reason | Events |
| --- | --- |
| beyond_lead_limit | 19586 |
| day_over | 17179 |
| warming_up | 3935 |
