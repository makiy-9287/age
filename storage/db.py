"""SQLite: signals, lifecycle events, token/cost accounting."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

import config

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at INTEGER, symbol TEXT, direction TEXT, mode TEXT, entry_type TEXT,
  entry_low REAL, entry_high REAL, entry_price REAL, stop_loss REAL,
  tp1 REAL, tp2 REAL, tp3 REAL, confidence INTEGER, htf_bias TEXT,
  entry_tf TEXT, poi TEXT, rr REAL, reasoning TEXT, confirmations TEXT,
  status TEXT DEFAULT 'PENDING', activated_at INTEGER, closed_at INTEGER,
  exit_price REAL, exit_reason TEXT,
  tp1_hit INTEGER DEFAULT 0, tp2_hit INTEGER DEFAULT 0, tp3_hit INTEGER DEFAULT 0,
  pnl_pct REAL, r_mult REAL, msg_id INTEGER);
CREATE INDEX IF NOT EXISTS ix_status ON signals(status);
CREATE INDEX IF NOT EXISTS ix_symbol ON signals(symbol);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id INTEGER, ts INTEGER, event TEXT, price REAL, note TEXT);

CREATE TABLE IF NOT EXISTS usage (
  day TEXT PRIMARY KEY, requests INTEGER DEFAULT 0, hit INTEGER DEFAULT 0,
  miss INTEGER DEFAULT 0, out INTEGER DEFAULT 0);
"""


def conn():
    global _CONN
    with _LOCK:
        if _CONN is None:
            _CONN = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            _CONN.row_factory = sqlite3.Row
            _CONN.executescript(SCHEMA)
            _CONN.commit()
        return _CONN


def now() -> int:
    return int(time.time())


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ------------------------------------------------------------------- signals
def insert(sig: dict) -> int:
    with _LOCK:
        c = conn()
        cur = c.execute(
            """INSERT INTO signals (created_at,symbol,direction,mode,entry_type,
               entry_low,entry_high,entry_price,stop_loss,tp1,tp2,tp3,confidence,
               htf_bias,entry_tf,poi,rr,reasoning,confirmations,status,activated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now(), sig["symbol"], sig["direction"], sig["mode"], sig["entry_type"],
             sig["entry_low"], sig["entry_high"], sig.get("entry_price"),
             sig["stop_loss"], sig["tp1"], sig["tp2"], sig["tp3"],
             sig.get("confidence"), sig.get("htf_bias"), sig.get("entry_tf"),
             sig.get("poi"), sig.get("rr"), sig.get("reasoning"),
             json.dumps(sig.get("confirmations") or []),
             sig.get("status", "PENDING"), sig.get("activated_at")))
        c.commit()
        return int(cur.lastrowid)


def update(sid: int, **f):
    if not f:
        return
    with _LOCK:
        c = conn()
        c.execute(f"UPDATE signals SET {', '.join(k + '=?' for k in f)} WHERE id=?",
                  (*f.values(), sid))
        c.commit()


def event(sid: int, ev: str, price=None, note=""):
    with _LOCK:
        c = conn()
        c.execute("INSERT INTO events (signal_id,ts,event,price,note) VALUES (?,?,?,?,?)",
                  (sid, now(), ev, price, note))
        c.commit()


def get(sid: int):
    return conn().execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()


def open_signals():
    return conn().execute(
        "SELECT * FROM signals WHERE status IN ('PENDING','ACTIVE') ORDER BY id").fetchall()


def open_for(symbol: str):
    return conn().execute(
        "SELECT * FROM signals WHERE symbol=? AND status IN ('PENDING','ACTIVE')",
        (symbol,)).fetchall()


def recent(symbol: str, direction: str, minutes: int) -> bool:
    return conn().execute(
        "SELECT 1 FROM signals WHERE symbol=? AND direction=? AND created_at>? LIMIT 1",
        (symbol, direction, now() - minutes * 60)).fetchone() is not None


def last(n=10):
    return conn().execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def since(ts: int):
    return conn().execute(
        "SELECT * FROM signals WHERE created_at>=? ORDER BY created_at DESC",
        (ts,)).fetchall()


def performance(ts: int) -> dict:
    rows = conn().execute(
        "SELECT * FROM signals WHERE closed_at IS NOT NULL AND closed_at>=?",
        (ts,)).fetchall()
    if not rows:
        return {"n": 0, "wins": 0, "wr": 0.0, "pnl": 0.0, "r": 0.0, "modes": {}}
    wins = [r for r in rows if (r["pnl_pct"] or 0) > 0]
    modes: dict[str, dict] = {}
    for r in rows:
        m = modes.setdefault(r["mode"], {"n": 0, "w": 0, "pnl": 0.0})
        m["n"] += 1
        m["w"] += 1 if (r["pnl_pct"] or 0) > 0 else 0
        m["pnl"] += r["pnl_pct"] or 0
    return {"n": len(rows), "wins": len(wins),
            "wr": round(len(wins) / len(rows) * 100, 1),
            "pnl": round(sum(r["pnl_pct"] or 0 for r in rows), 2),
            "r": round(sum(r["r_mult"] or 0 for r in rows), 2),
            "modes": modes}


# --------------------------------------------------------------------- usage
def add_usage(hit: int, miss: int, out: int):
    with _LOCK:
        c = conn()
        c.execute("""INSERT INTO usage (day,requests,hit,miss,out) VALUES (?,1,?,?,?)
                     ON CONFLICT(day) DO UPDATE SET requests=requests+1,
                     hit=hit+?, miss=miss+?, out=out+?""",
                  (today(), hit, miss, out, hit, miss, out))
        c.commit()


def usage(day: str | None = None):
    return conn().execute("SELECT * FROM usage WHERE day=?", (day or today(),)).fetchone()


def usage_all():
    return conn().execute("SELECT * FROM usage ORDER BY day DESC LIMIT 14").fetchall()


def cost_of(row) -> float:
    if not row:
        return 0.0
    return (row["hit"] / 1e6 * config.PRICE_IN_HIT
            + row["miss"] / 1e6 * config.PRICE_IN_MISS
            + row["out"] / 1e6 * config.PRICE_OUT)
