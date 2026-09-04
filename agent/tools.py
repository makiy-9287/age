"""Tools the agent can call.

  fetch_candles    -> CCXT Pro websocket store (no REST, no weight)
  compute_indicators -> pandas_ta on demand
  get_zones        -> full OB / Breaker / FVG detail for one timeframe
  send_signal      -> python-telegram-bot + database

Only send_signal has side effects. The read tools exist so the snapshot can stay
tiny: the agent pays tokens for detail only on coins it actually cares about.
"""
from __future__ import annotations

import logging

import config
from core import features, smc
from storage import db
from tg import send as tg

log = logging.getLogger("tools")

TOOLS = [
    {"type": "function", "function": {
        "name": "fetch_candles",
        "description": ("Recent OHLCV for one symbol/timeframe from the live "
                        "websocket store. Use only when the snapshot is not "
                        "enough to judge a setup."),
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "exact symbol from the snapshot"},
            "timeframe": {"type": "string", "enum": config.TIMEFRAMES},
            "limit": {"type": "integer", "description": f"1-{config.MAX_TOOL_CANDLES}"}},
            "required": ["symbol", "timeframe"]}}},
    {"type": "function", "function": {
        "name": "compute_indicators",
        "description": ("Full pandas_ta indicator set (RSI, EMA20/50/200, ATR, "
                        "MACD, Bollinger, volume) for one symbol/timeframe."),
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string"},
            "timeframe": {"type": "string", "enum": config.TIMEFRAMES}},
            "required": ["symbol", "timeframe"]}}},
    {"type": "function", "function": {
        "name": "get_zones",
        "description": ("Every order block, breaker block and fair value gap on "
                        "one timeframe with volume, quote value and resting "
                        "liquidity - more than the snapshot's trimmed list."),
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string"},
            "timeframe": {"type": "string", "enum": config.TIMEFRAMES}},
            "required": ["symbol", "timeframe"]}}},
    {"type": "function", "function": {
        "name": "send_signal",
        "description": ("Dispatch one validated A+ SMC/ICT sniper setup to "
                        "Telegram. Call once per qualifying coin."),
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string"},
            "direction": {"type": "string", "enum": ["LONG", "SHORT"]},
            "mode": {"type": "string", "enum": ["day", "swing"]},
            "entry_type": {"type": "string", "enum": ["CMP", "LIMIT"]},
            "entry_low": {"type": "number"}, "entry_high": {"type": "number"},
            "stop_loss": {"type": "number"},
            "tp1": {"type": "number"}, "tp2": {"type": "number"},
            "tp3": {"type": "number"},
            "confidence": {"type": "integer", "description": "1-10, send only >=7"},
            "htf_bias": {"type": "string"},
            "entry_tf": {"type": "string", "enum": config.TIMEFRAMES},
            "poi": {"type": "string",
                    "enum": ["FVG", "OB", "BREAKER", "OTE", "SWEEP"]},
            "confirmations": {"type": "array", "items": {"type": "string"},
                              "description": "at least 3, one from 1h or 4h"},
            "reasoning": {"type": "string"}},
            "required": ["symbol", "direction", "mode", "entry_type", "entry_low",
                         "entry_high", "stop_loss", "tp1", "tp2", "tp3",
                         "confidence", "htf_bias", "entry_tf", "poi",
                         "confirmations", "reasoning"]}}},
]


class Rejected(Exception):
    pass


def _validate(a: dict, known: set[str]) -> dict:
    sym = str(a.get("symbol", "")).strip()
    if sym not in known:
        short = sym.split(":")[0]
        if short in known:
            sym = short
        else:
            raise Rejected(f"unknown symbol {sym!r}")
    d = str(a.get("direction", "")).upper()
    if d not in ("LONG", "SHORT"):
        raise Rejected("bad direction")
    mode = str(a.get("mode", "")).lower()
    if mode not in config.MODES:
        raise Rejected("bad mode")
    et = str(a.get("entry_type", "")).upper()
    if et not in ("CMP", "LIMIT"):
        raise Rejected("bad entry_type")
    try:
        lo, hi = float(a["entry_low"]), float(a["entry_high"])
        sl = float(a["stop_loss"])
        t1, t2, t3 = float(a["tp1"]), float(a["tp2"]), float(a["tp3"])
    except (KeyError, TypeError, ValueError) as ex:
        raise Rejected(f"bad number: {ex}")
    if lo > hi:
        lo, hi = hi, lo
    if min(lo, hi, sl, t1, t2, t3) <= 0:
        raise Rejected("non-positive price")
    if d == "LONG" and not (sl < lo <= hi < t1 < t2 < t3):
        raise Rejected("LONG level order invalid")
    if d == "SHORT" and not (sl > hi >= lo > t1 > t2 > t3):
        raise Rejected("SHORT level order invalid")
    ref = (lo + hi) / 2
    risk = abs(ref - sl)
    if risk <= 0:
        raise Rejected("zero risk")
    if risk / ref > 0.15:
        raise Rejected("stop wider than 15%")
    rr = abs(t1 - ref) / risk
    if rr < config.MIN_RR:
        raise Rejected(f"TP1 R:R {rr:.2f} < {config.MIN_RR}")
    conf = int(a.get("confidence") or 0)
    if conf < config.MIN_CONFIDENCE:
        raise Rejected(f"confidence {conf} < {config.MIN_CONFIDENCE}")
    confirms = [str(x) for x in (a.get("confirmations") or [])][:6]
    if len(confirms) < 3:
        raise Rejected("fewer than 3 confirmations")
    return {"symbol": sym, "direction": d, "mode": mode, "entry_type": et,
            "entry_low": lo, "entry_high": hi, "stop_loss": sl,
            "tp1": t1, "tp2": t2, "tp3": t3, "confidence": conf,
            "htf_bias": str(a.get("htf_bias", ""))[:160],
            "entry_tf": str(a.get("entry_tf", ""))[:6],
            "poi": str(a.get("poi", ""))[:16],
            "confirmations": confirms,
            "reasoning": str(a.get("reasoning", ""))[:900],
            "rr": round(rr, 2)}


