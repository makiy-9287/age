"""Configuration. Everything tunable is here."""
from __future__ import annotations

import os
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _i(k, d):
    try:
        return int(float(os.getenv(k, d)))
    except (TypeError, ValueError):
        return d


def _f(k, d):
    try:
        return float(os.getenv(k, d))
    except (TypeError, ValueError):
        return d


def _t(k, d):
    raw = os.getenv(k, d)
    try:
        h, m = raw.split(":")
        return dtime(int(h), int(m))
    except Exception:
        h, m = d.split(":")
        return dtime(int(h), int(m))


DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
for _d in (DATA_DIR, LOG_DIR):
    _d.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "signals.db"

# ---------------------------------------------------------------- exchange
EXCHANGE = "binanceusdm"
MIN_24H_QUOTE_VOLUME = _f("MIN_24H_QUOTE_VOLUME", 20_000_000)
TIMEFRAMES = ["15m", "1h", "4h"]   # 15m is the entry timeframe
CANDLES = _i("CANDLES", 300)

# ---------------------------------------------------------------- schedule
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TZ", "Asia/Colombo"))
ACTIVE_START = _t("ACTIVE_START", "06:00")
ACTIVE_END = _t("ACTIVE_END", "21:00")
SCAN_SECONDS = _i("SCAN_SECONDS", 900)   # one scan per closed 15m candle
ALIGN_TO_CANDLE = _i("ALIGN_TO_CANDLE", 1)
ALIGN_OFFSET = _i("ALIGN_OFFSET", 15)    # seconds after the close
MONITOR_SECONDS = _i("MONITOR_SECONDS", 60)
HEARTBEAT_SECONDS = _i("HEARTBEAT_SECONDS", 30)

# ---------------------------------------------------------------- SMC engine
SWING = _i("SWING", 2)
SCAN_WINDOW = _i("SCAN_WINDOW", 150)     # candles searched for zones
DISPLACEMENT = _f("DISPLACEMENT", 1.6)   # body > x * mean body(20)
OB_LOOKBACK = _i("OB_LOOKBACK", 10)
# Fresh zones only: an order block price has already returned to, or a gap that
# is mostly filled, is spent. The exception is a zone price is sitting inside
# right now - that is the live test, and dropping it would hide the CMP entry.
FRESH_ONLY = _i("FRESH_ONLY", 1)
MAX_FVG_FILL = _f("MAX_FVG_FILL", 50.0)   # % filled before a gap counts as spent
MAX_ZONE_AGE = _i("MAX_ZONE_AGE", 80)     # bars
SR_LOOKBACK = _i("SR_LOOKBACK", 200)      # candles used for support/resistance

KEEP_FVG = _i("KEEP_FVG", 3)
KEEP_OB = _i("KEEP_OB", 3)
KEEP_BB = _i("KEEP_BB", 2)
KEEP_LIQ = _i("KEEP_LIQ", 3)
KEEP_SR = _i("KEEP_SR", 3)

# ---------------------------------------------------------------- DeepSeek
API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "low").strip()
TEMPERATURE = _f("TEMPERATURE", 0.2)
REQUEST_TIMEOUT = _i("REQUEST_TIMEOUT", 300)

# The snapshot carries NO raw candles - the agent pulls them per coin through
# its tools only when a coin looks interesting. That is the main token saving.
COINS_PER_REQUEST = _i("COINS_PER_REQUEST", 20)
MAX_TOOL_ROUNDS = _i("MAX_TOOL_ROUNDS", 3)
MAX_TOOL_CANDLES = _i("MAX_TOOL_CANDLES", 60)
AGENT_CONCURRENCY = _i("AGENT_CONCURRENCY", 4)

# Wire format: zones are emitted as positional arrays with a legend in the
# (cached) system prompt, prices at 5 significant digits, quote volumes in
# millions. Zones further than this % from price are dropped - a 15m entry zone
# 3% away is noise, a 4h swing zone 5% away is not.
SIG_DIGITS = _i("SIG_DIGITS", 5)
ZONE_NEAR_PCT = {"15m": _f("NEAR_15M", 2.0), "1h": _f("NEAR_1H", 3.5),
                 "4h": _f("NEAR_4H", 6.0)}

# Pre-send gate. A coin is only worth an API call if CMP sits within
# GATE_NEAR_PCT of some actionable level. This is separate from ZONE_NEAR_PCT
# above: the gate decides whether the coin is sent at all, ZONE_NEAR_PCT
# decides how far out the surrounding context travels with it.
PREFILTER = _i("PREFILTER", 1)
GATE_NEAR_PCT = _f("GATE_NEAR_PCT", 1.0)
# GATE_MODE, measured pass rates on test data:
#   any        CMP within GATE_NEAR_PCT of ANY level (incl. S/R + liquidity) ~100%
#   near       CMP within GATE_NEAR_PCT of a live OB/FVG/breaker             ~84%
#   inside     CMP sitting INSIDE a live POI on 15m or 1h                    ~30%
#   confluence inside + POI polarity matches 1h/4h bias + OB needs bos=1
#              + premium/discount agrees                                     ~10%
GATE_MODE = os.getenv("GATE_MODE", "confluence").strip().lower()
GATE_POI_TFS = [x.strip() for x in
                os.getenv("GATE_POI_TFS", "15m,1h").split(",") if x.strip()]
# Which level types count toward the gate. S/R and liquidity lines are dense -
# on most coins something is always within 1% - so including them makes the
# gate close to a no-op (~100% pass). GATE_LEVELS=ob,fvg,bb gates on points of
# interest only and passes ~88%.
GATE_LEVELS = [x.strip() for x in
               os.getenv("GATE_LEVELS", "ob,fvg,bb,sr,liq").split(",") if x.strip()]
PREFILTER_TFS = ["15m", "1h", "4h"]

# USD per 1M tokens, used only for the running cost report
PRICE_IN_HIT = _f("PRICE_IN_HIT", 0.014)
PRICE_IN_MISS = _f("PRICE_IN_MISS", 0.28)
PRICE_OUT = _f("PRICE_OUT", 1.32)

# ---------------------------------------------------------------- telegram
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ---------------------------------------------------------------- signals
MIN_CONFIDENCE = _i("MIN_CONFIDENCE", 7)
MIN_RR = _f("MIN_RR", 1.5)
DUP_COOLDOWN_MIN = _i("DUP_COOLDOWN_MIN", 90)
PENDING_EXPIRY_MIN = _i("PENDING_EXPIRY_MIN", 240)
TP_SPLIT = [1 / 3, 1 / 3, 1 / 3]
MODES = ["day", "swing"]
