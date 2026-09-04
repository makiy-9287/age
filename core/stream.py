"""CCXT Pro websocket market data.

REST seeds 300 candles once, then websockets keep them current. After the seed
there are no weighted REST calls in the hot loop at all.
"""
from __future__ import annotations

import asyncio
import logging

import ccxt.pro as ccxtpro
import pandas as pd

import config

log = logging.getLogger("stream")

COLS = ["ts", "open", "high", "low", "close", "volume"]


class MarketStream:
    def __init__(self):
        self.ex = ccxtpro.binanceusdm({
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        self.frames: dict[str, dict[str, pd.DataFrame]] = {}
        self.prices: dict[str, float] = {}
        self.universe: list[dict] = []
        self._tasks: list[asyncio.Task] = []
        self._seeded = asyncio.Event()

    # ------------------------------------------------------------------ setup
    async def load_universe(self) -> list[dict]:
        await self.ex.load_markets()
        tickers = await self.ex.fetch_tickers()
        out = []
        for sym, t in tickers.items():
            m = self.ex.markets.get(sym)
            if not m or not m.get("swap") or not m.get("active"):
                continue
            if m.get("quote") != "USDT" or m.get("settle") != "USDT":
                continue
            qv = t.get("quoteVolume")
            if qv is None or float(qv) < config.MIN_24H_QUOTE_VOLUME:
                continue
            out.append({"symbol": sym, "base": m.get("base"),
                        "qv24": float(qv),
                        "chg": float(t.get("percentage") or 0.0)})
            self.prices[sym] = float(t.get("last") or 0.0)
        out.sort(key=lambda x: -x["qv24"])
        self.universe = out
        log.info("universe: %d symbols over %.0fM 24h volume",
                 len(out), config.MIN_24H_QUOTE_VOLUME / 1e6)
        return out

    async def seed(self):
        """One-off REST backfill of `CANDLES` bars for every symbol/timeframe."""
        sem = asyncio.Semaphore(8)

        async def one(sym, tf):
            async with sem:
                for attempt in range(3):
                    try:
                        raw = await self.ex.fetch_ohlcv(sym, tf, limit=config.CANDLES)
                        if raw and len(raw) >= 60:
                            df = pd.DataFrame(raw, columns=COLS)
                            self.frames.setdefault(sym, {})[tf] = df
                        return
                    except Exception as e:
                        if attempt == 2:
                            log.debug("seed failed %s %s: %s", sym, tf, e)
                        await asyncio.sleep(1 + attempt)

        jobs = [one(u["symbol"], tf) for u in self.universe for tf in config.TIMEFRAMES]
        await asyncio.gather(*jobs)
        ready = sum(1 for s in self.frames if len(self.frames[s]) == len(config.TIMEFRAMES))
        log.info("seeded %d/%d symbols x %d timeframes",
                 ready, len(self.universe), len(config.TIMEFRAMES))
        self._seeded.set()

    # ------------------------------------------------------------- websockets
    async def _run_ohlcv(self):
        pairs = [[u["symbol"], tf] for u in self.universe for tf in config.TIMEFRAMES]
        while True:
            try:
                updates = await self.ex.watch_ohlcv_for_symbols(pairs)
                for sym, per_tf in updates.items():
                    for tf, candles in per_tf.items():
                        self._merge(sym, tf, candles)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("ohlcv socket: %s - reconnecting in 5s",
                            str(e).split("\n")[0][:160])
                await asyncio.sleep(5)

    async def _run_tickers(self):
        while True:
            try:
                tickers = await self.ex.watch_tickers()
                for sym, t in tickers.items():
                    last = t.get("last") or t.get("close")
                    if last:
                        self.prices[sym] = float(last)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("ticker socket: %s - reconnecting in 5s",
                            str(e).split("\n")[0][:160])
                await asyncio.sleep(5)

    def _merge(self, sym: str, tf: str, candles):
        df = self.frames.get(sym, {}).get(tf)
        if df is None or not candles:
            return
        for c in candles:
            ts = int(c[0])
            row = [ts, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])]
            if len(df) and int(df.iat[-1, 0]) == ts:
                df.iloc[-1] = row
            elif not len(df) or ts > int(df.iat[-1, 0]):
                df.loc[len(df)] = row
        if len(df) > config.CANDLES:
            self.frames[sym][tf] = df.iloc[-config.CANDLES:].reset_index(drop=True)

    async def start(self):
        await self.load_universe()
        await self.seed()
        self._tasks = [asyncio.create_task(self._run_ohlcv()),
                       asyncio.create_task(self._run_tickers())]
        log.info("websockets live (%d streams)",
                 len(self.universe) * len(config.TIMEFRAMES) + 1)

    async def close(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await self.ex.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- access
    def get(self, symbol: str, timeframe: str) -> pd.DataFrame | None:
        return self.frames.get(symbol, {}).get(timeframe)

    def price(self, symbol: str) -> float | None:
        p = self.prices.get(symbol)
        if p:
            return p
        df = self.get(symbol, "15m")
        return float(df.iat[-1, 4]) if df is not None and len(df) else None

    def ready(self, symbol: str) -> bool:
        f = self.frames.get(symbol, {})
        return len(f) == len(config.TIMEFRAMES) and all(len(d) >= 60 for d in f.values())


    # ------------------------------------------------- futures positioning data
    async def fetch_extras(self, symbols: list[str]) -> dict[str, dict]:
        """Funding, open interest change and long/short ratio.

        Fetched AFTER the gate, for the handful of coins that survived it, so it
        costs a few requests per scan rather than one per symbol in the universe.
        Funding tells you where the crowd is paying to stay; open interest rising
        into a sweep is new money, falling is capitulation being absorbed.
        """
        out: dict[str, dict] = {}
        if not symbols:
            return out

        funding = {}
        try:
            funding = await self.ex.fetch_funding_rates(symbols)
        except Exception as e:
            log.debug("funding fetch failed: %s", e)

        sem = asyncio.Semaphore(6)

        async def one(sym: str):
            row: dict = {}
            f = funding.get(sym)
            if f and f.get("fundingRate") is not None:
                row["fr"] = round(float(f["fundingRate"]) * 100, 4)
            try:
                async with sem:
                    hist = await self.ex.fetch_open_interest_history(sym, "1h", limit=5)
                if hist:
                    cur = float(hist[-1].get("openInterestValue")
                                or hist[-1].get("openInterestAmount") or 0)
                    row["oi"] = round(cur / 1e6, 2)
                    if len(hist) >= 2:
                        prev = float(hist[-2].get("openInterestValue")
                                     or hist[-2].get("openInterestAmount") or 0)
                        if prev:
                            row["oi1"] = round((cur - prev) / prev * 100, 2)
                    if len(hist) >= 5:
                        p4 = float(hist[-5].get("openInterestValue")
                                   or hist[-5].get("openInterestAmount") or 0)
                        if p4:
                            row["oi4"] = round((cur - p4) / p4 * 100, 2)
            except Exception as e:
                log.debug("open interest failed %s: %s", sym, e)
            try:
                async with sem:
                    ls = await self.ex.fapiDataGetGlobalLongShortAccountRatio(
                        {"symbol": self.ex.market(sym)["id"], "period": "1h", "limit": 1})
                if ls:
                    row["ls"] = round(float(ls[-1]["longShortRatio"]), 2)
            except Exception as e:
                log.debug("long/short failed %s: %s", sym, e)
            if row:
                out[sym] = row

        await asyncio.gather(*[one(s) for s in symbols], return_exceptions=True)
        return out

    def relative_strength(self, symbol: str, bars: int = 24) -> float | None:
        """1h ROC of the coin minus BTC's - is it leading or lagging the market."""
        btc = self.get("BTC/USDT:USDT", "1h")
        me = self.get(symbol, "1h")
        if btc is None or me is None or len(btc) <= bars or len(me) <= bars:
            return None
        def roc(df):
            a = float(df["close"].iloc[-1]); b = float(df["close"].iloc[-1 - bars])
            return (a - b) / b * 100 if b else 0.0
        return round(roc(me) - roc(btc), 2)
