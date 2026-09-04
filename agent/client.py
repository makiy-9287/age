"""DeepSeek agent: batches the snapshot, runs the tool loop, tracks token cost."""
from __future__ import annotations

import asyncio
import json
import logging

from openai import AsyncOpenAI

import config
from agent.prompt import SYSTEM, user_prompt
from agent.tools import TOOLS, ToolBox
from storage import db

log = logging.getLogger("agent")

READ_TOOLS = {"fetch_candles", "compute_indicators", "get_zones"}


def dumps(o) -> str:
    return json.dumps(o, separators=(",", ":"), default=float)


class Agent:
    def __init__(self, toolbox: ToolBox):
        self.tb = toolbox
        self.client = AsyncOpenAI(api_key=config.API_KEY, base_url=config.BASE_URL,
                                  timeout=config.REQUEST_TIMEOUT, max_retries=2)
        self._extra_ok = True   # flipped off if the API rejects reasoning params

    # ------------------------------------------------------------------ call
    async def _chat(self, messages: list[dict]):
        kwargs = dict(model=config.MODEL, messages=messages, tools=TOOLS,
                      tool_choice="auto", temperature=config.TEMPERATURE)
        if self._extra_ok and config.REASONING_EFFORT:
            kwargs["extra_body"] = {"reasoning_effort": config.REASONING_EFFORT,
                                    "thinking": {"type": "enabled"}}
        try:
            return await self.client.chat.completions.create(**kwargs)
        except Exception as ex:
            msg = str(ex)
            if self._extra_ok and ("reasoning_effort" in msg or "thinking" in msg
                                   or "Unrecognized" in msg or "invalid_request" in msg):
                log.warning("API rejected reasoning params - retrying without "
                            "them (%s)", msg.split("\n")[0][:120])
                self._extra_ok = False
                kwargs.pop("extra_body", None)
                return await self.client.chat.completions.create(**kwargs)
            raise

    @staticmethod
    def _account(resp) -> tuple[int, int, int]:
        u = getattr(resp, "usage", None)
        if not u:
            return 0, 0, 0
        hit = getattr(u, "prompt_cache_hit_tokens", None)
        miss = getattr(u, "prompt_cache_miss_tokens", None)
        if hit is None and miss is None:
            miss, hit = getattr(u, "prompt_tokens", 0) or 0, 0
        out = getattr(u, "completion_tokens", 0) or 0
        hit, miss = hit or 0, miss or 0
        db.add_usage(hit, miss, out)
        return hit, miss, out

    # ----------------------------------------------------------------- batch
    async def run_batch(self, coins: list[dict], idx: int, total: int) -> int:
        symbols = [c["s"] for c in coins]
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_prompt(idx, total, symbols)
             + dumps({"coins": coins})},
        ]
        sent = 0
        tool_calls_made = 0

        for rnd in range(config.MAX_TOOL_ROUNDS + 1):
            try:
                resp = await self._chat(messages)
            except Exception as ex:
                log.error("batch %d/%d request failed: %s", idx, total,
                          str(ex).split("\n")[0][:200])
                return sent

            hit, miss, out = self._account(resp)
            choice = resp.choices[0]
            msg = choice.message
            calls = list(msg.tool_calls or [])
            log.info("  batch %d/%d round %d | in %s hit + %s miss, out %s | "
                     "%d tool call(s)", idx, total, rnd + 1,
                     f"{hit:,}", f"{miss:,}", f"{out:,}", len(calls))

            if not calls:
                break

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name,
                                             "arguments": c.function.arguments}}
                               for c in calls]})

            for call in calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await self._dispatch(name, args)
                if name == "send_signal" and result.get("status") == "sent":
                    sent += 1
                if name in READ_TOOLS:
                    tool_calls_made += 1
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": dumps(result)[:6000]})

            if not any(c.function.name in READ_TOOLS for c in calls):
                # only signals were emitted - nothing left to reason about
                break

        if tool_calls_made:
            log.info("  batch %d/%d used %d data tool call(s)", idx, total,
                     tool_calls_made)
        return sent

    async def _dispatch(self, name: str, args: dict) -> dict:
        try:
            if name == "send_signal":
                return await self.tb.send_signal(args)
            if name == "fetch_candles":
                log.info("    tool fetch_candles %s %s",
                         args.get("symbol"), args.get("timeframe"))
                return self.tb.fetch_candles(args.get("symbol", ""),
                                             args.get("timeframe", "15m"),
                                             args.get("limit", 30))
            if name == "compute_indicators":
                log.info("    tool compute_indicators %s %s",
                         args.get("symbol"), args.get("timeframe"))
                return self.tb.compute_indicators(args.get("symbol", ""),
                                                  args.get("timeframe", "15m"))
            if name == "get_zones":
                log.info("    tool get_zones %s %s",
                         args.get("symbol"), args.get("timeframe"))
                return self.tb.get_zones(args.get("symbol", ""),
                                         args.get("timeframe", "15m"))
            return {"error": f"unknown tool {name}"}
        except Exception as ex:
            log.exception("tool %s failed", name)
            return {"error": str(ex)[:200]}

    # ----------------------------------------------------------------- scan
    async def analyse(self, coins: list[dict]) -> int:
        if not coins:
            return 0
        self.tb.known = {c["s"] for c in coins}
        n = config.COINS_PER_REQUEST
        batches = [coins[i:i + n] for i in range(0, len(coins), n)]
        payload_kb = len(dumps(coins)) / 1024
        log.info("agent: %d coins -> %d batches of %d (%.0f KB snapshot)",
                 len(coins), len(batches), n, payload_kb)

        sem = asyncio.Semaphore(config.AGENT_CONCURRENCY)

        async def run(i, b):
            async with sem:
                return await self.run_batch(b, i, len(batches))

        results = await asyncio.gather(
            *[run(i, b) for i, b in enumerate(batches, 1)], return_exceptions=True)
        total = 0
        for r in results:
            if isinstance(r, Exception):
                log.error("batch crashed: %s", r)
            else:
                total += r
        return total
