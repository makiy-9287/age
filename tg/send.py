"""Telegram delivery. Every send is guarded so a formatting or network fault
can never take down the scan loop."""
from __future__ import annotations

import html
import logging

from telegram import Bot
from telegram.constants import ParseMode

import config

log = logging.getLogger("tg")
_BOT: Bot | None = None


def bot() -> Bot:
    global _BOT
    if _BOT is None:
        if not config.TG_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
        _BOT = Bot(config.TG_TOKEN)
    return _BOT


def set_bot(b: Bot):
    global _BOT
    _BOT = b


def e(x) -> str:
    return html.escape(str(x))


def fmt(p) -> str:
    p = float(p)
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:.8f}".rstrip("0").rstrip(".")


async def send(text: str, chat_id: str | None = None) -> int | None:
    cid = chat_id or config.TG_CHAT
    if not cid:
        log.error("TELEGRAM_CHAT_ID not set")
        return None
    for attempt in range(3):
        try:
            m = await bot().send_message(chat_id=cid, text=text,
                                         parse_mode=ParseMode.HTML,
                                         disable_web_page_preview=True)
            return m.message_id
        except Exception as ex:
            log.error("telegram send failed (%d/3): %s", attempt + 1,
                      str(ex).split("\n")[0][:200])
            if attempt == 1:
                # last resort: strip markup in case the payload broke the parser
                try:
                    m = await bot().send_message(chat_id=cid,
                                                 text=html.unescape(
                                                     _strip(text))[:4000])
                    return m.message_id
                except Exception:
                    pass
    return None


def _strip(t: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", t)





def signal_text(s: dict, sid: int) -> str:
    long = s["direction"] == "LONG"
    ref = (s["entry_low"] + s["entry_high"]) / 2
    risk = abs(ref - s["stop_loss"]) or 1e-12
    ez = (fmt(s["entry_low"]) if abs(s["entry_high"] - s["entry_low"]) < 1e-12
          else f"{fmt(s['entry_low'])} – {fmt(s['entry_high'])}")

    def pct(x):
        return f"{(x - ref) / ref * 100:+.2f}%"

    def rr(x):
        return f"{abs(x - ref) / risk:.1f}R"

    conf = "\n".join(f"  • {e(c)}" for c in (s.get("confirmations") or [])[:5]) or "  • –"
    return (
        f"<b>{'🟢 LONG' if long else '🔴 SHORT'}  |  {e(s['symbol'].split(':')[0])}</b>\n"
        f"🎯 MTF SNIPER  ·  <code>#{sid}</code>  ·  "
        f"conf {s.get('confidence', '-')}/10\n"
        f"────────────────\n"
        f"<b>Entry ({e(s['entry_type'])}):</b> <code>{ez}</code>\n"
        f"<b>SL:</b> <code>{fmt(s['stop_loss'])}</code> ({pct(s['stop_loss'])})\n"
        f"<b>TP1:</b> <code>{fmt(s['tp1'])}</code> ({pct(s['tp1'])} · {rr(s['tp1'])})\n"
        f"<b>TP2:</b> <code>{fmt(s['tp2'])}</code> ({pct(s['tp2'])} · {rr(s['tp2'])})\n"
        f"<b>TP3:</b> <code>{fmt(s['tp3'])}</code> ({pct(s['tp3'])} · {rr(s['tp3'])})\n"
        f"────────────────\n"
        f"<b>HTF:</b> {e(s.get('htf_bias', '-'))}  ·  <b>TF:</b> "
        f"{e(s.get('entry_tf', '-'))}  ·  <b>POI:</b> {e(s.get('poi', '-'))}\n"
        f"<b>Confirmations:</b>\n{conf}\n\n"
        f"<i>{e((s.get('reasoning') or '')[:500])}</i>")


ICON = {"FILLED": "🎯 <b>ENTRY FILLED</b>", "TP1": "✅ <b>TP1 HIT</b>",
        "TP2": "✅✅ <b>TP2 HIT</b>", "TP3": "🏆 <b>TP3 — COMPLETE</b>",
        "SL": "🛑 <b>STOP LOSS</b>", "EXPIRED": "⌛ <b>EXPIRED</b>",
        "INVALID": "❌ <b>INVALIDATED</b>", "CLOSED": "📕 <b>CLOSED</b>"}


def event_text(row, ev: str, price: float, extra: str = "") -> str:
    return (f"{ICON.get(ev, e(ev))}\n{e(row['symbol'].split(':')[0])} · "
            f"{row['direction']} · <code>#{row['id']}</code>\n"
            f"@ <code>{fmt(price)}</code>{('  ' + extra) if extra else ''}")
