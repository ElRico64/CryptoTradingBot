"""Paper trading with a realistic cost model, so paper results are not fantasy.

Fills use the current DexScreener price adjusted for constant-product price impact
(trade size vs. the pool's quote reserve), a latency slippage allowance, and fees on both sides.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from ..models import Fill, PairSnapshot, token_key
from .base import ExecutionError, Executor

PairSource = Callable[[str, str], Awaitable[PairSnapshot | None]]


class PaperExecutor(Executor):
    shared_cash = True  # one simulated USD balance across chains

    def __init__(self, starting_usd: float, pair_source: PairSource, fee_pct: float = 1.0, extra_slippage_pct: float = 1.0):
        self.cash = starting_usd
        self.pair_source = pair_source
        self.fee_pct = fee_pct
        self.extra_slippage_pct = extra_slippage_pct
        self.holdings: dict[str, float] = {}

    def impact_pct(self, usd: float, liquidity_usd: float) -> float:
        reserve = max(liquidity_usd / 2, 1.0)  # quote side of the pool
        return 100 * usd / (reserve + usd)

    async def _pair(self, chain: str, token: str) -> PairSnapshot:
        pair = await self.pair_source(chain, token)
        if pair is None or pair.price_usd <= 0:
            raise ExecutionError(f"no price for {token}")
        return pair

    async def buy(self, chain: str, token: str, usd: float) -> Fill:
        if usd > self.cash:
            raise ExecutionError("insufficient paper balance")
        pair = await self._pair(chain, token)
        cost_pct = self.impact_pct(usd, pair.liquidity_usd) + self.extra_slippage_pct
        price = pair.price_usd * (1 + cost_pct / 100)
        qty = usd * (1 - self.fee_pct / 100) / price
        self.cash -= usd
        k = token_key(chain, token)
        self.holdings[k] = self.holdings.get(k, 0.0) + qty
        return Fill(qty=qty, usd=usd, price=usd / qty, tx="paper")

    async def sell(self, chain: str, token: str, fraction: float) -> Fill:
        k = token_key(chain, token)
        held = self.holdings.get(k, 0.0)
        if held <= 0:
            raise ExecutionError("nothing to sell")
        qty = held if fraction >= 0.999 else held * fraction
        pair = await self.pair_source(chain, token)
        if pair is None or pair.price_usd <= 0:
            # Delisted / rugged: in a simulation the honest assumption is that it is worthless.
            self.holdings.pop(k, None)
            return Fill(qty=held, usd=0.0, price=0.0, tx="paper-unsellable")
        gross = qty * pair.price_usd
        cost_pct = self.impact_pct(gross, pair.liquidity_usd) + self.extra_slippage_pct
        usd = gross * (1 - cost_pct / 100) * (1 - self.fee_pct / 100)
        self.holdings[k] = held - qty
        if self.holdings[k] <= 1e-12:
            del self.holdings[k]
        self.cash += usd
        return Fill(qty=qty, usd=usd, price=usd / qty if qty else 0.0, tx="paper")

    async def roundtrip_loss_pct(self, chain: str, token: str, usd: float) -> float:
        pair = await self._pair(chain, token)
        buy_cost = self.impact_pct(usd, pair.liquidity_usd) + self.extra_slippage_pct
        sell_cost = self.impact_pct(usd, pair.liquidity_usd) + self.extra_slippage_pct
        keep = (1 - buy_cost / 100) * (1 - sell_cost / 100) * (1 - self.fee_pct / 100) ** 2
        return 100 * (1 - keep)

    async def cash_usd(self, chain: str) -> float:
        return self.cash
