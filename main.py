#!/usr/bin/env python3
"""SMC/ICT DeepSeek futures signal agent.

  6:00-21:00 local : scan every 5 min -> snapshot -> agent -> Telegram
  24/7             : every open signal checked every minute for SL/TP
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

C = {"g": "\033[32m", "y": "\033[33m", "c": "\033[36m", "r": "\033[31m",
     "d": "\033[2m", "b": "\033[1m", "0": "\033[0m"}


def setup_logging():
    fmt = f"{C['d']}%(asctime)s{C['0']} %(levelname)-5s {C['c']}%(name)-8s{C['0']} %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt="%H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(config.LOG_DIR / "agent.log",
                                              maxBytes=8_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)-8s %(message)s"))
    logging.getLogger().addHandler(fh)
    for n in ("httpx", "httpcore", "telegram", "ccxt", "openai", "asyncio"):
        logging.getLogger(n).setLevel(logging.WARNING)


log = logging.getLogger("main")


def local_now() -> datetime:
    return datetime.now(config.LOCAL_TZ)


def seconds_to_next_scan() -> float:
    """Align scans to the 15m candle close so the newest bar is always complete."""
    if not config.ALIGN_TO_CANDLE:
        return float(config.SCAN_SECONDS)
    now = time.time()
    step = config.SCAN_SECONDS
    nxt = (now // step + 1) * step + config.ALIGN_OFFSET
    if nxt - now < 5:
        nxt += step
    return nxt - now


def in_window(now: datetime | None = None) -> bool:
    now = now or local_now()
    t = now.time()
    if config.ACTIVE_START <= config.ACTIVE_END:
        return config.ACTIVE_START <= t < config.ACTIVE_END
    return t >= config.ACTIVE_START or t < config.ACTIVE_END


async def sleep_with_heartbeat(seconds: float, label: str):
    """Sleep while printing the clock, so the terminal never looks frozen."""
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return
        print(f"{C['d']}[{local_now():%Y-%m-%d %H:%M:%S}] {label} "
              f"— {int(left)}s{C['0']}", end="\r", flush=True)
        await asyncio.sleep(min(config.HEARTBEAT_SECONDS, max(1, left)))


class Scanner:
    def __init__(self, stream: MarketStream, agent: Agent):
        self.stream = stream
        self.agent = agent
        self.n = 0

    def build_snapshot(self) -> tuple[list[dict], int, int]:
        coins, built, skipped = [], 0, 0
        for u in self.stream.universe:
            sym = u["symbol"]
            if not self.stream.ready(sym):
                continue
            try:
                c = features.build_coin(u, self.stream.frames[sym])
            except Exception as ex:
                log.debug("build failed %s: %s", sym, ex)
                continue
            if not c:
                continue
            built += 1
            if features.is_actionable(c):
                c["rs"] = self.stream.relative_strength(sym)
                coins.append((c, sym))
            else:
                skipped += 1
        return coins, built, skipped

    async def cycle(self):
        self.n += 1
        t0 = time.monotonic()
        print(" " * 78, end="\r")
        log.info("%sscan #%d starting%s", C["b"], self.n, C["0"])

        coins, built, skipped = await asyncio.get_running_loop().run_in_executor(
            None, self.build_snapshot)
        if not built:
            log.warning("no coins ready - websockets may still be warming up")
            return
        log.info("snapshot: %d built in %.1fs · %d actionable · %d skipped "
                 "(no level within %.1f%% of CMP)", built, time.monotonic() - t0,
                 len(coins), skipped, config.GATE_NEAR_PCT)
        if not coins:
            log.info("%snothing actionable this scan - no API call made%s",
                     C["y"], C["0"])
            tgbot.STATE["last_scan"] = f"#{self.n} {local_now():%H:%M} (0 sent)"
            return

        if tgbot.STATE.get("paused"):
            log.info("dispatch paused - snapshot discarded")
            return

        # positioning data only for the coins that survived the gate
        try:
            extras = await self.stream.fetch_extras([sym for _, sym in coins])
            for rich, sym in coins:
                if sym in extras:
                    rich["mkt"] = extras[sym]
            log.info("enriched %d/%d coins with funding / OI / long-short",
                     len(extras), len(coins))
        except Exception as ex:
            log.warning("extras fetch failed, continuing without: %s", ex)

        encoded = [(features.encode_coin(rich), sym) for rich, sym in coins]
        self.agent.tb.symbols = {enc["s"]: full for enc, full in encoded}
        sent = await self.agent.analyse([enc for enc, _ in encoded])
        row = db.usage()
        log.info("%sscan #%d done in %.0fs — %d signal(s) · today $%.3f%s",
                 C["g"] if sent else C["y"], self.n, time.monotonic() - t0,
                 sent, db.cost_of(row), C["0"])
        tgbot.STATE["last_scan"] = f"#{self.n} {local_now():%H:%M} ({sent} signals)"

    async def run_forever(self):
        while True:
            now = local_now()
            active = in_window(now)
            tgbot.STATE["in_window"] = active
            if active:
                start = time.monotonic()
                try:
                    await self.cycle()
                except Exception as ex:
                    log.exception("scan failed: %s", ex)
                wait = (seconds_to_next_scan() if config.ALIGN_TO_CANDLE
                        else max(5.0, config.SCAN_SECONDS - (time.monotonic() - start)))
                await sleep_with_heartbeat(wait, "next 15m close in")
            else:
                log.info("%soutside %s–%s %s — scanner idle, monitor still running%s",
                         C["y"], config.ACTIVE_START.strftime("%H:%M"),
                         config.ACTIVE_END.strftime("%H:%M"), config.LOCAL_TZ.key,
                         C["0"])
                await sleep_with_heartbeat(60, "idle (outside trading window)")


async def amain():
    setup_logging()
    db.conn()

    missing = [k for k, v in (("DEEPSEEK_API_KEY", config.API_KEY),
                              ("TELEGRAM_BOT_TOKEN", config.TG_TOKEN),
                              ("TELEGRAM_CHAT_ID", config.TG_CHAT)) if not v]
    if missing:
        raise SystemExit("Missing in .env: " + ", ".join(missing))

    log.info("model=%s effort=%s window=%s-%s %s", config.MODEL,
             config.REASONING_EFFORT, config.ACTIVE_START.strftime("%H:%M"),
             config.ACTIVE_END.strftime("%H:%M"), config.LOCAL_TZ.key)

    stream = MarketStream()
    await stream.start()

    toolbox = ToolBox(stream)
    agent = Agent(toolbox)
    monitor = Monitor(stream)
    scanner = Scanner(stream, agent)

    app = tgbot.build()
    tg.set_bot(app.bot)
    tgbot.STATE.update({"stream": stream, "monitor": monitor, "paused": False})
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await tg.send(f"🤖 <b>SMC/ICT agent online</b>\n{len(stream.universe)} coins "
                  f"&gt;{config.MIN_24H_QUOTE_VOLUME / 1e6:.0f}M · scan every "
                  f"{config.SCAN_SECONDS // 60}m · "
                  f"{config.ACTIVE_START:%H:%M}–{config.ACTIVE_END:%H:%M} "
                  f"{config.LOCAL_TZ.key}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (sig.SIGINT, sig.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass

    tasks = [asyncio.create_task(scanner.run_forever()),
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
