"""GeckoTerminal public API: 1-minute candles, trending pools and new pools (no key needed)."""

from __future__ import annotations

from typing import NamedTuple

from ..util import safe_float
from .http import Http

BASE = "https://api.geckoterminal.com/api/v2"
NETWORKS = {"solana": "solana", "base": "base", "bsc": "bsc", "ethereum": "eth"}
HEADERS = {"Accept": "application/json;version=20230302"}


class Candle(NamedTuple):
    ts: int
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: float


def parse_ohlcv(data: dict) -> list[Candle]:
    rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    candles = [Candle(int(r[0]), *(safe_float(x) for x in r[1:6])) for r in rows if len(r) >= 6]
    candles.sort(key=lambda c: c.ts)  # API returns newest first
    return candles


def parse_pool_tokens(data: dict, network: str) -> list[str]:
    """Base-token addresses from a pools listing ('solana_<addr>' ids)."""
    out = []
    prefix = network + "_"
    for pool in (data or {}).get("data") or []:
        tok_id = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        if tok_id.startswith(prefix):
            out.append(tok_id[len(prefix) :])
    return out


class GeckoTerminal:
    def __init__(self, http: Http):
        self.http = http

    async def ohlcv(self, chain: str, pool: str, timeframe: str = "minute", aggregate: int = 1, limit: int = 120) -> list[Candle]:
        network = NETWORKS[chain]
        data = await self.http.get_json(
            f"{BASE}/networks/{network}/pools/{pool}/ohlcv/{timeframe}",
            params={"aggregate": aggregate, "limit": limit, "currency": "usd"},
            headers=HEADERS,
        )
        return parse_ohlcv(data)

    async def trending_tokens(self, chain: str) -> list[str]:
        network = NETWORKS[chain]
        data = await self.http.get_json(f"{BASE}/networks/{network}/trending_pools", headers=HEADERS)
        return parse_pool_tokens(data, network)

    async def new_tokens(self, chain: str) -> list[str]:
        network = NETWORKS[chain]
        data = await self.http.get_json(f"{BASE}/networks/{network}/new_pools", headers=HEADERS)
        return parse_pool_tokens(data, network)
