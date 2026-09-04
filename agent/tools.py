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
from core import smc
from storage import db
from tg import send as tg

log = logging.getLogger("tools")

SIGNAL_FN = {"type": "function", "function": {
    "name": "send_signal",
    "description": "Dispatch one validated A+ SMC/ICT setup to Telegram.",
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
        "htf_bias": {"type": "string", "description": "the 4h narrative"},
        "entry_tf": {"type": "string", "enum": ["1h", "4h"]},
        "poi": {"type": "string",
                "enum": ["FVG", "OB", "BREAKER", "OTE", "SWEEP"]},
        "confirmations": {"type": "array", "items": {"type": "string"},
                          "description": "at least 3"},
        "reasoning": {"type": "string"}},
        "required": ["symbol", "direction", "mode", "entry_type", "entry_low",
                     "entry_high", "stop_loss", "tp1", "tp2", "tp3",
                     "confidence", "htf_bias", "entry_tf", "poi",
                     "confirmations", "reasoning"]}}}

GET_1H_FN = {"type": "function", "function": {
    "name": "get_1h_context",
    "description": ("Return the 1h structural payload for ONE coin whose 4h "
                    "scan showed a live POI near CMP. Costs tokens - only call "
                    "it for coins you would actually trade."),
    "parameters": {"type": "object", "properties": {
        "symbol": {"type": "string"},
        "reason": {"type": "string",
                   "description": "which 4h POI and why, in one line"}},
        "required": ["symbol", "reason"]}}}

WATCH_FN = {"type": "function", "function": {
    "name": "watch_hourly",
    "description": ("Put a coin under hourly re-check until its trigger fires "
                    "or it invalidates. Use when the entry is close but needs "
                    "another 1h candle."),
    "parameters": {"type": "object", "properties": {
        "symbol": {"type": "string"},
        "bias": {"type": "string"},
        "poi": {"type": "string", "description": "the zone being watched"},
        "trigger": {"type": "string", "description": "concrete entry condition"},
        "invalidation": {"type": "string", "description": "concrete price level"}},
        "required": ["symbol", "bias", "poi", "trigger", "invalidation"]}}}

KEEP_FN = {"type": "function", "function": {
    "name": "keep_watching",
    "description": "Setup still forming, invalidation not hit. Stay on watch.",
    "parameters": {"type": "object", "properties": {
        "symbol": {"type": "string"}, "note": {"type": "string"}},
        "required": ["symbol", "note"]}}}

DROP_FN = {"type": "function", "function": {
    "name": "drop_watch",
    "description": "Setup is dead or invalidated. Stop watching this coin.",
    "parameters": {"type": "object", "properties": {
        "symbol": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["symbol", "reason"]}}}

SCAN_TOOLS = [GET_1H_FN]
DRILL_TOOLS = [SIGNAL_FN, WATCH_FN]
WATCH_TOOLS = [SIGNAL_FN, KEEP_FN, DROP_FN]


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
    """Binds tool names to the live market stream."""

    def __init__(self, stream):
        self.stream = stream
        self.known: set[str] = set()
        self.symbols: dict[str, str] = {}
        self.drills: list[tuple[str, str]] = []   # (symbol, reason) requested
        self.watch_calls: list[dict] = []
        self.drop_calls: list[dict] = []
        self.keep_calls: list[dict] = []

    def resolve(self, name: str) -> str:
        name = (name or "").strip()
        return self.symbols.get(name, self.symbols.get(name.split(":")[0], name))

    # -------------------------------------------------------- stage 1 request
    def request_1h(self, symbol: str, reason: str) -> dict:
        full = self.resolve(symbol)
        if self.stream.get(full, config.LTF) is None:
            return {"error": f"no 1h data for {symbol}"}
        if len(self.drills) >= config.MAX_DRILLDOWNS:
            return {"error": "drill-down budget for this scan is used up"}
        if any(sym == full for sym, _ in self.drills):
            return {"status": "already queued"}
        self.drills.append((full, reason))
        return {"status": "queued",
                "note": "1h payload will be delivered in the next stage"}

    # --------------------------------------------------------- watch handling
    def watch(self, a: dict) -> dict:
        sym = self.resolve(a.get("symbol", ""))
        if not sym or self.stream.get(sym, config.LTF) is None:
            return {"error": "unknown symbol"}
        if len(db.watches()) >= config.MAX_WATCHES:
            return {"error": "watch list is full"}
        db.add_watch(sym, config.WATCH_MAX_HOURS,
                     bias=str(a.get("bias", ""))[:120],
                     poi=str(a.get("poi", ""))[:120],
                     trigger=str(a.get("trigger", ""))[:200],
                     invalidation=str(a.get("invalidation", ""))[:120])
        self.watch_calls.append({"symbol": sym})
        log.info("WATCH  %s · trigger: %s", sym.split(":")[0],
                 str(a.get("trigger", ""))[:80])
        return {"status": "watching", "expires_hours": config.WATCH_MAX_HOURS}

    def keep(self, a: dict) -> dict:
        sym = self.resolve(a.get("symbol", ""))
        db.bump_watch(sym)
        self.keep_calls.append({"symbol": sym})
        log.info("  keep  %s · %s", sym.split(":")[0], str(a.get("note", ""))[:80])
        return {"status": "still watching"}

    def drop(self, a: dict) -> dict:
        sym = self.resolve(a.get("symbol", ""))
        db.drop_watch(sym)
        self.drop_calls.append({"symbol": sym})
        log.info("  drop  %s · %s", sym.split(":")[0], str(a.get("reason", ""))[:80])
        return {"status": "dropped"}

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
        db.drop_watch(s["symbol"])
        mid = await tg.send(tg.signal_text(s, sid))
        if mid:
            db.update(sid, msg_id=mid)
        log.info("SIGNAL #%d %s %s %s conf=%d rr=%.2f", sid,
                 s["symbol"].split(":")[0], s["direction"], s["mode"],
                 s["confidence"], s["rr"])
        return {"status": "sent", "signal_id": sid}
