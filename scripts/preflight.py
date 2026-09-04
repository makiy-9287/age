#!/usr/bin/env python3
"""Verify the box before running main.py.

Checks env, exchange + universe, websocket, one full coin build, the pre-send
gate hit rate, Telegram delivery and one live DeepSeek call, then projects the
daily token volume. Run:  python3 scripts/preflight.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


async def main() -> int:
    ok = True
    print("== config ==")
    print(f"  model            : {config.MODEL}  (effort={config.REASONING_EFFORT})")
    print(f"  base url         : {config.BASE_URL}")
    print(f"  api key          : {'set' if config.API_KEY else 'MISSING'}")
    print(f"  telegram         : "
          f"{'set' if config.TG_TOKEN and config.TG_CHAT else 'MISSING'}")
    print(f"  window           : {config.ACTIVE_START:%H:%M}-{config.ACTIVE_END:%H:%M}"
          f" {config.LOCAL_TZ.key}")
    print(f"  filter           : {config.MIN_24H_QUOTE_VOLUME/1e6:.0f}M 24h volume")
    print(f"  scan             : every {config.SCAN_SECONDS//60} min, "
          f"{len(config.TIMEFRAMES)} timeframes x {config.CANDLES} candles")
    print(f"  prefilter        : {'ON' if config.PREFILTER else 'OFF'} "
          f"(POI within {config.PREFILTER_NEAR_PCT}%, "
          f"{config.PREFILTER_FRESH_BARS} bars)")
    if not (config.API_KEY and config.TG_TOKEN and config.TG_CHAT):
        ok = False

    print("\n== exchange / websocket ==")
    from core.stream import MarketStream
    from core import features
    stream = MarketStream()
    n_universe = 0
    kb = 0.0
    try:
        t0 = time.time()
        universe = await stream.load_universe()
        n_universe = len(universe)
        print(f"  universe         : {n_universe} symbols "
              f"({time.time()-t0:.1f}s)")
        if universe:
            print(f"  top              : "
                  f"{', '.join(u['symbol'].split(':')[0] for u in universe[:6])}")
            stream.universe = universe[:6]
            t0 = time.time()
            await stream.seed()
            print(f"  seeded 6 symbols : {time.time()-t0:.1f}s "
                  f"-> ~{n_universe/6*(time.time()-t0):.0f}s for the full set")

            sym = stream.universe[0]["symbol"]
            t0 = time.time()
            coin = features.build_coin(stream.universe[0], stream.frames[sym])
            build_ms = (time.time() - t0) * 1000
            blob = json.dumps(coin, separators=(",", ":"), default=float)
            kb = len(blob) / 1024
            print(f"  build {sym.split(':')[0]:<12}: {kb:.2f} KB in {build_ms:.0f} ms")

            passed = sum(1 for u in stream.universe
                         if (c := features.build_coin(u, stream.frames[u["symbol"]]))
                         and features.is_actionable(c))
            print(f"  gate sample      : {passed}/6 coins actionable right now")

            t0 = time.time()
            task = asyncio.create_task(stream._run_tickers())
            await asyncio.sleep(6)
            task.cancel()
            print(f"  ticker socket    : {len(stream.prices)} live prices "
                  f"in {time.time()-t0:.0f}s")
    except Exception as e:
        print(f"  FAILED: {e}")
        ok = False
    finally:
        await stream.close()

    if n_universe and kb:
        print("\n== token projection ==")
        cpt = 1.6
        scans = int((config.ACTIVE_END.hour - config.ACTIVE_START.hour)
                    * 3600 / config.SCAN_SECONDS)
        for rate, label in ((1.0, "gate OFF"), (0.5, "gate ~50% pass"),
                            (0.3, "gate ~30% pass")):
            coins = n_universe * rate
            per = coins * kb * 1024 / cpt
            day = per * scans
            print(f"  {label:<16}: {coins:>4.0f} coins · {per/1000:>5.0f}k tok/scan "
                  f"· {day/1e6:>6.1f}M tok/day")
        print(f"  scans/day        : {scans}  "
              f"({config.ACTIVE_START:%H:%M}-{config.ACTIVE_END:%H:%M})")
        print(f"  cost at your rates: input miss ${config.PRICE_IN_MISS}/M -> "
              f"set PRICE_IN_MISS in .env from your DeepSeek dashboard")

    print("\n== telegram ==")
    try:
        from tg import send as tg
        mid = await tg.send("✅ Preflight: Telegram works.")
        print(f"  sent (id {mid})" if mid else "  FAILED to send")
        ok = ok and bool(mid)
    except Exception as e:
        print(f"  FAILED: {e}")
        ok = False

    print("\n== deepseek ==")
    try:
        from openai import AsyncOpenAI
        c = AsyncOpenAI(api_key=config.API_KEY, base_url=config.BASE_URL, timeout=60)
        kw = dict(model=config.MODEL,
                  messages=[{"role": "user", "content": "Reply with one word: READY"}])
        try:
            r = await c.chat.completions.create(
                **kw, extra_body={"reasoning_effort": config.REASONING_EFFORT,
                                  "thinking": {"type": "enabled"}})
            print(f"  reasoning params : accepted")
        except Exception as e:
            print(f"  reasoning params : REJECTED ({str(e).split(chr(10))[0][:90]})")
            print("                     the agent will fall back automatically")
            r = await c.chat.completions.create(**kw)
        print(f"  model reply      : {(r.choices[0].message.content or '').strip()[:40]}")
        u = r.usage
        print(f"  usage fields     : "
              f"hit={getattr(u,'prompt_cache_hit_tokens','n/a')} "
              f"miss={getattr(u,'prompt_cache_miss_tokens','n/a')} "
              f"out={getattr(u,'completion_tokens','n/a')}")
    except Exception as e:
        print(f"  FAILED: {str(e).split(chr(10))[0][:200]}")
        print("  -> check DEEPSEEK_MODEL is the exact string your account exposes")
        ok = False

    print("\n" + ("ALL CHECKS PASSED - start with: python3 main.py" if ok
                  else "SOME CHECKS FAILED - fix the above first"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
