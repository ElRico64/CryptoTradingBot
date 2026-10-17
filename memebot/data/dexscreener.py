"""DexScreener public API: pair stats, discovery feeds and search (no key needed)."""

from __future__ import annotations

import logging
from typing import Any

from ..models import PairSnapshot, norm_addr
from ..util import safe_float
from .http import Http

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"
WINDOWS = ("m5", "h1", "h6", "h24")


def parse_pair(raw: dict[str, Any]) -> PairSnapshot | None:
    try:
        base = raw["baseToken"]
        chain = raw["chainId"]
        info = raw.get("info") or {}
        txns = {
            w: {
                "buys": int((raw.get("txns") or {}).get(w, {}).get("buys", 0) or 0),
                "sells": int((raw.get("txns") or {}).get(w, {}).get("sells", 0) or 0),
            }
            for w in WINDOWS
        }
        return PairSnapshot(
            chain=chain,
            token_address=base["address"],
            pair_address=raw.get("pairAddress", ""),
            dex_id=raw.get("dexId", ""),
            symbol=base.get("symbol", "") or "",
            name=base.get("name", "") or "",
            price_usd=safe_float(raw.get("priceUsd")),
            liquidity_usd=safe_float((raw.get("liquidity") or {}).get("usd")),
            market_cap=safe_float(raw.get("marketCap")),
            fdv=safe_float(raw.get("fdv")),
            created_at_ms=int(raw["pairCreatedAt"]) if raw.get("pairCreatedAt") else None,
            txns=txns,
            volume={w: safe_float((raw.get("volume") or {}).get(w)) for w in WINDOWS},
            price_change={w: safe_float((raw.get("priceChange") or {}).get(w)) for w in WINDOWS},
            quote_symbol=(raw.get("quoteToken") or {}).get("symbol", "") or "",
            boosts=int((raw.get("boosts") or {}).get("active", 0) or 0),
            socials=len(info.get("socials") or []),
            websites=len(info.get("websites") or []),
            url=raw.get("url", ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.debug("Unparseable pair: %s", exc)
        return None


def best_pairs(pairs: list[PairSnapshot]) -> dict[str, PairSnapshot]:
    """For each token, keep the pair with the deepest liquidity (that is where price is real)."""
    best: dict[str, PairSnapshot] = {}
    for p in pairs:
        k = norm_addr(p.chain, p.token_address)
        if k not in best or p.liquidity_usd > best[k].liquidity_usd:
            best[k] = p
    return best


class DexScreener:
    def __init__(self, http: Http):
        self.http = http

    async def _list(self, path: str) -> list[dict]:
        data = await self.http.get_json(BASE + path)
        return data if isinstance(data, list) else []

    async def latest_profiles(self) -> list[dict]:
        return await self._list("/token-profiles/latest/v1")

    async def latest_boosts(self) -> list[dict]:
        return await self._list("/token-boosts/latest/v1")

    async def top_boosts(self) -> list[dict]:
        return await self._list("/token-boosts/top/v1")

    async def pairs_for_tokens(self, chain: str, addresses: list[str]) -> dict[str, PairSnapshot]:
        """Best pair per token address (keys normalised with norm_addr). Batches of 30."""
        out: dict[str, PairSnapshot] = {}
        uniq = list(dict.fromkeys(addresses))
        for i in range(0, len(uniq), 30):
            chunk = uniq[i : i + 30]
            data = await self.http.get_json(f"{BASE}/tokens/v1/{chain}/{','.join(chunk)}")
            pairs = [p for p in (parse_pair(r) for r in (data or [])) if p]
            wanted = {norm_addr(chain, a) for a in chunk}
            for k, p in best_pairs(pairs).items():
                if k in wanted:
                    out[k] = p
        return out

    async def best_pair(self, chain: str, address: str) -> PairSnapshot | None:
        return (await self.pairs_for_tokens(chain, [address])).get(norm_addr(chain, address))

    async def search(self, query: str) -> list[PairSnapshot]:
        data = await self.http.get_json(f"{BASE}/latest/dex/search", params={"q": query})
        return [p for p in (parse_pair(r) for r in (data or {}).get("pairs") or []) if p]
