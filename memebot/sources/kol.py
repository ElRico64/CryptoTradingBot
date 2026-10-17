"""Smart-money tracker: follow the wallets of top memecoin traders (KOLs) on Solana.

Copying one wallet blindly gets you exit-liquidity'd (many KOLs buy to sell to followers).
So we only act on *consensus*: several independent tracked wallets accumulating the same token
inside a short window, weighted by how much you trust each wallet. The tracker also reports
when those same wallets sell, which the exit engine uses to get out alongside them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..config import KOLConfig
from ..data.solana_rpc import USDC_MINT, USDT_MINT, WSOL_MINT, SolanaRPC
from ..models import Candidate
from ..util import safe_float

log = logging.getLogger(__name__)

QUOTE_MINTS = {WSOL_MINT, USDC_MINT, USDT_MINT}
Emit = Callable[[Candidate], Awaitable[None]]


@dataclass
class KolTrade:
    wallet: str
    mint: str
    side: str  # "buy" | "sell"
    token_delta: float
    sol_delta: float
    ts: float
    signature: str


def _account_keys(tx: dict) -> list[str]:
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return [k["pubkey"] if isinstance(k, dict) else k for k in keys]


def parse_wallet_swaps(tx: dict, wallet: str) -> list[KolTrade]:
    """Token buys/sells by `wallet` in a jsonParsed transaction, from balance deltas.

    Works for any DEX or aggregator because it only looks at what the wallet ended up with.
    """
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    keys = _account_keys(tx)
    sol_delta = 0.0
    if wallet in keys:
        i = keys.index(wallet)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pre) and i < len(post):
            sol_delta = (post[i] - pre[i]) / 1e9

    deltas: dict[str, float] = {}
    for sign, field in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(field) or []:
            if b.get("owner") != wallet:
                continue
            amt = b.get("uiTokenAmount") or {}
            ui = safe_float(amt.get("uiAmountString") or amt.get("uiAmount"))
            deltas[b["mint"]] = deltas.get(b["mint"], 0.0) + sign * ui

    sol_delta += deltas.pop(WSOL_MINT, 0.0)
    for stable in (USDC_MINT, USDT_MINT):
        deltas.pop(stable, None)
    ts = float(tx.get("blockTime") or time.time())
    sig = ((tx.get("transaction") or {}).get("signatures") or [""])[0]
    trades = []
    for mint, d in deltas.items():
        if abs(d) <= 0 or mint in QUOTE_MINTS:
            continue
        trades.append(KolTrade(wallet, mint, "buy" if d > 0 else "sell", d, sol_delta, ts, sig))
    return trades


class KOLTracker:
    def __init__(self, cfg: KOLConfig, rpc: SolanaRPC | None):
        self.cfg = cfg
        self.rpc = rpc
        self.wallets = {w.address: w for w in cfg.wallets}
        self.trades: dict[str, list[KolTrade]] = {}
        self._last_sig: dict[str, str] = {}
        self._emitted: dict[str, float] = {}

    def name(self, wallet: str) -> str:
        w = self.wallets.get(wallet)
        return (w.name or wallet[:6]) if w else wallet[:6]

    def record(self, trade: KolTrade) -> None:
        if trade.side == "buy" and -trade.sol_delta < self.cfg.min_buy_sol:
            return  # dust buys are noise (or bait)
        self.trades.setdefault(trade.mint, []).append(trade)

    def _recent(self, mint: str, now: float) -> list[KolTrade]:
        horizon = now - self.cfg.window_min * 60
        return [t for t in self.trades.get(mint, []) if t.ts >= horizon]

    def consensus(self, mint: str, now: float | None = None) -> tuple[float, list[str]]:
        """Total weight of distinct tracked wallets that bought and have not sold since."""
        now = now or time.time()
        holders: dict[str, KolTrade] = {}
        for t in sorted(self._recent(mint, now), key=lambda t: t.ts):
            if t.side == "buy":
                holders[t.wallet] = t
            else:
                holders.pop(t.wallet, None)
        weight = sum(self.wallets[w].weight if w in self.wallets else 1.0 for w in holders)
        return weight, list(holders)

    def sellers(self, mint: str, wallets: list[str], since: float) -> int:
        """How many of `wallets` sold `mint` after `since`."""
        sold = {t.wallet for t in self.trades.get(mint, []) if t.side == "sell" and t.ts >= since}
        return len(sold & set(wallets))

    async def _process_signature(self, wallet: str, sig: str, emit: Emit) -> None:
        assert self.rpc is not None
        tx = await self.rpc.get_transaction(sig)
        if not tx:
            return
        for trade in parse_wallet_swaps(tx, wallet):
            self.record(trade)
            log.info("KOL %s %s %s (%.2f SOL)", self.name(wallet), trade.side, trade.mint, abs(trade.sol_delta))
            if trade.side != "buy":
                continue
            weight, wallets = self.consensus(trade.mint)
            if weight >= self.cfg.min_weight and time.time() - self._emitted.get(trade.mint, 0) > 120:
                self._emitted[trade.mint] = time.time()
                await emit(
                    Candidate(
                        "solana",
                        trade.mint,
                        "kol",
                        context={"kol_weight": weight, "kol_wallets": wallets, "kol_names": [self.name(w) for w in wallets]},
                    )
                )

    async def poll_wallet(self, wallet: str, emit: Emit) -> None:
        assert self.rpc is not None
        sigs = await self.rpc.get_signatures(wallet, limit=15)
        if not sigs:
            return
        last = self._last_sig.get(wallet)
        self._last_sig[wallet] = sigs[0]["signature"]
        if last is None:
            return  # first poll only sets the cursor; history is not a signal
        fresh = []
        for s in sigs:
            if s["signature"] == last:
                break
            if not s.get("err"):
                fresh.append(s["signature"])
        for sig in reversed(fresh):
            try:
                await self._process_signature(wallet, sig, emit)
            except Exception as exc:
                log.debug("KOL tx %s failed: %s", sig, exc)

    async def run(self, emit: Emit) -> None:
        if not self.wallets or self.rpc is None:
            log.info("KOL tracker idle (no wallets configured)")
            return
        log.info("Tracking %d KOL wallets", len(self.wallets))
        while True:
            for wallet in list(self.wallets):
                try:
                    await self.poll_wallet(wallet, emit)
                except Exception as exc:
                    log.warning("KOL poll %s failed: %s", self.name(wallet), exc)
            await asyncio.sleep(self.cfg.poll_interval_sec)
