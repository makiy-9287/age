#!/usr/bin/env python3
"""SMC/ICT DeepSeek futures agent — 4h scan, 1h drill-down, hourly watch.

  05:00 local   rebuild the top-50 watchlist
  4h closes     scan all 50 on 4h -> agent screens -> 1h drill-down per hit
  1h closes     re-check every coin under watch
  always        open signals checked every 60s for SL/TP
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal as sig
import time
from datetime import datetime

import config
from agent.client import Agent
from agent.tools import ToolBox
from core import features
from core.stream import MarketStream
from monitor.tracker import Monitor
from storage import db
from tg import bot as tgbot
from tg import send as tg

C = {"g": "\033[32m", "y": "\033[33m", "c": "\033[36m", "d": "\033[2m",
     "b": "\033[1m", "0": "\033[0m"}


def setup_logging():
    fmt = (f"{C['d']}%(asctime)s{C['0']} %(levelname)-5s "
           f"{C['c']}%(name)-7s{C['0']} %(message)s")
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt="%H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(config.LOG_DIR / "agent.log",
                                              maxBytes=8_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)-7s %(message)s"))
    logging.getLogger().addHandler(fh)
    for n in ("httpx", "httpcore", "telegram", "ccxt", "openai", "asyncio"):
        logging.getLogger(n).setLevel(logging.WARNING)


log = logging.getLogger("main")


def now_local() -> datetime:
    return datetime.now(config.LOCAL_TZ)


def in_window(now: datetime | None = None) -> bool:
    t = (now or now_local()).time()
    if config.ACTIVE_START <= config.ACTIVE_END:
        return config.ACTIVE_START <= t < config.ACTIVE_END
    return t >= config.ACTIVE_START or t < config.ACTIVE_END


def next_close(step: int) -> float:
    """Seconds until the next candle close of `step` seconds, plus the offset."""
    now = time.time()
    nxt = (now // step + 1) * step + config.ALIGN_OFFSET
    if nxt - now < 5:
        nxt += step
    return nxt - now


async def heartbeat(seconds: float, label: str):
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return
        m, s = divmod(int(left), 60)
        print(f"{C['d']}[{now_local():%Y-%m-%d %H:%M:%S}] {label} — "
              f"{m}m {s:02d}s{C['0']}", end="\r", flush=True)
        await asyncio.sleep(min(config.HEARTBEAT_SECONDS, max(1, left)))


class Engine:
    def __init__(self, stream: MarketStream, agent: Agent):
        self.stream = stream
        self.agent = agent
        self.scan_no = 0
        self._last_refresh_day = None

    # ------------------------------------------------------------- 4h scan
    def build_htf(self):
        views, encoded = {}, []
        for u in self.stream.universe:
            sym = u["symbol"]
            df = self.stream.get(sym, config.HTF)
            try:
                v = features.build_view(u, df, config.HTF)
            except Exception as ex:
                log.debug("build failed %s: %s", sym, ex)
                continue
            if not v:
                continue
            views[sym] = v
            encoded.append((v, sym))
        return views, encoded

    async def scan(self):
        self.scan_no += 1
        t0 = time.monotonic()
        print(" " * 78, end="\r")
        log.info("%s4h scan #%d — %d coins%s", C["b"], self.scan_no,
                 len(self.stream.universe), C["0"])

        views, pairs = await asyncio.get_running_loop().run_in_executor(
            None, self.build_htf)
        if not pairs:
            log.warning("no 4h data ready")
            return

        extras = {}
        try:
            extras = await self.stream.fetch_extras([s for _, s in pairs])
        except Exception as ex:
            log.warning("extras unavailable: %s", ex)

        encoded = []
        for v, sym in pairs:
            ex = {"rs": self.stream.relative_strength(sym), "mkt": extras.get(sym)}
            encoded.append(features.encode_view(v, ex))
        self.agent.tb.symbols = {e["s"]: s for e, (_, s) in zip(encoded, pairs)}

        near = sum(1 for v, _ in pairs
                   if (features.nearest_poi_pct(v) or 99) <= config.POI_MAX_DIST_PCT)
        log.info("built %d views in %.1fs · %d with a POI within %.1f%% of CMP",
                 len(encoded), time.monotonic() - t0, near, config.POI_MAX_DIST_PCT)

        if tgbot.STATE.get("paused"):
            log.info("dispatch paused — scan discarded")
            return

        drills = await self.agent.scan(encoded)
        log.info("agent requested %d 1h drill-down(s)", len(drills))

        sent = 0
        for sym, reason in drills:
            df = self.stream.get(sym, config.LTF)
            v = features.build_view({"symbol": sym,
                                     "qv24": next((u["qv24"] for u in self.stream.universe
                                                   if u["symbol"] == sym), 0),
                                     "chg": 0.0}, df, config.LTF)
            if not v:
                continue
            enc = features.encode_view(v, {"rs": self.stream.relative_strength(sym),
                                           "mkt": extras.get(sym)})
            self.agent.tb.symbols[enc["s"]] = sym
            sent += await self.agent.drill(sym, reason, enc)

        row = db.usage()
        log.info("%sscan #%d done in %.0fs — %d drill-down(s), %d signal(s), "
                 "%d watching · today $%.4f%s", C["g"] if sent else C["y"],
                 self.scan_no, time.monotonic() - t0, len(drills), sent,
                 len(db.watches()), db.cost_of(row), C["0"])
        tgbot.STATE["last_scan"] = (f"#{self.scan_no} {now_local():%H:%M} "
                                    f"({len(drills)} drills, {sent} signals)")

    # --------------------------------------------------------- hourly watch
    async def hourly(self):
        db.purge_watches()
        rows = db.watches()
        if not rows:
            return
        log.info("%shourly re-check — %d coin(s) under watch%s",
                 C["b"], len(rows), C["0"])
        for w in rows:
            sym = w["symbol"]
            df = self.stream.get(sym, config.LTF)
            if df is None:
                db.drop_watch(sym)
                continue
            v = features.build_view({"symbol": sym, "qv24": 0, "chg": 0.0},
                                    df, config.LTF)
            if not v:
                continue
            enc = features.encode_view(v, {"rs": self.stream.relative_strength(sym)})
            self.agent.tb.symbols[enc["s"]] = sym
            db.bump_watch(sym)
            hours = (db.now() - w["created_at"]) / 3600
            await self.agent.recheck(w, enc, hours)

    # ---------------------------------------------------------------- loops
    async def scan_loop(self):
        while True:
            wait = next_close(config.HTF_SECONDS)
            await heartbeat(wait, "next 4h close in")
            if not in_window():
                log.info("%s4h close outside %s–%s %s — skipping scan%s", C["y"],
                         config.ACTIVE_START.strftime("%H:%M"),
                         config.ACTIVE_END.strftime("%H:%M"),
                         config.LOCAL_TZ.key, C["0"])
                continue
            try:
                await self.scan()
            except Exception as ex:
                log.exception("scan failed: %s", ex)

    async def hourly_loop(self):
        while True:
            wait = next_close(config.LTF_SECONDS)
            await asyncio.sleep(wait)
            if not in_window():
                continue
            try:
                await self.hourly()
            except Exception as ex:
                log.exception("hourly re-check failed: %s", ex)

    async def refresh_loop(self):
        while True:
            now = now_local()
            day = now.date()
            if (self._last_refresh_day != day
                    and now.time() >= config.WATCHLIST_REFRESH):
                self._last_refresh_day = day
                try:
                    log.info("%sdaily watchlist refresh%s", C["b"], C["0"])
                    await self.stream.refresh_watchlist()
                except Exception as ex:
                    log.exception("watchlist refresh failed: %s", ex)
            await asyncio.sleep(60)


async def amain():
    setup_logging()
    db.conn()
    missing = [k for k, v in (("DEEPSEEK_API_KEY", config.API_KEY),
                              ("TELEGRAM_BOT_TOKEN", config.TG_TOKEN),
                              ("TELEGRAM_CHAT_ID", config.TG_CHAT)) if not v]
    if missing:
        raise SystemExit("Missing in .env: " + ", ".join(missing))

    log.info("model=%s effort=%s · top %d coins · %s x%d scan, %s x%d drill",
             config.MODEL, config.REASONING_EFFORT, config.WATCHLIST_SIZE,
             config.HTF, config.HTF_CANDLES, config.LTF, config.LTF_CANDLES)

    stream = MarketStream()
    await stream.start()
    tb = ToolBox(stream)
    agent = Agent(tb)
    monitor = Monitor(stream)
    engine = Engine(stream, agent)

    app = tgbot.build()
    tg.set_bot(app.bot)
    tgbot.STATE.update({"stream": stream, "monitor": monitor, "engine": engine,
                        "paused": False})
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await tg.send(f"🤖 <b>SMC/ICT agent online</b>\nTop {len(stream.universe)} coins · "
                  f"4h scan at each close · {config.ACTIVE_START:%H:%M}–"
                  f"{config.ACTIVE_END:%H:%M} {config.LOCAL_TZ.key}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (sig.SIGINT, sig.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass

    tasks = [asyncio.create_task(engine.scan_loop()),
             asyncio.create_task(engine.hourly_loop()),
             asyncio.create_task(engine.refresh_loop()),
             asyncio.create_task(monitor.run_forever()),
             asyncio.create_task(monitor.sample_forever())]
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            pass
        await stream.close()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
