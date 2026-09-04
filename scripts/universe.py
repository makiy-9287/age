#!/usr/bin/env python3
"""Count the live Binance USDⓈ-M universe at several volume thresholds.

Answers "how many coins do I actually lose going 20M -> 50M" with real numbers
instead of an estimate, and projects the token cost of each choice.
Run:  python3 scripts/universe.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ccxt.pro as ccxtpro  # noqa: E402
import config  # noqa: E402

THRESHOLDS = [10, 20, 30, 50, 75, 100, 200]
KB_PER_COIN = 6.33      # measured; check preflight for your real figure
CHARS_PER_TOKEN = 1.6


async def main():
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "future"}})
    try:
        await ex.load_markets()
        tickers = await ex.fetch_tickers()
        vols = []
        for sym, t in tickers.items():
            m = ex.markets.get(sym)
            if not m or not m.get("swap") or not m.get("active"):
                continue
            if m.get("quote") != "USDT" or m.get("settle") != "USDT":
                continue
            qv = t.get("quoteVolume")
            if qv:
                vols.append((sym, float(qv)))
    finally:
        await ex.close()

    vols.sort(key=lambda x: -x[1])
    scans = int((config.ACTIVE_END.hour - config.ACTIVE_START.hour)
                * 3600 / config.SCAN_SECONDS)
    gate = 0.77

    print(f"{len(vols)} active USDT perpetuals · {scans} scans/day\n")
    print(f"{'filter':>7}{'coins':>7}{'sent*':>7}{'req':>5}{'tok/scan':>11}{'tok/day':>10}"
          f"{'$/day**':>9}")
    for th in THRESHOLDS:
        n = sum(1 for _, v in vols if v >= th * 1e6)
        sent = n * gate
        per = sent * KB_PER_COIN * 1024 / CHARS_PER_TOKEN
        day = per * scans
        req = -(-int(sent) // config.COINS_PER_REQUEST) if sent else 0
        mark = "  <- current" if abs(th * 1e6 - config.MIN_24H_QUOTE_VOLUME) < 1 else ""
        print(f"{th:>6}M{n:>7}{sent:>7.0f}{req:>5}{per/1000:>10.0f}k"
              f"{day/1e6:>9.1f}M{day/1e6*config.PRICE_IN_MISS:>9.2f}{mark}")

    print(f"\n * after the pre-send gate (~{gate:.0%} pass; set PREFILTER=0 to send all)")
    print(f"** at PRICE_IN_MISS=${config.PRICE_IN_MISS}/M — set this from your "
          f"DeepSeek dashboard")

    cur = sum(1 for _, v in vols if v >= config.MIN_24H_QUOTE_VOLUME)
    at50 = sum(1 for _, v in vols if v >= 50e6)
    dropped = [s.split(":")[0] for s, v in vols
               if config.MIN_24H_QUOTE_VOLUME <= v < 50e6]
    print(f"\ngoing to 50M drops {cur - at50} coins, keeps {at50}")
    if dropped:
        print("dropped: " + ", ".join(dropped[:30])
              + (f" … +{len(dropped)-30} more" if len(dropped) > 30 else ""))


if __name__ == "__main__":
    asyncio.run(main())
