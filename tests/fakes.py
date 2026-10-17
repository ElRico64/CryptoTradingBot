"""Test doubles and fixture builders."""

from __future__ import annotations

import time
from typing import Any, Callable

from memebot.data.geckoterminal import Candle
from memebot.models import PairSnapshot


class FakeHttp:
    """Routes requests by (method, substring of URL) to canned responses or callables."""

    def __init__(self, routes: list[tuple[str, str, Any]] | None = None):
        self.routes = routes or []
        self.calls: list[tuple[str, str, dict | None, Any]] = []

    def add(self, method: str, fragment: str, response: Any) -> None:
        self.routes.insert(0, (method, fragment, response))

    async def request(self, method, url, *, params=None, json=None, headers=None, as_text=False):
        self.calls.append((method, url, params, json))
        for m, frag, resp in self.routes:
            if m == method and frag in url:
                return resp(url, params, json) if callable(resp) else resp
        raise AssertionError(f"unexpected request {method} {url} {params}")

    async def get_json(self, url, params=None, headers=None):
        return await self.request("GET", url, params=params, headers=headers)

    async def get_text(self, url, params=None, headers=None):
        return await self.request("GET", url, params=params, headers=headers, as_text=True)

    async def post_json(self, url, payload, headers=None):
        return await self.request("POST", url, json=payload, headers=headers)

    async def close(self):
        return None


def make_pair(
    address: str = "TokenMint1111111111111111111111111111111111",
    chain: str = "solana",
    price: float = 0.001,
    liquidity: float = 80_000,
    mcap: float = 600_000,
    age_min: float = 90,
    buys_m5: int = 70,
    sells_m5: int = 30,
    buys_h1: int = 500,
    sells_h1: int = 300,
    vol_m5: float = 25_000,
    vol_h1: float = 150_000,
    pc_m5: float = 6.0,
    pc_h1: float = 45.0,
    symbol: str = "TEST",
    name: str = "Test Token",
    quote: str = "SOL",
) -> PairSnapshot:
    now = time.time()
    return PairSnapshot(
        chain=chain,
        token_address=address,
        pair_address="Pool" + address[:20],
        dex_id="raydium",
        symbol=symbol,
        name=name,
        price_usd=price,
        liquidity_usd=liquidity,
        market_cap=mcap,
        fdv=mcap,
        created_at_ms=int((now - age_min * 60) * 1000),
        txns={
            "m5": {"buys": buys_m5, "sells": sells_m5},
            "h1": {"buys": buys_h1, "sells": sells_h1},
            "h6": {"buys": buys_h1 * 3, "sells": sells_h1 * 3},
            "h24": {"buys": buys_h1 * 5, "sells": sells_h1 * 5},
        },
        volume={"m5": vol_m5, "h1": vol_h1, "h6": vol_h1 * 3, "h24": vol_h1 * 5},
        price_change={"m5": pc_m5, "h1": pc_h1, "h6": 120.0, "h24": 300.0},
        quote_symbol=quote,
        socials=2,
        websites=1,
        fetched_at=now,
    )


def pair_json(p: PairSnapshot) -> dict:
    """DexScreener wire format for a PairSnapshot."""
    return {
        "chainId": p.chain,
        "dexId": p.dex_id,
        "url": f"https://dexscreener.com/{p.chain}/{p.pair_address}",
        "pairAddress": p.pair_address,
        "baseToken": {"address": p.token_address, "name": p.name, "symbol": p.symbol},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112", "name": "Wrapped SOL", "symbol": p.quote_symbol},
        "priceNative": "0.00001",
        "priceUsd": str(p.price_usd),
        "txns": p.txns,
        "volume": p.volume,
        "priceChange": p.price_change,
        "liquidity": {"usd": p.liquidity_usd, "base": 1, "quote": 1},
        "fdv": p.fdv,
        "marketCap": p.market_cap,
        "pairCreatedAt": p.created_at_ms,
        "info": {"websites": [{"url": "x"}], "socials": [{"type": "twitter"}, {"type": "telegram"}]},
    }


def candles_from_closes(closes: list[float], vols: list[float] | None = None, start_ts: int = 1_700_000_000) -> list[Candle]:
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        h = max(o, c) * 1.01
        lo = min(o, c) * 0.99
        v = vols[i] if vols else 1000.0
        out.append(Candle(start_ts + 60 * i, o, h, lo, c, v))
        prev = c
    return out


def clean_rugcheck() -> dict:
    return {
        "mintAuthority": None,
        "freezeAuthority": None,
        "risks": [],
        "score": 1,
        "score_normalised": 1,
        "rugged": False,
        "totalHolders": 1800,
        "markets": [{"pubkey": "MarketPool", "liquidityA": "VaultA", "liquidityB": "VaultB"}],
        "topHolders": [
            {"address": "VaultA", "owner": "PoolAuth", "pct": 22.0, "insider": False},
            *[{"address": f"Holder{i}", "owner": f"Owner{i}", "pct": 1.5, "insider": False} for i in range(12)],
        ],
    }


Clock = Callable[[], float]
