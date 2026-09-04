"""pandas_ta indicators + the compact snapshot handed to the agent.

The snapshot deliberately carries NO raw candles. Candles are the densest token
sink and the least useful thing to dump on a model wholesale, so the agent pulls
them per coin through its tools only when a coin looks worth a closer look.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# pandas_ta 0.3.14b still imports numpy.NaN, removed in numpy 2.x
if not hasattr(np, "NaN"):
    np.NaN = np.nan
import pandas_ta as ta  # noqa: E402

import config  # noqa: E402
from core import smc  # noqa: E402
from math import floor, log10  # noqa: E402

KILLZONES = [("asia", 0, 5), ("london", 7, 10), ("ny_am", 12, 15), ("ny_pm", 18, 20)]


def indicators(df: pd.DataFrame) -> dict:
    """pandas_ta indicator block for one timeframe."""
    close = df["close"]
    rsi = ta.rsi(close, length=14)
    e20 = ta.ema(close, length=20)
    e50 = ta.ema(close, length=50)
    e200 = ta.ema(close, length=200)
    atr = ta.atr(df["high"], df["low"], close, length=14)
    vma = ta.sma(df["volume"], length=20)

    def last(s):
        if s is None or not len(s):
            return None
        s = s.dropna()
        return float(s.iloc[-1]) if len(s) else None

    price = float(close.iloc[-1])
    a = last(atr) or 0.0
    ev = [last(e20), last(e50), last(e200)]
    stack = ("bull" if all(x is not None for x in ev) and ev[0] >= ev[1] >= ev[2]
             else "bear" if all(x is not None for x in ev) and ev[0] <= ev[1] <= ev[2]
             else "mix")
    r = last(rsi)
    rprev = None
    if rsi is not None and len(rsi.dropna()) > 5:
        rprev = float(rsi.dropna().iloc[-6])
    vm = last(vma) or 0.0
    return {
        "rsi": round(r, 1) if r else None,
        "rsi_d": round(r - rprev, 1) if r and rprev else None,
        "ema": [round((price - x) / price * 100, 2) if x else None for x in ev],
        "stack": stack,
        "atr": round(a / price * 100, 3) if price else None,
        "rvol": round(float(df["volume"].iloc[-1]) / vm, 2) if vm else None,
        "_atr_abs": a,
    }


def timeframe_block(df: pd.DataFrame, price: float) -> dict:
    o, h, l, c, v = smc.arrays(df)
    typ = (h + l + c) / 3.0
    ind = indicators(df)
    atr = ind.pop("_atr_abs")

    bias, events, sh, sl, ph, pl = smc.structure(h, l, c, config.SWING)
    fvg = smc.find_fvg(o, h, l, c, v, typ, atr, config.KEEP_FVG)
    obs, broken = smc.find_ob(o, h, l, c, v, typ, atr, sh, sl, config.KEEP_OB)
    bb = smc.find_breakers(broken, h, l, c, v, typ, config.KEEP_BB)
    liq = smc.liquidity(h, l, c, v, typ, sh, sl, atr, config.KEEP_LIQ)
    rng = smc.dealing_range(h, l, c, sh, sl)

    for z in fvg:
        z.pop("_i", None)
        for k in ("top", "bot", "ce"):
            z[k] = smc.rnd(z[k], price)
    for z in obs + bb:
        z.pop("_i", None)
        z.pop("_v", None)
        for k in ("hi", "lo", "bhi", "blo"):
            if k in z:
                z[k] = smc.rnd(z[k], price)
    for side in ("u", "d", "sw"):
        for z in liq[side]:
            z["lvl"] = smc.rnd(z["lvl"], price)
    if rng:
        for k in ("hi", "lo", "eq"):
            rng[k] = smc.rnd(rng[k], price)
        rng["ote"] = [smc.rnd(x, price) for x in rng["ote"]]

    last_ev = events[-2:]
    block = {
        "seq": smc.swing_sequence(sh, sl, h, l),
        "atr_abs": smc.rnd(atr, price),
        "bias": bias,
        "ev": [f"{t} {d} {int(c.size - 1 - i)}b @{smc.rnd(lv, price)}"
               for t, d, i, lv in last_ev],
        "sh": smc.rnd(h[sh[-1]] if sh.size else None, price),
        "sl": smc.rnd(l[sl[-1]] if sl.size else None, price),
        "ind": ind,
    }
    if fvg:
        block["fvg"] = fvg
    if obs:
        block["ob"] = obs
    if bb:
        block["bb"] = bb
    liq = {k: val for k, val in liq.items() if val}
    if liq:
        block["liq"] = liq
    if rng:
        block["rng"] = rng
    return block


def sessions(df: pd.DataFrame, price: float) -> dict:
    ts = pd.to_datetime(df["ts"], unit="ms", utc=True)
    now = ts.iloc[-1]
    hour = now.hour + now.minute / 60
    kz = next((n for n, s, e in KILLZONES if s <= hour < e), "off")
    today = now.normalize()
    out = {"kz": kz, "h": round(hour, 1)}
    y = df[(ts >= today - pd.Timedelta(days=1)) & (ts < today)]
    if len(y):
        out["pdh"] = smc.rnd(y["high"].max(), price)
        out["pdl"] = smc.rnd(y["low"].min(), price)
    a = df[(ts >= today) & (ts.dt.hour < 5)]
    if len(a):
        out["ah"] = smc.rnd(a["high"].max(), price)
        out["al"] = smc.rnd(a["low"].min(), price)
    return out


def build_coin(meta: dict, frames: dict[str, pd.DataFrame]) -> dict | None:
    base = frames.get("15m")
    if base is None or len(base) < 60:
        return None
    price = float(base["close"].iloc[-1])
    if price <= 0:
        return None
    out = {"s": meta["symbol"], "p": smc.rnd(price, price),
           "qv24": int(meta.get("qv24", 0)),
           "chg": round(meta.get("chg", 0.0), 1), "tf": {}}
    for tf in config.TIMEFRAMES:
        df = frames.get(tf)
        if df is not None and len(df) >= 60:
            out["tf"][tf] = timeframe_block(df, price)
    if len(out["tf"]) < len(config.TIMEFRAMES):
        return None

    h1 = frames.get("1h").iloc[-config.SR_LOOKBACK:]
    o, h, l, c, v = smc.arrays(h1)
    typ = (h + l + c) / 3
    atr = ta.atr(h1["high"], h1["low"], h1["close"], length=14)
    atr = float(atr.dropna().iloc[-1]) if atr is not None and len(atr.dropna()) else 0.0
    sr = smc.support_resistance(h, l, c, v, typ, atr, config.KEEP_SR)
    for side in ("s", "r"):
        for x in sr[side]:
            x["lvl"] = smc.rnd(x["lvl"], price)
    out["sr"] = sr
    out["sess"] = sessions(frames["15m"], price)

    # volume profile over the same 200 1h candles used for support/resistance
    vp = smc.volume_profile(h, l, c, v, typ)
    if vp:
        out["vp"] = {k: smc.rnd(x, price) for k, x in vp.items()}

    # daily and weekly opens - ICT reads price above/below them as daily bias
    ts1 = pd.to_datetime(frames["1h"]["ts"], unit="ms", utc=True)
    now = ts1.iloc[-1]
    d0 = frames["1h"][ts1 >= now.normalize()]
    w0 = frames["1h"][ts1 >= (now.normalize() - pd.Timedelta(days=int(now.dayofweek)))]
    out["opens"] = {"d": smc.rnd(d0["open"].iloc[0], price) if len(d0) else None,
                    "w": smc.rnd(w0["open"].iloc[0], price) if len(w0) else None}

    # the three most recent 15m candles - the shape of the entry bar
    tail = frames["15m"].iloc[-3:]
    out["c3"] = [[smc.rnd(r.open, price), smc.rnd(r.high, price),
                  smc.rnd(r.low, price), smc.rnd(r.close, price)]
                 for r in tail.itertuples()]
    return out


# ---------------------------------------------------------------------------
# Pre-send gate. Skips coins with nothing an SMC trader could act on, which is
# the largest single token saving available. It never judges a setup - it only
# drops coins with no live POI near price, no recent sweep and no recent
# structure shift. Set PREFILTER=0 in .env to send every coin instead.
# ---------------------------------------------------------------------------
def _pois(coin: dict, tfs):
    """Yield (timeframe, kind, polarity, low, high, zone) for every live POI."""
    for tf in tfs:
        b = coin["tf"].get(tf) or {}
        for z in b.get("fvg", []):
            yield tf, "fvg", z["k"], z["bot"], z["top"], z
        for z in b.get("ob", []):
            yield tf, "ob", z["k"], z["lo"], z["hi"], z
        for z in b.get("bb", []):
            yield tf, "bb", z["k"], z["lo"], z["hi"], z


def is_actionable(coin: dict) -> bool:
    """Decide whether a coin is worth an API call this scan.

    Zones reaching here are already fresh-only, so every POI considered is
    unmitigated. Stricter modes push more of the screening into Python: that
    cuts tokens hard, but the agent can only choose from what it is shown, so
    a wrong bias call here silently hides the coin.
    """
    if not config.PREFILTER:
        return True
    p = coin.get("p") or 0
    if p <= 0:
        return False
    mode = config.GATE_MODE
    tol = config.GATE_NEAR_PCT

    def near(level) -> bool:
        return level is not None and abs(level - p) / p * 100 <= tol

    # ---- any: CMP close to any level at all, S/R and liquidity included ----
    if mode == "any":
        want = config.GATE_LEVELS
        for tf in config.PREFILTER_TFS:
            b = coin["tf"].get(tf) or {}
            if "fvg" in want:
                for z in b.get("fvg", []):
                    if z["bot"] <= p <= z["top"] or near(z["bot"]) or near(z["top"]):
                        return True
            for kind, key in (("ob", "ob"), ("bb", "bb")):
                if kind in want:
                    for z in b.get(key, []):
                        if z["lo"] <= p <= z["hi"] or near(z["lo"]) or near(z["hi"]):
                            return True
            if "liq" in want:
                for side in ("u", "d", "sw"):
                    for z in b.get("liq", {}).get(side, []):
                        if near(z["lvl"]):
                            return True
        if "sr" in want:
            for side in ("s", "r"):
                for z in coin.get("sr", {}).get(side, []):
                    if near(z["lvl"]):
                        return True
        return False

    tfs = config.GATE_POI_TFS
    b1 = (coin["tf"].get("1h") or {}).get("bias")
    b4 = (coin["tf"].get("4h") or {}).get("bias")

    for tf, kind, pol, lo, hi, z in _pois(coin, tfs):
        inside = lo <= p <= hi

        if mode == "near":
            if not (inside or near(lo) or near(hi)):
                continue
            return True

        if not inside:
            continue
        if mode == "inside":
            return True

        # ---- confluence ----
        if pol not in (b1, b4):
            continue                                  # POI fights the HTF story
        if kind == "ob" and not z.get("bos"):
            continue                                  # OB never broke structure
        rng = (coin["tf"].get(tf) or {}).get("rng")
        if not rng:
            continue
        if pol == "bull" and rng["z"] != "disc":
            continue                                  # buying premium
        if pol == "bear" and rng["z"] != "prem":
            continue                                  # selling discount
        return True
    return False


# ---------------------------------------------------------------------------
# Wire encoding. The rich dict above is what the engine and the gate work with;
# this collapses it to positional arrays for transport. Key names repeated on
# every zone object were most of the payload - the legend lives in the cached
# system prompt instead, so it is paid for once.
# ---------------------------------------------------------------------------
def _sig(x, n: int = None):
    if x is None:
        return None
    x = float(x)
    if x == 0 or not np.isfinite(x):
        return 0
    n = n or config.SIG_DIGITS
    d = n - 1 - floor(log10(abs(x)))
    return round(x, d) if d > 0 else int(round(x, d))


def _m(x) -> float:
    """Quote volume in millions - 143009593 is four tokens, 143.01 is two.

    Three decimals so a genuinely thin zone ($9k) reads as 0.009 rather than
    0.0, which the agent would read as 'no money here at all'.
    """
    return round((x or 0) / 1e6, 3)


def encode_coin(coin: dict) -> dict:
    """Rich snapshot dict -> compact wire form (~1.1k tokens instead of ~3.9k)."""
    p = coin["p"]
    out = {"s": coin["s"].split(":")[0], "p": _sig(p), "v": _m(coin["qv24"]),
           "ch": coin["chg"], "tf": {}}

    for tf, b in coin["tf"].items():
        near = config.ZONE_NEAR_PCT.get(tf, 3.0)

        def close(level) -> bool:
            return abs(level - p) / p * 100 <= near

        i = b["ind"]
        blk = {"b": b["bias"], "q": b.get("seq"), "e": b["ev"],
               "i": [i["rsi"], i["rsi_d"], i["ema"][0], i["ema"][1], i["ema"][2],
                     i["stack"][0], i["atr"], i["rvol"], b.get("atr_abs")]}

        f = [[z["k"][0], _sig(z["top"]), _sig(z["bot"]), z["age"], z["fill"],
              _m(z["fq"]), _m(z["rq"])]
             for z in b.get("fvg", []) if close((z["top"] + z["bot"]) / 2)]
        o = [[z["k"][0], _sig(z["hi"]), _sig(z["lo"]), z["age"], z["bos"],
              z["tap"], z["disp"], _m(z["qv"]), _m(z["rq"])]
             for z in b.get("ob", []) if close((z["hi"] + z["lo"]) / 2)]
        k = [[z["k"][0], _sig(z["hi"]), _sig(z["lo"]), z["age"], z["rt"],
              _m(z["rq"])]
             for z in b.get("bb", []) if close((z["hi"] + z["lo"]) / 2)]
        if f:
            blk["f"] = f
        if o:
            blk["o"] = o
        if k:
            blk["k"] = k

        lq = {}
        for side in ("u", "d", "sw"):
            arr = [[_sig(z["lvl"]), z["t"], z["n"], z["age"], _m(z["qv"])]
                   + ([z.get("ago"), z.get("rec")] if side == "sw" else [])
                   for z in b.get("liq", {}).get(side, [])[:2]]
            if arr:
                lq[side] = arr
        if lq:
            blk["l"] = lq

        r = b.get("rng")
        if r:
            blk["r"] = [_sig(r["hi"]), _sig(r["lo"]), r["pos"], r["z"][0],
                        _sig(r["ote"][0]), _sig(r["ote"][1])]
        out["tf"][tf] = blk

    out["sr"] = [[_sig(x["lvl"]), x["n"], _m(x["qv"])]
                 for x in coin["sr"]["s"][:2] + coin["sr"]["r"][:2]]
    se = coin["sess"]
    out["z"] = [se.get("kz"), _sig(se.get("pdh")), _sig(se.get("pdl")),
                _sig(se.get("ah")), _sig(se.get("al"))]
    vp = coin.get("vp")
    if vp:
        out["vp"] = [_sig(vp["poc"]), _sig(vp["vah"]), _sig(vp["val"])]
    op = coin.get("opens") or {}
    out["op"] = [_sig(op.get("d")), _sig(op.get("w"))]
    if coin.get("c3"):
        out["c3"] = [[_sig(x) for x in row] for row in coin["c3"]]
    if coin.get("rs") is not None:
        out["rs"] = coin["rs"]
    m = coin.get("mkt")
    if m:
        out["m"] = [m.get("fr"), m.get("oi"), m.get("oi1"), m.get("oi4"), m.get("ls")]
    return out
