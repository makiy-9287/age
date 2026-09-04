# SMC / ICT DeepSeek Futures Signal Agent

CCXT Pro websockets feed the data, pandas + pandas_ta structure it, DeepSeek
analyses it and dispatches its own signals through its Telegram tool.

```
06:00–21:00 Asia/Colombo        every 5 min
  websockets ─► pandas_ta + SMC engine ─► compact snapshot ─► DeepSeek
                                                                │
                          tools: fetch_candles / compute_indicators / get_zones
                                                                │
                                                      send_signal ─► Telegram + SQLite
24/7
  every open signal checked every 60s for SL / TP ─► live Telegram updates
```

## Data

Per coin the agent receives, in ~1,070 tokens: structure and swing sequence
(HH/HL/LH/LL) on each timeframe, fresh OB / FVG / breaker zones with their
volume and resting liquidity, liquidity pools and sweeps, dealing range with
OTE, RSI / EMA / ATR (absolute and %) / relative volume, 1h support-resistance,
volume profile (POC and value area), daily and weekly opens, session killzone
with PDH/PDL and the Asian range, the last three 15m candles, relative strength
versus BTC, and futures positioning — funding rate, open interest with 1h and
4h change, and the long/short account ratio.

Positioning data is fetched **after** the gate, only for the handful of coins
that survived it, so it costs a few requests per scan rather than one per
symbol in the universe.


- Binance USDⓈ-M perpetuals with **24h quote volume > 20M**
- **15m, 1h, 4h**, 300 candles each. 15m is the entry timeframe; there is no
  5m and no scalp mode — signals are **day** or **swing** only.
- **Fresh zones only.** Order blocks and FVGs are filtered in Python before the
  JSON is built: never mitigated, under 50% filled, under 80 bars old. The one
  exception is a zone price is sitting inside *right now* — that is the live
  test and the CMP entry candidate, so it is kept even though `tapped=1`.
- Support/resistance is clustered from the **last 200 1h candles** only.
- REST seeds the 300 candles once at startup; after that **CCXT Pro websockets**
  keep them current. No weighted REST calls in the loop.
- A second socket streams tickers for the trade monitor.

Per timeframe the engine measures:

- market structure: swings, BOS / CHoCH with bars-ago
- **FVG** (live only): top, bottom, CE, size in ATR, fill %, formation quote
  volume, and **resting quote volume** — the money actually traded inside the gap
- **Order Blocks**: displacement-validated, wick and body bounds, `bos`, `tap`,
  OB quote volume, impulse quote volume, resting liquidity
- **Breaker Blocks**: an OB that was violated and flipped polarity, with retest
  state, break volume and resting liquidity
- liquidity: EQH / EQL clusters, unswept pools above and below, sweeps in the
  last 20 bars with reclaim flag, quote volume at each level
- dealing range with premium/discount and the OTE band
- pandas_ta: RSI + 5-bar delta, EMA 20/50/200 as % distance, stack, ATR%, rvol
- 1h support/resistance clusters, killzone, PDH/PDL, Asian range

## Token conservation

Per coin: **~1,070 tokens** (1.7 KB), down from ~3,950. Five things do it.

**1. No raw candles in the snapshot.** Only structure: FVG, OB, breaker,
CHoCH/BOS, liquidity, S/R, dealing range, CMP, and the volume + resting money in
each zone. The agent pulls candles per coin through `fetch_candles` when it
wants a closer look.

**2. Positional arrays, not objects.** `{"k":"bull","top":61397.69,...}` is
~40 chars of key names for ~60 chars of data, repeated on every zone across four
timeframes. Now it is `["b",61398,61393,68,0,0.14,0]` with the legend in the
system prompt — paid for once, and cached.

**3. Quote volumes in millions.** `143009593` is four tokens; `143.01` is two.
There are three to five of these per zone.

**4. Prices at 5 significant digits.** `61397.69` → `61398`. Below the tick size
that matters for a level.

**5. Zones filtered by distance from price, per timeframe** — 15m within 2%,
1h 3.5%, 4h 6%. A 15m entry zone 3% away is noise; a 4h swing zone 5% away is
not. If a timeframe has no zone key, nothing relevant is in range.

The pre-send gate then decides which coins are worth an API call, and the scan
runs once per closed 15m candle in a 15-hour window — **60 scans/day**, aligned
to the close so the newest bar is always complete.

`GATE_MODE` measured on test data, 63-coin universe at ~907 tokens/coin:

