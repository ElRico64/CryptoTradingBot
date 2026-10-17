"""Continuous discovery: every few seconds, new tokens worth a look are pushed to the engine.

- DexScreener: newly listed token profiles and boosts (paid promotion: weak signal by itself).
- GeckoTerminal: organically trending pools and brand-new pools.
- PumpPortal: pump.fun graduations (migrations to an AMM) in real time, plus new launches that
  match a live narrative.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import aiohttp

from ..data.dexscreener import DexScreener
from ..data.geckoterminal import GeckoTerminal
from ..models import Candidate

log = logging.getLogger(__name__)

Emit = Callable[[Candidate], Awaitable[None]]


class DexScreenerScanner:
    def __init__(self, dex: DexScreener, chains: list[str], interval: float):
        self.dex = dex
        self.chains = set(chains)
        self.interval = interval

    async def poll_once(self, emit: Emit) -> int:
        n = 0
        for fetch, source in (
            (self.dex.latest_profiles, "dex_profile"),
            (self.dex.latest_boosts, "dex_boost"),
            (self.dex.top_boosts, "dex_top_boost"),
        ):
            try:
                items = await fetch()
            except Exception as exc:
                log.warning("DexScreener %s failed: %s", source, exc)
                continue
            for it in items:
                chain, addr = it.get("chainId"), it.get("tokenAddress")
                if chain in self.chains and addr:
                    await emit(Candidate(chain, addr, source))
                    n += 1
        return n

    async def run(self, emit: Emit) -> None:
        while True:
            await self.poll_once(emit)
            await asyncio.sleep(self.interval)


class GeckoTerminalScanner:
    def __init__(self, gecko: GeckoTerminal, chains: list[str], interval: float):
        self.gecko = gecko
        self.chains = chains
        self.interval = interval

    async def run(self, emit: Emit) -> None:
        while True:
            for chain in self.chains:
                for fetch, source in ((self.gecko.trending_tokens, "gt_trending"), (self.gecko.new_tokens, "gt_new")):
                    try:
                        for addr in await fetch(chain):
                            await emit(Candidate(chain, addr, source))
                    except Exception as exc:
                        log.warning("GeckoTerminal %s/%s failed: %s", chain, source, exc)
            await asyncio.sleep(self.interval)


class PumpPortalStream:
    """wss://pumpportal.fun/api/data: real-time pump.fun launches and graduations (free)."""

    URL = "wss://pumpportal.fun/api/data"

    def __init__(self, on_new_token: Callable[[dict], Awaitable[None]] | None = None):
        self.on_new_token = on_new_token

    @staticmethod
    def to_candidate(msg: dict) -> Candidate | None:
        mint = msg.get("mint")
        if not mint:
            return None
        if msg.get("txType") == "migrate":
            return Candidate("solana", mint, "pump_graduation", context={"pool": msg.get("pool", "")})
        return None

    async def run(self, emit: Emit) -> None:
        backoff = 1.0
        while True:
            try:
                async with aiohttp.ClientSession(trust_env=True) as sess:
                    async with sess.ws_connect(self.URL, heartbeat=20) as ws:
                        await ws.send_str(json.dumps({"method": "subscribeMigration"}))
                        if self.on_new_token:
                            await ws.send_str(json.dumps({"method": "subscribeNewToken"}))
                        log.info("PumpPortal stream connected")
                        backoff = 1.0
                        async for raw in ws:
                            if raw.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                msg = json.loads(raw.data)
                            except json.JSONDecodeError:
                                continue
                            cand = self.to_candidate(msg)
                            if cand:
                                await emit(cand)
                            elif msg.get("txType") == "create" and self.on_new_token:
                                await self.on_new_token(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("PumpPortal stream error: %s (reconnecting in %.0fs)", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
