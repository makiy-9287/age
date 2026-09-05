# SMC / ICT DeepSeek Futures Agent — 4H scan, 1H drill-down, hourly watch

```
05:00 SLST      rebuild the top-50 watchlist by 24h volume
every 4h close  scan all 50 on 4h ──► agent screens for a POI near CMP
                                       │
                                       └─► get_1h_context  (only for hits)
                                              │
                              ┌───────────────┼───────────────┐
                         send_signal    watch_hourly       nothing
                              │               │
                         Telegram        every 1h close: re-read 1h until
                         + SQLite        it triggers, or invalidates, or the
                                         next 4h scan expires it
always          open signals checked every 60s for SL / TP
```

## Timezone — you do not need to change the server clock

The schedule is anchored to `LOCAL_TZ` (Asia/Colombo) and to UTC candle
boundaries, never to the server's clock. An Alibaba box defaults to
Asia/Shanghai; the bot works correctly on it either way.

What *was* confusing: log lines used to print in the server's timezone, so the
log read 08:17 while your clock said 05:47 and the schedule looked broken when
it was not. **Every log line now prints in `LOCAL_TZ`**, and startup prints all
three clocks side by side plus the next four scan times:

```
clocks: server 08:17 (CST) · UTC 00:47 · 05:47 Asia/Colombo  <- all logs use Asia/Colombo
window 05:00-21:00 Asia/Colombo · next scans: Sat 09:30, Sat 13:30, Sat 17:30, Sun 05:30
```

Changing the server timezone with `timedatectl set-timezone Asia/Colombo` is
optional and makes no difference to behaviour.

## Startup scan

`SCAN_ON_START=1` (default) runs one scan immediately on boot rather than
waiting for the next 4h close — starting at 05:47 no longer means idling until
09:30. `MIN_RESCAN_MINUTES=60` guards it, so restarting the process repeatedly
cannot trigger repeated scans. Outside the window it waits as normal.

## Schedule

The 4h scan fires on the 4h candle close. In Sri Lanka time those land at
**05:30, 09:30, 13:30, 17:30** — exactly four inside the 05:00–21:00 window.
The 21:30 close falls just outside, which is why it is four scans and not five.

## Stage 1 — the 4H scan (~558 tokens per coin)

500 4h candles per coin, one timeframe only. Each payload carries: market
structure with BOS / CHoCH events and the swing sequence (HH/HL/LH/LL),
**unmitigated** order blocks and FVGs with their volume and resting monetary
liquidity, breaker blocks (which are broken structure by definition, so they are
exempt from the freshness rule), liquidity pools above and below plus recent
sweeps with reclaim flags, dealing range with premium/discount and the OTE band,
volume profile POC and value area, 1h-derived support/resistance, RSI, EMA
20/50/200, ATR in both % and price, relative volume, killzone, PDH/PDL, daily
and weekly opens, the last two candles, relative strength versus BTC, and
futures positioning — funding rate, open interest with 1h and 4h change, and the
long/short account ratio.

Every zone and level carries a signed `dist%` from CMP so the agent never does
that arithmetic itself.

The agent's only job here is screening: does a live POI sit within
`POI_MAX_DIST_PCT` (0.5%) of CMP and fit the 4h narrative? It cannot signal at
this stage — it has not seen the 1h. For each coin that qualifies it calls
`get_1h_context`.

## Stage 2 — the 1H drill-down (~501 tokens per coin)

300 1h candles, the same structure, for that one coin. The agent then does one
of three things: `send_signal` if the entry is live, `watch_hourly` if it needs
another candle, or nothing.

## Stage 3 — the hourly watch

Coins under watch are re-read at each 1h close with a fresh 1h payload plus the
trigger and invalidation the agent wrote down. It then fires, keeps watching, or
drops. Watches expire after `WATCH_MAX_HOURS` (4) so the next 4h scan starts
clean.

## Token budget

| stage | per call | calls/day | tokens/day |
|---|---|---|---|
| 4h scan | 558 × 50 coins | 4 scans | **112k** |
| 1h drill-down | 501 | ~5 × 4 | ~10k |
| hourly watch | 501 | ~4 × 16h | ~32k |
| system prompts | ~1,000 | mostly cache hits | ~10k |
| | | | **~165k/day** |

That is about **6 days per 1M input tokens**. Output is small: most coins return
`NONE` and a signal is one tool call.

Set `PRICE_IN_MISS` in `.env` from your DeepSeek dashboard — you know the
cache-hit rate, but coin data is new every scan so it is always a cache miss,
and that is where the cost lands. `/cost` reports real spend from the API's own
`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`.

## One interpretation to confirm

"a minimum distance threshold of 0.5% to the POI zone" is implemented as **the
POI must be within 0.5% of CMP** — close enough to trade. If you meant the
opposite (the POI must be at least 0.5% away, so there is room to travel), that
is a one-line change in `agent/prompt.py` and the `POI_MAX_DIST_PCT` comparison
in `core/features.py: nearest_poi_pct`.

## Install

```bash
bash scripts/install.sh
nano .env                       # DeepSeek key, Telegram token + chat id
./venv/bin/python scripts/preflight.py
./venv/bin/python main.py
```

## Telegram

`/active` `/watch` `/pnl [24h|7d|30d|all]` `/report` `/last [n]` `/cost`
`/status` `/close ID` `/pause` `/resume`

`/watch` shows every coin under hourly watch with its POI, trigger,
invalidation, age and check count.

## Layout

```
config.py            all tunables
main.py              4h scan loop, 1h watch loop, 05:00 watchlist refresh
core/stream.py       CCXT Pro websockets, top-50 watchlist, funding / OI / L-S
core/smc.py          OB, breaker, FVG, liquidity, structure, range, POC
core/features.py     pandas_ta, single-timeframe view, wire encoding
agent/prompt.py      scan / drill / watch prompts + schema legend
agent/tools.py       get_1h_context, watch_hourly, keep_watching, drop_watch, send_signal
agent/client.py      three-stage tool loop, token accounting
monitor/tracker.py   minute-by-minute SL/TP
storage/db.py        signals, events, watches, usage
tg/                  sender + commands
```

## Note

These are automated trade *ideas* from a model, not advice. Leveraged futures
can lose more than you put in. Paper-trade it and check `/pnl` first.