| mode | pass | coins | tokens/scan | tokens/day |
|---|---|---|---|---|
| `any` | ~100% | 63 | 57k | 3.43M |
| `near` | ~81% | 51 | 46k | 2.79M |
| `inside` | ~30% | 19 | 17k | 1.03M |
| **`confluence`** (default) | **~10%** | **6** | **6k** | **0.34M** |

`confluence` requires price sitting *inside* a live unmitigated POI on 15m or
1h, with that POI's polarity matching the 1h or 4h bias, an order block that
broke structure, and price in the correct premium/discount half.

**The trade-off is real.** At `confluence` the Python gate is making a large
part of the SMC judgement, and the agent can only choose from what it is shown —
if the bias or structure call in `core/smc.py` is wrong on a coin, that coin
never reaches the model and you never hear about the setup. `inside` keeps the
gate mechanical (is price in a live zone, yes or no) and leaves the
interpretation to the agent at 3x the tokens, which is still only ~1M/day. If
signal count comes out too low after a week, step back to `inside`.

**Set `PRICE_IN_MISS` in `.env` from your DeepSeek dashboard.** You quoted the
cache-*hit* rate; the coin JSON is new every scan so it is always a cache *miss*,
and that is where the cost lands. `/cost` reports real spend from the API's own
`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`.

## Agent tools

| tool | backed by | purpose |
|---|---|---|
| `fetch_candles` | CCXT Pro websocket store | recent OHLCV for one symbol/timeframe |
| `compute_indicators` | pandas_ta | RSI, EMA, ATR, MACD, Bollinger, volume |
| `get_zones` | SMC engine | every OB / breaker / FVG with volume and resting liquidity |
| `send_signal` | python-telegram-bot + SQLite | dispatch a validated setup |

Only `send_signal` has side effects. Tool rounds are capped at
`MAX_TOOL_ROUNDS` (default 3) so a curious agent cannot run up the bill.

Every `send_signal` is validated before it reaches Telegram: symbol must be in
that batch, level ordering must be directionally correct, stop under 15% of
price, TP1 at least 1.5R, confidence ≥ 7, at least 3 confirmations, no duplicate
open signal on the same symbol.

## Trade monitor

Runs 24/7 on live websocket prices, including outside the scan window. Between
minute checks it tracks the running high/low from the ticker stream, so a wick
that touches a level and retraces inside the same minute is still caught.

- `PENDING` → `ACTIVE` when price enters the entry zone
- stopped before filling → `INVALID`; untouched past `PENDING_EXPIRY_MIN` → `EXPIRED`
- `ACTIVE` → TP1 / TP2 / TP3 each pushed to Telegram; SL or TP3 closes it
- PnL % and R stored per signal, equal thirds at TP1/2/3 (`TP_SPLIT`)

## Install

```bash
bash scripts/install.sh
nano .env                       # DeepSeek key, Telegram token + chat id
./venv/bin/python scripts/preflight.py
./venv/bin/python main.py
```

`preflight.py` checks the API key, verifies whether your account accepts the
reasoning parameters, measures real per-coin size against the live universe and
projects your daily token volume — all for one cheap API call.

As a service:

```bash
sudo cp scripts/smcagent.service /etc/systemd/system/
sudo nano /etc/systemd/system/smcagent.service   # fix User= and paths
sudo systemctl daemon-reload && sudo systemctl enable --now smcagent
journalctl -u smcagent -f
```

## Telegram commands

`/active` `/pnl [24h|7d|30d|all]` `/report` `/last [n]` `/cost` `/status`
`/close ID` `/pause` `/resume`

## Notes on the model string

`deepseek-v4-flash` is set from `.env`. If the API returns a model-not-found
error, check the exact string your account exposes and update `DEEPSEEK_MODEL`.
`reasoning_effort` and `thinking` are sent via `extra_body`; if your endpoint
rejects them the client logs a warning once and retries without them, so a
parameter mismatch can never stall a scan.

## Layout

```
config.py              all tunables
main.py                scan window, 5-min loop, terminal heartbeat
core/stream.py         CCXT Pro websockets + REST seed
core/smc.py            OB, breaker, FVG, liquidity, structure, dealing range
core/features.py       pandas_ta, snapshot builder, pre-send gate
agent/prompt.py        SMC/ICT ruleset + schema legend
agent/tools.py         tool schemas, validation, dispatch
agent/client.py        DeepSeek tool loop, token accounting
monitor/tracker.py     minute-by-minute SL/TP
storage/db.py          signals, events, usage
tg/                    sender + commands
```

## Note

This produces automated trade *ideas* from a model, not advice. Leveraged
futures can lose more than you put in. Paper-trade it and check `/pnl` before
risking money.
