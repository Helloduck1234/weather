# Research notes

What was checked about the markets and data sources while building the bot. All of it was read from live APIs or official docs in late October 2026. These venues change without notice, so recheck anything you're about to rely on. Anything inferred rather than read is marked.

For a short list of where this differs from the original brief, see the [README](../README.md#where-this-differs-from-the-original-brief).

## polymarket.com (`intl`)

**How markets resolve.** Older write-ups say Wunderground. Around October 21–23, 2026, 48 of 49 cities moved to METAR: the highest T-group of any routine report, with SPECI reports excluded, °F rounded half up, on the local clock with DST. Closed listings also show `noaa_timeseries` and `wunderground`, older rules the bot can still read. Hong Kong settles on its own observatory, which the bot skips as `unsupported_source`.

**Stations.** They aren't always the airport you'd guess, or what older sources say. Denver is KDEN, not KBKF. Dallas is KDAL, Houston KHOU, London EGLC, Paris LFPB, Seoul RKSI and Taipei RCSS. New York is KLGA here but KNYC on Polymarket US. The bot reads each one from its market.

**Listing events.** `gamma-api.polymarket.com/events` sorts oldest first by default, so the bot sends `order=endDate` and `ascending=false`. Offset paging is fine for open events but failed with a 422 around offset 2,100, so closed events use `/events/keyset`.

**Tags.** `/tags/slug/weather` is 84 and `/tags/slug/daily-temperature` is 103040. Both are real. The bot resolves them at runtime, tries `daily-temperature` first, and filters by title.

**Fees.** Temperature markets now carry `feeType: zero_fees`. Until about October 17 they carried `weather_fees`, with a 0.05 rate and a 25% maker rebate, and [the docs](https://docs.polymarket.com/trading/fees) still list that. The bot trusts each market's own fields.

**Order client.** [`py-clob-client`](https://github.com/Polymarket/py-clob-client) was archived on May 25, 2026. Its successor is [py-sdk](https://github.com/Polymarket/py-sdk), still at 0.x.

**Geoblock.** `polymarket.com/api/geoblock` reports US addresses as blocked, so placing orders there from the US is off-limits. Public market data reads worked. `weatherbot check-live` shows the current status.

## Polymarket US (`us`)

**It lists temperature markets.** Six cities: New York (KNYC), Chicago (KMDW), Miami, Los Angeles, San Francisco and Philadelphia, with 6 buckets of 2°F. `check-live` showed 12 open temperature events on October 31, 2026.

**It's a different contract.** Markets settle on the NWS Climatological Report (Daily) over the local standard-time day. That's the sensor's own maximum, not the highest report, which is why the model adds [sensor gap and opening terms](model.md#5-bucket-probabilities).

**Fees.** Coefficient 0.0695 from October 19, 2026, with no maker rebate below $10M of monthly maker volume ([fees](https://docs.polymarket.us/fees.md), [a third-party summary](https://www.rivermarkets.com/insights/polymarket-us-fees.html)).

**Market data.** `gateway.polymarket.us` serves public data. `/v1/event/slug` returned 404, so the bot uses `/v1/events?slug=`. In [quote history](https://docs.polymarket.us/api-reference/price-history/get-price-history.md), `longPrice` is the ask and `shortPrice` is one minus the bid. These markets aren't on Gamma, so settlement also reads from the gateway.

**Orders.** `api.polymarket.us`, signed with [Ed25519](https://docs.polymarket.us/api-reference/authentication.md). [`POST /v1/orders`](https://docs.polymarket.us/api-reference/orders/create-order.md) documents only 200, 400, 401 and 500, and has no idempotency key or client order id, so a lost reply can't be retried safely or matched later. That's the reason for the fail-closed design in `live.py`. See also [orders](https://docs.polymarket.us/concepts/orders.md) and [rate limits](https://docs.polymarket.us/api-reference/rate-limits.md).

**Quotes lock late in the day.** They tend to lock around 20:15 UTC on the target day. The cause is probably the NWS afternoon preliminary report, but that's inference.

**Illinois: unconfirmed.** The Illinois Gaming Board's [cease-and-desist list](https://igb.illinois.gov/sports-wagering/cease-and-desist-letters.html) has an entry dated January 27, 2026, reported as addressed to QCX LLC d/b/a Polymarket US. The [CFTC has sued Illinois](https://capitolnewsillinois.com/news/tag/polymarket/) over the question. [Trackers](https://coinstats.app/prediction-markets/legal/polymarket-il/) call it available but contested. No primary list settles it, so the `live.jurisdiction_confirmed` gate stays false until a person has checked.

## METAR

Source: the [Aviation Weather Center API](https://aviationweather.gov/data/api/). It allows 100 requests a minute, and the bot sends its own User-Agent.

- The fields used are `icaoId`, `obsTime`, `rawOb`, `maxT` (the 6-hour group) and `maxT24` (the 24-hour group).
- Only US stations report tenths of a degree. Elsewhere readings are whole °C.
- The archive ends at about 30 days and thins out from about 25.
- A reply is capped at 400 rows.
- Every configured station stayed under 2.6 hours between reports over 204 station-days in October 2026, so a hole over 3 hours means something is wrong.

**The sensor gap.** Over 112 NWS station-days in October 2026, the sensor's maximum beat the highest hourly report by 0°F 46.4% of the time, 1°F 41.1%, 2°F 8.0%, 3°F 2.7% and 4°F 1.8%. No free source has the 5-minute data that would close this gap.

## Open-Meteo

**Ensembles.** The [ensemble API](https://open-meteo.com/en/docs/ensemble-api) gives GFS with 31 members and ECMWF IFS with 51. Part of this data is 3-hourly and interpolated, so an hourly maximum can understate the real peak.

**Time zones.** `timezone=auto` ignores DST changes, so requests use `timeformat=unixtime` and the bot works out each day from the station's zone.

**No past ensembles.** The ensemble API has no `run=` parameter, and past dates come back null. [Single Runs](https://open-meteo.com/en/docs/single-runs-api) covers deterministic models back to about April 2, 2026, which is what the backtest uses. `gfs025` comes back null there, so the replay uses `gfs_global`. See also [Previous Runs](https://open-meteo.com/en/docs/previous-runs-api).

**Rate limits.** The free tier allows about 10,000 weighted calls a day and 600 a minute. At the configured station count, refetching every 15 minutes would cost about 37,800 a day, against about 1,574 for fetching only new runs ([call weights](https://openmeteo.substack.com/p/weather-data-for-multiple-locations)).

**Licence.** The [free tier is non-commercial](https://open-meteo.com/en/terms), and the data is CC BY 4.0, so attribution is required. [Pricing](https://open-meteo.com/en/pricing) says ensemble data needs Professional or above. Paper research was read as fitting the free tier, which is a judgement call, not legal advice. Trading for profit needs a paid key, and `check-live` enforces that.