class ToolBox:
    """Binds the tool names to the live market stream."""

    def __init__(self, stream):
        self.stream = stream
        self.known: set[str] = set()       # short names sent to the agent
        self.symbols: dict[str, str] = {}  # short -> full ccxt symbol

    def resolve(self, name: str) -> str:
        name = (name or "").strip()
        return self.symbols.get(name, self.symbols.get(name.split(":")[0], name))

    # ------------------------------------------------------------ read tools
    def fetch_candles(self, symbol: str, timeframe: str, limit: int = 30) -> dict:
        symbol = self.resolve(symbol)
        df = self.stream.get(symbol, timeframe)
        if df is None or not len(df):
            return {"error": "no data"}
        n = max(1, min(int(limit or 30), config.MAX_TOOL_CANDLES))
        tail = df.iloc[-n:]
        price = float(df["close"].iloc[-1])
        return {"symbol": symbol, "timeframe": timeframe,
                "format": "[open,high,low,close,volume] oldest first",
                "candles": [[smc.rnd(r.open, price), smc.rnd(r.high, price),
                             smc.rnd(r.low, price), smc.rnd(r.close, price),
                             int(r.volume)] for r in tail.itertuples()]}

    def compute_indicators(self, symbol: str, timeframe: str) -> dict:
        symbol = self.resolve(symbol)
        df = self.stream.get(symbol, timeframe)
        if df is None or len(df) < 60:
            return {"error": "no data"}
        import pandas_ta as ta
        ind = features.indicators(df)
        ind.pop("_atr_abs", None)
        close = df["close"]
        macd = ta.macd(close)
        bb = ta.bbands(close, length=20)
        price = float(close.iloc[-1])
        if macd is not None and len(macd.dropna()):
            row = macd.dropna().iloc[-1]
            ind["macd"] = [round(float(x), 6) for x in row.tolist()[:3]]
        if bb is not None and len(bb.dropna()):
            row = bb.dropna().iloc[-1].tolist()
            ind["bb"] = [smc.rnd(row[0], price), smc.rnd(row[1], price),
                         smc.rnd(row[2], price)]
        return {"symbol": symbol, "timeframe": timeframe, "indicators": ind}

    def get_zones(self, symbol: str, timeframe: str) -> dict:
        symbol = self.resolve(symbol)
        df = self.stream.get(symbol, timeframe)
        if df is None or len(df) < 60:
            return {"error": "no data"}
        price = float(df["close"].iloc[-1])
        blk = features.timeframe_block(df, price)
        return {"symbol": symbol, "timeframe": timeframe,
                "fvg": blk.get("fvg", []), "ob": blk.get("ob", []),
                "bb": blk.get("bb", []), "liq": blk.get("liq", {}),
                "rng": blk.get("rng")}

    # ------------------------------------------------------------ write tool
    async def send_signal(self, args: dict) -> dict:
        try:
            s = _validate(args, self.known)
        except Rejected as ex:
            log.warning("rejected %s: %s", args.get("symbol"), ex)
            return {"status": "rejected", "reason": str(ex)}

        s["symbol"] = self.resolve(s["symbol"])
        if db.open_for(s["symbol"]):
            return {"status": "skipped", "reason": "already open on this symbol"}
        if db.recent(s["symbol"], s["direction"], config.DUP_COOLDOWN_MIN):
            return {"status": "skipped", "reason": "duplicate within cooldown"}

        price = self.stream.price(s["symbol"])
        if s["entry_type"] == "CMP":
            s["status"] = "ACTIVE"
            s["entry_price"] = price or (s["entry_low"] + s["entry_high"]) / 2
            s["activated_at"] = db.now()
        else:
            s["status"] = "PENDING"

        sid = db.insert(s)
        db.event(sid, "CREATED", s.get("entry_price"), s["poi"])
        mid = await tg.send(tg.signal_text(s, sid))
        if mid:
            db.update(sid, msg_id=mid)
        log.info("SIGNAL #%d %s %s %s conf=%d rr=%.2f", sid,
                 s["symbol"].split(":")[0], s["direction"], s["mode"],
                 s["confidence"], s["rr"])
        return {"status": "sent", "signal_id": sid}
