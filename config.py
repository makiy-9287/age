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
    try:
        h, m = os.getenv(k, d).split(":")
        return dtime(int(h), int(m))
    except Exception:
        h, m = d.split(":")
        return dtime(int(h), int(m))


DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
for _d in (DATA_DIR, LOG_DIR):
    _d.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "signals.db"

# ------------------------------------------------------------------ exchange
EXCHANGE = "binanceusdm"
WATCHLIST_SIZE = _i("WATCHLIST_SIZE", 50)          # top N by 24h quote volume
MIN_24H_QUOTE_VOLUME = _f("MIN_24H_QUOTE_VOLUME", 20_000_000)

HTF = "4h"                                          # the scan timeframe
LTF = "1h"                                          # the drill-down timeframe
HTF_CANDLES = _i("HTF_CANDLES", 500)
LTF_CANDLES = _i("LTF_CANDLES", 300)
TIMEFRAMES = [LTF, HTF]                             # both streamed over websocket

# ------------------------------------------------------------------ schedule
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TZ", "Asia/Colombo"))
ACTIVE_START = _t("ACTIVE_START", "05:00")
ACTIVE_END = _t("ACTIVE_END", "21:00")
# The 4h scan fires on the 4h candle close. In SLST those land at 05:30, 09:30,
# 13:30, 17:30 inside the window - exactly four scans a day.
HTF_SECONDS = 4 * 3600
LTF_SECONDS = 3600
ALIGN_OFFSET = _i("ALIGN_OFFSET", 20)               # seconds after the close
WATCHLIST_REFRESH = _t("WATCHLIST_REFRESH", "05:00")  # daily, local time
MONITOR_SECONDS = _i("MONITOR_SECONDS", 60)
HEARTBEAT_SECONDS = _i("HEARTBEAT_SECONDS", 30)

# ----------------------------------------------------------------- SMC engine
SWING = _i("SWING", 2)
SCAN_WINDOW = _i("SCAN_WINDOW", 250)
DISPLACEMENT = _f("DISPLACEMENT", 1.5)
OB_LOOKBACK = _i("OB_LOOKBACK", 10)
KEEP_FVG = _i("KEEP_FVG", 3)
KEEP_OB = _i("KEEP_OB", 3)
KEEP_BB = _i("KEEP_BB", 2)
KEEP_LIQ = _i("KEEP_LIQ", 2)
KEEP_SR = _i("KEEP_SR", 2)
KEEP_EVENTS = _i("KEEP_EVENTS", 2)

# OB and FVG are emitted only while unmitigated. Breaker blocks are by
# definition broken structure, so they are exempt from the freshness rule.
FRESH_ONLY = _i("FRESH_ONLY", 1)
MAX_FVG_FILL = _f("MAX_FVG_FILL", 50.0)
MAX_ZONE_AGE = _i("MAX_ZONE_AGE", 120)
ZONE_NEAR_PCT = {"4h": _f("NEAR_4H", 6.0), "1h": _f("NEAR_1H", 3.0)}
SIG_DIGITS = _i("SIG_DIGITS", 5)

# ------------------------------------------------------------------- DeepSeek
API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "low").strip()
TEMPERATURE = _f("TEMPERATURE", 0.2)
REQUEST_TIMEOUT = _i("REQUEST_TIMEOUT", 300)

COINS_PER_REQUEST = _i("COINS_PER_REQUEST", 25)     # 50 coins -> 2 requests
MAX_TOOL_ROUNDS = _i("MAX_TOOL_ROUNDS", 4)
AGENT_CONCURRENCY = _i("AGENT_CONCURRENCY", 2)

# A 4h POI must be at least this close to CMP before the agent is allowed to
# spend a 1h drill-down on it.
POI_MAX_DIST_PCT = _f("POI_MAX_DIST_PCT", 0.5)
MAX_DRILLDOWNS = _i("MAX_DRILLDOWNS", 10)           # per scan, hard ceiling
MAX_WATCHES = _i("MAX_WATCHES", 8)                  # concurrent hourly watches
WATCH_MAX_HOURS = _i("WATCH_MAX_HOURS", 4)          # expires by the next 4h scan

# USD per 1M tokens, for the /cost report only
PRICE_IN_HIT = _f("PRICE_IN_HIT", 0.014)
PRICE_IN_MISS = _f("PRICE_IN_MISS", 0.28)
PRICE_OUT = _f("PRICE_OUT", 1.32)

# ------------------------------------------------------------------- telegram
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# --------------------------------------------------------------------- signals
MIN_CONFIDENCE = _i("MIN_CONFIDENCE", 7)
MIN_RR = _f("MIN_RR", 1.5)
DUP_COOLDOWN_MIN = _i("DUP_COOLDOWN_MIN", 240)
PENDING_EXPIRY_MIN = _i("PENDING_EXPIRY_MIN", 480)
TP_SPLIT = [1 / 3, 1 / 3, 1 / 3]
MODES = ["day", "swing"]
